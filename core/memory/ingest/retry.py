"""重试策略与死信队列：记忆层的失败补偿。

**为什么过载走丢弃、故障才走死信**（这段与设计文档 §6.5 末段同源，动之前请读完）：

对话比记忆快是**正常现象**——说话即时，抽取与落库要过 LLM、要写图库。把队列溢出
塞进死信队列，会让 DLQ 无限膨胀，而且重放永远追不上新来的对话，等于把「忙」错当
成「坏」。所以分界是：

- **过载 ⇒ 丢队列里还没结构化的回合 + 记 ERROR**（背压策略，见 `queue.py`）；
- **故障 ⇒ 进死信队列**（瞬时故障退避重试，永久故障或重试用尽才落 DLQ，即本模块）。

两者不是一回事：前者是「太多」，后者是「坏了」。

**为什么死信是独立 JSONL、刻意不放进 Kùzu**（设计文档决策 17）：

死信要覆盖**阶段 A（原文落库）**的失败，而阶段 A 失败恰恰意味着库可能正写不进去
（打不开、磁盘满、schema 版本不符）。此时若 DLQ 也存在 Kùzu 里，它会跟着一起挂，
失败就真正无声消失了。因此 DLQ 落成 append-only 的 JSONL 文件，与库解耦：
库写不进去时，DLQ 依然写得进。代价是它没有索引与事务，只做「追加—读回—整体重写」。

**本模块不写死信**：`run_with_retry` 只负责重试循环并把最后一次异常上抛，
**进死信是调用方的事**（`MemoryService`）。这样重试与死信可各自独立测试，
调用方也能按阶段（A 带载荷 / B 只记 id）决定死信条怎么写。

**本模块不打日志**：纯逻辑与纯数据。记日志（含上下文与健康指标）是
`MemoryService` 编排时的职责，两者不重叠。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from core.decision.errors import (
    DecisionConnectionError,
    DecisionRequestError,
    DecisionTimeoutError,
)
from core.embedding.errors import (
    EmbeddingConnectionError,
    EmbeddingRequestError,
    EmbeddingTimeoutError,
)
from core.llm.errors import LLMConnectionError, LLMRequestError, LLMTimeoutError

#: 阶段 A：原文落库。失败时死信要带**完整载荷**——原文必须最终进库，
#: 只记 id 的话重放时无从取原文（库里根本没有它）。
STAGE_UTTERANCE = "原文落库"

#: 阶段 B：结构化（抽取 / 判定 / 落库）。原文已在库里，死信只需 `utterance_id`
#: 与阶段；重放时从库里读原文即可，不重复占用队列载荷。
STAGE_STRUCTURED = "结构化"

#: 瞬时故障的判定依据：下层三支的**超时 / 连接 / 请求错误**。
#:
#: 为何只认这三类：它们是「这趟没走通、下趟可能通」的故障形态（超时、断连、
#: 429 与 5xx）。其余错误重试无意义——
#: - schema 不符（`LLMSchemaError`）：模型输出形态不对，再问几次仍是这个形态；
#: - 输入非法（`MemoryInputError` / `EmbeddingInputError`）：在本地就拦下的错；
#: - 库错（`MemoryStoreError`）：打不开、事务失败，重试只会拖着不落死信；
#: - 返回体与请求对不上（`EmbeddingResponseError`）/ 缺内容（`LLMResponseError`）：
#:   同样是形态问题，不是通道问题。
#:
#: 刻意**不用基类**（如 `LLMError`）笼统判定：基类会把 schema 错一起放进来，
#: 那就把所有永久故障都误判成可重试了。
_RETRYABLE: tuple[type[BaseException], ...] = (
    EmbeddingTimeoutError,
    EmbeddingConnectionError,
    EmbeddingRequestError,
    LLMTimeoutError,
    LLMConnectionError,
    LLMRequestError,
    DecisionTimeoutError,
    DecisionConnectionError,
    DecisionRequestError,
)


def is_retryable(exc: BaseException) -> bool:
    """该异常是否属于瞬时故障（重试有意义）。

    **只承认本层已知的九类**：非本层列的异常（`ValueError`、Kùzu 裸抛的
    `RuntimeError`……）一律为假——不确定性不该被擅自当成可重试，
    让它们走死信由人来看，比无限重试安全。
    """
    return isinstance(exc, _RETRYABLE)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """重试策略：次数上限与指数退避基数。

    `max_attempts` 指**重试次数**，首次尝试不计入：总尝试次数 = `max_attempts + 1`。
    `backoff` 是退避基数（秒），按 `backoff * 2**(attempt - 1)` 递增；
    配置里可设为 0（本仓 `MemoryRetrySettings.backoff` 的 `gt=0` 是给真实配置的，
    但策略对象本身不校验，测试与某些环境需要「零等待」）。
    """

    max_attempts: int
    backoff: float

    def delay(self, attempt: int) -> float:
        """第 `attempt` 次重试前的等待秒数（`backoff * 2**(attempt - 1)`）。

        `attempt` 从 1 起：第 1 次重试等基数，之后逐次翻倍。
        """
        # 底数写作 2.0 而非 2：`int ** int` 的类型是 Any（结果可能是复数），
        # 用浮点底数把类型收敛成 float，省掉一次 cast。
        return self.backoff * 2.0 ** (attempt - 1)


@dataclass(frozen=True, slots=True)
class DeadLetter:
    """一条死信记录：一次用尽重试或不可重试的失败。

    `stage` 是 `STAGE_UTTERANCE` / `STAGE_STRUCTURED`。`payload` 按阶段分流：
    阶段 A 存**完整观测载荷**（原文还没进库，这是唯一的副本），
    阶段 B 为 `None`（原文已在库，重放时读库即可）。
    `failed_at` 是失败时刻（epoch 秒），留给重放与对账排序。
    """

    utterance_id: str
    stage: str
    error_type: str
    error_message: str
    attempts: int
    payload: dict[str, object] | None
    failed_at: int

    def to_json(self) -> str:
        """序列化成一行 JSON。

        `ensure_ascii=False`：死信要能被人直接打开看，中文别转成 `\\uXXXX`。
        """
        return json.dumps(
            {
                "utterance_id": self.utterance_id,
                "stage": self.stage,
                "error_type": self.error_type,
                "error_message": self.error_message,
                "attempts": self.attempts,
                "payload": self.payload,
                "failed_at": self.failed_at,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, text: str) -> DeadLetter:
        """从一行 JSON 还原。

        字段缺失或类型不符时由调用方（`DeadLetterBox.load`）按坏行处理：
        遇到不合形状的数据就抛 `TypeError` / `ValueError` / `KeyError`，
        **绝不静默补默认值**——那会让坏行混进好行里。刻意用 `isinstance` 显式校验
        而不是 `cast` 强转：`cast` 只是对类型检查器的谎言，坏行照样能构造出半成品条。
        """
        decoded = cast("object", json.loads(text))  # json 返回 Any，先 cast 成 object
        if not isinstance(decoded, dict):
            raise TypeError("死信行必须是 JSON 对象")
        mapping = cast("dict[str, object]", decoded)
        payload = mapping["payload"]
        if payload is not None and not isinstance(payload, dict):
            raise TypeError("死信条的 payload 必须是对象或空")
        return cls(
            utterance_id=_require_str(mapping["utterance_id"]),
            stage=_require_str(mapping["stage"]),
            error_type=_require_str(mapping["error_type"]),
            error_message=_require_str(mapping["error_message"]),
            attempts=_require_int(mapping["attempts"]),
            payload=cast("dict[str, object] | None", payload),
            failed_at=_require_int(mapping["failed_at"]),
        )


def _require_str(value: object) -> str:
    """从 JSON 解出的值里取字符串，类型不符就抛错（坏行交给调用方跳过）。"""
    if not isinstance(value, str):
        raise TypeError("死信条的该字段必须是字符串")
    return value


def _require_int(value: object) -> int:
    """取整数。`bool` 是 `int` 的子类，要挡在门外，免得 `True` 混成 1。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("死信条的该字段必须是整数")
    return value


