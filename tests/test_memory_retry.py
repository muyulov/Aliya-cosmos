"""记忆层重试策略与死信队列测试。

本文件锁住六条不变量，任何一条被「优化掉」都会当场变红：

1. **可重试性由错误类型判定**（设计文档 §6.5 / §10.1）：下层三支的
   `*TimeoutError` / `*ConnectionError` / `*RequestError` 是瞬时故障，重试有意义；
   schema 不符、输入非法、库错是永久故障，重试只会白花成本。
2. **退避按 `backoff * 2**(n-1)` 递增**：第一次重试用基数，之后逐次翻倍。
3. **重试用尽抛最后一次异常**，且 `__cause__` 链保留（调用方要顺链排查）。
4. **不可重试异常立刻原样上抛**，一次都不重试。
5. **死信是独立 JSONL**：append-only、坏行可跳过、重写走原子替换——阶段 A 失败
   恰恰意味着库可能写不进去，DLQ 必须还能写（设计文档 §6.5）。
6. **`payload` 按阶段分流**：阶段 A 存完整载荷（原文必须最终进库），阶段 B 为 None
   （原文已在库，重放时读库即可）。

关于「本模块不写死信」：`run_with_retry` 只负责重试循环并把最后一次异常上抛，
**进死信是调用方的事**（`MemoryService`）。这里的用例把该职责边界一起钉住——
`test_重试用尽进死信` 与 `test_永久故障零重试直接死信` 都是「测试内的调用方」
显式捕获异常后自己写入死信箱，证明两者可独立组合、也可独立测试。

测试名刻意不含英文断言关键词（`pass` / `assert` / `is` / `not` / `in` 之类），
否则会被日志文件的 `tmp_path` 命中而假红。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from core.decision.errors import (
    DecisionConnectionError,
    DecisionRequestError,
    DecisionTimeoutError,
)
from core.embedding.errors import (
    EmbeddingConnectionError,
    EmbeddingRequestError,
    EmbeddingResponseError,
    EmbeddingTimeoutError,
)
from core.llm.errors import (
    LLMConnectionError,
    LLMRequestError,
    LLMSchemaError,
    LLMTimeoutError,
)
from core.memory.errors import MemoryInputError, MemoryStoreError
from core.memory.ingest.retry import (
    STAGE_STRUCTURED,
    STAGE_UTTERANCE,
    DeadLetter,
    DeadLetterBox,
    RetryPolicy,
    is_retryable,
    run_with_retry,
)


async def _no_sleep(_seconds: float) -> None:
    """零耗时的假 sleep：用例不必真等退避（退避基数在测试里也设 0，双保险）。"""
    return None


def _deadletter(count: int, stage: str = STAGE_UTTERANCE) -> list[DeadLetter]:
    """造若干条最小死信，便于批量往返与重写用例。"""
    return [
        DeadLetter(
            utterance_id=f"u{i}",
            stage=stage,
            error_type="LLMTimeoutError",
            error_message=f"第{i}条超时",
            attempts=2,
            payload={"kind": "dialogue", "text": f"第{i}句"},
            failed_at=1000 + i,
        )
        for i in range(count)
    ]


def _mutate_field(field: str, value: object) -> str:
    """把一条好死信的某个字段改成非法值并序列化，用来造「合法 JSON 但坏结构」的行。"""
    raw = cast("dict[str, object]", json.loads(_deadletter(1)[0].to_json()))
    raw[field] = value
    return json.dumps(raw, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 可重试性判定
# ---------------------------------------------------------------------------


def test_瞬时故障可重试() -> None:
    """超时 / 连接 / 请求错误三支各三类都是瞬时故障，重试有意义。"""
    assert is_retryable(LLMTimeoutError("超时")) is True
    assert is_retryable(LLMConnectionError("断网")) is True
    assert (
        is_retryable(
            LLMRequestError("503", status_code=503, endpoint="e", model="m", request_id=None)
        )
        is True
    )
    assert is_retryable(EmbeddingTimeoutError("超时")) is True
    assert is_retryable(EmbeddingConnectionError("连接被拒")) is True
    assert (
        is_retryable(
            EmbeddingRequestError("429", status_code=429, endpoint="e", model="m", request_id=None)
        )
        is True
    )
    assert is_retryable(DecisionTimeoutError("超时")) is True
    assert is_retryable(DecisionConnectionError("DNS 失败")) is True
    assert (
        is_retryable(
            DecisionRequestError("500", status_code=500, endpoint="e", model="m", request_id=None)
        )
        is True
    )


def test_永久故障不重试() -> None:
    """schema 不符 / 输入非法 / 库错重试无意义，直接走死信。"""
    assert is_retryable(MemoryInputError("空文本")) is False
    assert is_retryable(MemoryStoreError("写不进去", operation="UPSERT_UTTERANCE")) is False
    assert is_retryable(LLMSchemaError("召回体缺字段")) is False


def test_非本层已知异常一律不重试() -> None:
    """本层只认自己列出的九类瞬时故障；外来的 ValueError 之类不该被擅自重试。"""
    assert is_retryable(ValueError("谁抛的都不知道")) is False
    assert is_retryable(RuntimeError("Kùzu 裸抛")) is False
    assert is_retryable(EmbeddingResponseError("条数不符")) is False


# ---------------------------------------------------------------------------
# 退避策略
# ---------------------------------------------------------------------------


def test_退避按基数翻倍() -> None:
    """delay(n) = backoff * 2**(n-1)：第 1 次用基数，之后逐次翻倍。"""
    policy = RetryPolicy(max_attempts=4, backoff=0.5)
    assert policy.delay(1) == 0.5
    assert policy.delay(2) == 1.0
    assert policy.delay(3) == 2.0
    assert policy.delay(4) == 4.0


def test_退避基数可配成零() -> None:
    """退避基数抽成实例字段（来自配置），测试里可设 0，用例不必真等。"""
    policy = RetryPolicy(max_attempts=3, backoff=0.0)
    assert policy.delay(1) == 0.0
    assert policy.delay(5) == 0.0


def test_策略字段可读() -> None:
    """max_attempts / backoff 是公开只读（来自配置，调用方排日志要用）。"""
    policy = RetryPolicy(max_attempts=3, backoff=1.0)
    assert policy.max_attempts == 3
    assert policy.backoff == 1.0


# ---------------------------------------------------------------------------
# 重试循环
# ---------------------------------------------------------------------------


async def test_退避后重试成功() -> None:
    """第一次必败、第二次成功：尝试数为 2；正常返回结果，不进死信。"""
    policy = RetryPolicy(max_attempts=3, backoff=0.0)
    call_count = 0

    async def operation() -> str:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise LLMTimeoutError("第一次超时")
        return "成功了"

    result, attempt_count = await run_with_retry(operation, policy=policy, sleep=_no_sleep)
    assert result == "成功了"
    assert attempt_count == 2
    assert call_count == 2


async def test_退避被真的等过() -> None:
    """注入的 sleep 必须被调用，且等待时长等于 policy.delay(attempt)。"""
    policy = RetryPolicy(max_attempts=3, backoff=0.25)
    waited: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waited.append(seconds)

    nth = 0

    async def operation() -> str:
        nonlocal nth
        nth += 1
        if nth < 3:
            raise LLMConnectionError("先断两次")
        return "第三次通了"

    _, attempt_count = await run_with_retry(operation, policy=policy, sleep=fake_sleep)
    # 首次失败 → 等 delay(1)=0.25；第二次失败 → 等 delay(2)=0.5；第三次成功
    assert attempt_count == 3
    assert waited == [0.25, 0.5]


async def test_重试用尽上抛最后一次() -> None:
    """全部尝试都失败：抛出的必须是最后一次那个异常，不吞不换。"""
    policy = RetryPolicy(max_attempts=3, backoff=0.0)
    index = 0

    async def operation() -> str:
        nonlocal index
        index += 1
        # 上限**在循环内钉住**：断言排在每次调用里，若 max_attempts 被改大或判断被删，
        # 循环不会等到 delay() 因 2**n 溢出崩溃（attempt≈1025）才失败，而是在第
        # max_attempts + 2 次调用时立刻以断言失配变红——这样用例才区分得出
        # 「正确的上限」与「任意一个错误的上限」，而不是只能靠崩溃变红。
        assert index <= policy.max_attempts + 1, f"调用次数 {index} 超过上限"
        raise LLMTimeoutError(f"第{index}次")

    with pytest.raises(LLMTimeoutError) as captured:
        _ = await run_with_retry(operation, policy=policy, sleep=_no_sleep)
    assert index == 4  # 首次 + 3 次重试
    assert "第4次" in str(captured.value)


async def test_重试用尽保留异常链() -> None:
    """上抛的异常要带 __cause__，调用方能顺链排查原始报错。"""
    policy = RetryPolicy(max_attempts=2, backoff=0.0)
    inner = OSError("TLS 握手失败")
    nth = 0

    async def operation() -> str:
        nonlocal nth
        nth += 1
        # 每次都是**新的**异常实例，但原底（TLS 握手失败）是同一个
        raise LLMConnectionError(f"第{nth}次连不上") from inner

    with pytest.raises(LLMConnectionError) as captured:
        _ = await run_with_retry(operation, policy=policy, sleep=_no_sleep)
    assert nth == 3  # 首次 + 2 次重试
    # 上抛的是最后一次的实例；链条是「最后一次 →(cause) 上一次 →(cause) TLS 原底」，
    # 顺着走两跳能摸到最早的原始报错。
    assert "第3次" in str(captured.value)
    previous = captured.value.__cause__
    assert previous is not None
    assert previous.__cause__ is inner


async def test_永久故障立刻上抛不重试() -> None:
    """不可重试的异常立刻上抛，一次都不重试。"""
    policy = RetryPolicy(max_attempts=5, backoff=0.0)
    call_count = 0

    async def operation() -> str:
        nonlocal call_count
        call_count += 1
        raise LLMSchemaError("召回体缺 content")

    with pytest.raises(LLMSchemaError):
        _ = await run_with_retry(operation, policy=policy, sleep=_no_sleep)
    assert call_count == 1


async def test_零重试配置只跑一次() -> None:
    """max_attempts 为 0 时退化成「试一次」，不进重试循环。"""
    policy = RetryPolicy(max_attempts=0, backoff=0.0)
    call_count = 0

    async def operation() -> str:
        nonlocal call_count
        call_count += 1
        raise LLMTimeoutError("超时")

    with pytest.raises(LLMTimeoutError):
        _ = await run_with_retry(operation, policy=policy, sleep=_no_sleep)
    assert call_count == 1


# ---------------------------------------------------------------------------
# 死信条往返
# ---------------------------------------------------------------------------


def test_死信条JSON往返一致() -> None:
    """to_json / from_json 往返后逐字段相等，且中文不被转义。"""
    original = DeadLetter(
        utterance_id="u1",
        stage=STAGE_STRUCTURED,
        error_type="LLMSchemaError",
        error_message="抽取结果不符合 schema",
        attempts=3,
        payload=None,
        failed_at=1735689600,
    )
    text = original.to_json()
    assert "\\u" not in text  # ensure_ascii=False
    assert json.loads(text)["stage"] == STAGE_STRUCTURED
    assert DeadLetter.from_json(text) == original


def test_死信条字段可读() -> None:
    """阶段 A 的死信要带完整载荷，否则原文永久丢失。"""
    entry = _deadletter(1)[0]
    assert entry.payload == {"kind": "dialogue", "text": "第0句"}
    assert entry.stage == STAGE_UTTERANCE
    assert entry.utterance_id == "u0"


def test_阶段常量取值符合约定() -> None:
    """Task 9 的消费方靠这两个常量拼死信条，取值不能随手改。"""
    assert STAGE_UTTERANCE == "原文落库"
    assert STAGE_STRUCTURED == "结构化"


# ---------------------------------------------------------------------------
# 死信箱
# ---------------------------------------------------------------------------


def test_死信箱追加与读回(tmp_path: Path) -> None:
    """append 是 append-only：两次追加读回两条，顺序不变。"""
    box = DeadLetterBox(tmp_path / "deadletter.jsonl")
    entry = _deadletter(2)
    box.append(entry[0])
    box.append(entry[1])
    assert box.load() == entry


def test_空箱读出空列表(tmp_path: Path) -> None:
    """文件还不存在是合法状态（一次失败都还没发生），读回空表而不是报错。"""
    box = DeadLetterBox(tmp_path / "还没有" / "deadletter.jsonl")
    assert box.load() == []


def test_追加自建父目录(tmp_path: Path) -> None:
    """路径的父目录不存在时要能自建——首次失败时没人替它建目录。"""
    box = DeadLetterBox(tmp_path / "深" / "更深" / "deadletter.jsonl")
    box.append(_deadletter(1)[0])
    assert box.load() == _deadletter(1)


def test_坏行跳过而非整份读不出(tmp_path: Path) -> None:
    """死信文件混入坏行时，好行照读——一行损坏不能把整份死信锁死。"""
    path = tmp_path / "deadletter.jsonl"
    _ = path.write_text(
        "\n".join(
            [
                _deadletter(1)[0].to_json(),
                "{这根本不是 JSON",
                "",
                _deadletter(2)[1].to_json(),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    box = DeadLetterBox(path)
    assert box.load() == _deadletter(2)


def test_坏行是合法JSON但缺字段也跳过(tmp_path: Path) -> None:
    """合法 JSON 但不符合死信条结构（缺字段）同样算坏行，不能炸掉整份读。"""
    path = tmp_path / "deadletter.jsonl"
    _ = path.write_text(
        "\n".join([_deadletter(1)[0].to_json(), json.dumps({"utterance_id": "u9"})]) + "\n",
        encoding="utf-8",
    )
    box = DeadLetterBox(path)
    assert box.load() == _deadletter(1)


def test_坏行是JSON数组也跳过(tmp_path: Path) -> None:
    """顶层不是对象（比如 JSON 数组）同样算坏行，不炸掉整份读。"""
    path = tmp_path / "deadletter.jsonl"
    _ = path.write_text(
        "\n".join(["[1, 2, 3]", _deadletter(1)[0].to_json()]) + "\n",
        encoding="utf-8",
    )
    box = DeadLetterBox(path)
    assert box.load() == _deadletter(1)


def test_坏行字段类型不符也跳过(tmp_path: Path) -> None:
    """字段存在但类型不符（utterance_id 是数字、attempts 是字符串）也算坏行。"""
    path = tmp_path / "deadletter.jsonl"
    bad_one = _mutate_field("utterance_id", 123)
    bad_two = _mutate_field("attempts", "三次")
    _ = path.write_text(
        "\n".join([bad_one, bad_two, _deadletter(1)[0].to_json()]) + "\n",
        encoding="utf-8",
    )
    box = DeadLetterBox(path)
    assert box.load() == _deadletter(1)


def test_坏行整数字段是布尔值也跳过(tmp_path: Path) -> None:
    """bool 是 int 的子类，但 attempts=True 不该被当成 1 混进好行。"""
    path = tmp_path / "deadletter.jsonl"
    bad = _mutate_field("failed_at", True)
    _ = path.write_text(
        "\n".join([bad, _deadletter(1)[0].to_json()]) + "\n",
        encoding="utf-8",
    )
    box = DeadLetterBox(path)
    assert box.load() == _deadletter(1)


def test_坏行载荷不是对象也跳过(tmp_path: Path) -> None:
    """payload 必须是对象或空；给个字符串同样是坏行。"""
    path = tmp_path / "deadletter.jsonl"
    bad = _mutate_field("payload", "这是字符串不是对象")
    _ = path.write_text(
        "\n".join([bad, _deadletter(1)[0].to_json()]) + "\n",
        encoding="utf-8",
    )
    box = DeadLetterBox(path)
    assert box.load() == _deadletter(1)


def test_重写是原子替换(tmp_path: Path) -> None:
    """rewrite 写临时文件再 replace：目标被整体换掉，不残留旧内容。"""
    box = DeadLetterBox(tmp_path / "deadletter.jsonl")
    box.append(_deadletter(3)[0])
    box.append(_deadletter(3)[1])
    box.append(_deadletter(3)[2])
    box.rewrite([_deadletter(1)[0]])
    remaining = box.load()
    assert remaining == _deadletter(1)
    # 临时文件必须已被替换走，目录里不留残渣
    directory = sorted(p.name for p in tmp_path.iterdir())
    assert directory == ["deadletter.jsonl"]


def test_重写成空清单清空文件(tmp_path: Path) -> None:
    """全部重放成功时 rewrite([]) 把文件清空，而不是留一份旧内容。"""
    box = DeadLetterBox(tmp_path / "deadletter.jsonl")
    box.append(_deadletter(2)[0])
    box.rewrite([])
    assert box.load() == []


def test_重写自建父目录(tmp_path: Path) -> None:
    """父目录不在时 rewrite 也要能自建，避免首次重放直接炸。"""
    box = DeadLetterBox(tmp_path / "新目录" / "deadletter.jsonl")
    box.rewrite(_deadletter(2))
    assert box.load() == _deadletter(2)


def test_路径可读(tmp_path: Path) -> None:
    """path 是公开只读：健康检查与日志要报死信落点。"""
    path = tmp_path / "deadletter.jsonl"
    assert DeadLetterBox(path).path == path


# ---------------------------------------------------------------------------
# 重试与死信的组合（调用方视角）
# ---------------------------------------------------------------------------


async def test_重试用尽进死信(tmp_path: Path) -> None:
    """死信条带 utterance_id / 阶段 / 尝试次数；文件是 append-only JSONL。

    run_with_retry 只上抛，进死信是这里（调用方）显式做的——职责边界可见。
    """
    policy = RetryPolicy(max_attempts=2, backoff=0.0)
    box = DeadLetterBox(tmp_path / "deadletter.jsonl")
    payload: dict[str, object] = {"kind": "dialogue", "text": "我要记住这句"}
    attempt_count = 0

    async def stage_a() -> None:
        nonlocal attempt_count
        attempt_count += 1
        raise EmbeddingTimeoutError("嵌入端点超时")

    try:
        _ = await run_with_retry(stage_a, policy=policy, sleep=_no_sleep)
    except EmbeddingTimeoutError as exc:
        box.append(
            DeadLetter(
                utterance_id="u-原文",
                stage=STAGE_UTTERANCE,
                error_type=type(exc).__name__,
                error_message=str(exc),
                attempts=attempt_count,
                payload=payload,
                failed_at=1735689600,
            )
        )

    read_back = box.load()
    assert len(read_back) == 1
    assert read_back[0].utterance_id == "u-原文"
    assert read_back[0].stage == STAGE_UTTERANCE
    assert read_back[0].attempts == 3  # 首次 + 2 次重试
    assert read_back[0].payload == payload  # 阶段 A 必须带完整载荷
    assert read_back[0].error_type == "EmbeddingTimeoutError"


async def test_永久故障零重试直接死信(tmp_path: Path) -> None:
    """LLMSchemaError 是永久故障：一次都不重试，直接进死信，且阶段 B 只记 id。"""
    policy = RetryPolicy(max_attempts=5, backoff=0.0)
    box = DeadLetterBox(tmp_path / "deadletter.jsonl")
    attempt_count = 0

    async def stage_b() -> None:
        nonlocal attempt_count
        attempt_count += 1
        raise LLMSchemaError("抽取结果不符合 schema")

    try:
        _ = await run_with_retry(stage_b, policy=policy, sleep=_no_sleep)
    except LLMSchemaError as exc:
        box.append(
            DeadLetter(
                utterance_id="u-已在库",
                stage=STAGE_STRUCTURED,
                error_type=type(exc).__name__,
                error_message=str(exc),
                attempts=attempt_count,
                payload=None,  # 阶段 B：原文已在库，重放时读库即可
                failed_at=1735689601,
            )
        )

    read_back = box.load()
    assert attempt_count == 1  # 零重试
    assert len(read_back) == 1
    assert read_back[0].attempts == 1
    assert read_back[0].stage == STAGE_STRUCTURED
    assert read_back[0].payload is None
    assert read_back[0].error_type == "LLMSchemaError"


async def test_死信可被重放剔除(tmp_path: Path) -> None:
    """重放成功的条目在重写后消失，失败的留下——DLQ 得自己收敛。"""
    box = DeadLetterBox(tmp_path / "deadletter.jsonl")
    for entry in _deadletter(3):
        box.append(entry)
    policy = RetryPolicy(max_attempts=1, backoff=0.0)

    async def replay(entry: DeadLetter) -> bool:
        async def operation() -> str:
            if entry.utterance_id == "u1":
                raise LLMTimeoutError("第 2 条还是不行")
            return "重放成功"

        try:
            _ = await run_with_retry(operation, policy=policy, sleep=_no_sleep)
        except LLMTimeoutError:
            return False
        return True

    survivors: list[DeadLetter] = []
    for entry in box.load():
        if not await replay(entry):
            survivors.append(entry)
    box.rewrite(survivors)

    leftover = box.load()
    assert [entry.utterance_id for entry in leftover] == ["u1"]