class DeadLetterBox:
    """append-only JSONL 死信箱：与 Kùzu 解耦的失败兜底（设计文档决策 17）。

    三个动作对应重放循环的三步：`append`（失败时追加一条）、`load`（每轮扫一遍）、
    `rewrite`（重放完把存活条目整体换回去）。刻意不放进 Kùzu——阶段 A 失败恰恰
    说明库可能正写不进去，那时 DLQ 必须还能写。
    """

    def __init__(self, path: Path) -> None:
        self._path: Path = path

    @property
    def path(self) -> Path:
        """死信文件路径（健康检查与日志要报落点）。"""
        return self._path

    def append(self, entry: DeadLetter) -> None:
        """追加一条死信，一行 JSON。父目录不存在就自建。

        `"a"` 模式 + 每次一行：append-only，不需要读回整份再写出去，
        因此**单条失败不会丢掉已有记录**（这正是「不能把 DLQ 存 Kùzu」的同一考量）。
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            _ = handle.write(entry.to_json() + "\n")

    def load(self) -> list[DeadLetter]:
        """读回全部死信，**逐行解析、坏行跳过**。

        文件不存在（一次失败都还没发生）→ 空表，是合法状态，不报错。
        坏行（不是合法 JSON，或 JSON 合法但缺字段 / 类型不符、空行）→ 跳过该行，
        继续读后面的：DLQ 是最后的兜底，**不能因为一行损坏就整份读不出来**，
        否则后续重放全部失效，失败就真无声消失了。
        """
        if not self._path.exists():
            return []
        entries: list[DeadLetter] = []
        with self._path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(DeadLetter.from_json(line))
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    continue  # 坏行：跳过，读后面的
        return entries

    def rewrite(self, entries: list[DeadLetter]) -> None:
        """用新清单整体替换死信文件（**原子替换**）。

        先写同目录下的临时文件，再 `Path.replace`：`replace` 是同文件系统上的
        原子改名，读者要么看到旧整份、要么看到新整份，**不会读到写了一半的中间态**。
        若直接原地截断重写，进程在写到一半时崩掉会留下一份半残的死信，好行也一起丢。
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._path.with_name(self._path.name + ".tmp")
        with temp.open("w", encoding="utf-8") as handle:
            for entry in entries:
                _ = handle.write(entry.to_json() + "\n")
        _ = temp.replace(self._path)


async def run_with_retry[V](
    operation: Callable[[], Awaitable[V]],
    *,
    policy: RetryPolicy,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> tuple[V, int]:
    """跑一个异步操作，瞬时故障按 `policy` 退避重试。

    返回 `(结果, 尝试次数)`；尝试次数含首次，故成功即 `>= 1`。

    语义：

    - 成功 → 立刻返回，不等待；
    - 抛**不可重试**异常（见 `is_retryable`）→ **立刻原样上抛**，一次都不重试；
    - 抛可重试异常且重试用尽 → 上抛**最后一次**的异常（`raise ... from 上次` 保留
      `__cause__` 链，调用方顺着 chain 能摸到最早那次原始报错）。

    `sleep` 必须可注入：真实退避是秒级等待，测试里传零耗时假函数，
    用例不必真等（也避免时序不稳）。

    **本函数不写死信**：它只把最后一次异常交给调用方，进死信由调用方决定
    （这样重试循环与死信落盘可各自独立测试）。
    """
    attempt = 0
    previous: BaseException | None = None
    while True:
        attempt += 1
        try:
            return await operation(), attempt
        except BaseException as exc:  # 先分类，不可重试的原样上抛
            if not is_retryable(exc):
                raise
            if attempt > policy.max_attempts:
                if previous is not None:
                    raise exc from previous
                raise
            previous = exc
            await sleep(policy.delay(attempt))
