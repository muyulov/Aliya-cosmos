"""记忆服务测试：生命周期、写入口与失败补偿。

约定（沿用 embedding / llm 两层的做法）：

- **依赖全假**：假 embedder / 假 llm / 假 decision 白盒注入，不碰网络、不起 mock server。
  本期 `_llm` / `_decision` 还没有消费者，但它们是容器契约要求的依赖，照样要造齐——
  少了它们，构造器签名与 `dependencies` 就对不上账。
- **Kùzu 用真库**：`tmp_path` 下每例一个库，天然隔离；图与索引行为本层最该锁死，不假。
- **定时循环不靠 sleep**：重放由 `replay_deadletters()` 显式驱动，用例不必真等。

测试名刻意不含英文断言关键词（`pass` / `assert` / `is` / `not` / `in` 之类），
否则会被日志文件内容的 `tmp_path` 命中而假红。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import cast, get_type_hints, override

import pytest
from loguru import logger

from core.config import (
    EmbeddingSettings,
    LLMEndpointSettings,
    LLMSettings,
    LogSettings,
    MemorySettings,
    Settings,
)
from core.decision import DecisionService
from core.embedding import EmbeddingService, EncodedVector, EncodeResult, RemoteEncoder
from core.embedding.errors import EmbeddingConnectionError, EmbeddingTimeoutError
from core.llm import LLMService
from core.logger import setup_logging
from core.memory import MemoryInputError, MemoryStoreError, Observation
from core.memory.ingest.queue import PendingObservation
from core.memory.ingest.retry import STAGE_STRUCTURED, STAGE_UTTERANCE, DeadLetter, DeadLetterBox
from core.memory.service import MemoryService
from core.memory.store import UTTERANCE_COUNT, MemorySession, escape_literal
from core.service.base import ServiceState, not_running_detail
from core.service.manager import ServiceManager

#: 测试用的向量维度：小一点，断言里好写。
_DIM = 4


class _FakeKernel:
    """嵌入内核替身：记录入参，按预设返回向量或抛错。

    与 `tests/test_embedding_service.py` 的 `_FakeEncoder` 同构，但只实现本文件
    用到的部分：默认兑现声明的维度，`第一次就抛` 用来造「瞬时故障重试」的场景。
    """

    def __init__(
        self,
        *,
        dimension: int = _DIM,
        error: Exception | None = None,
        fail_times: int = 0,
        error_factory: type[Exception] | None = None,
    ) -> None:
        self.calls: list[dict[str, object]] = []
        self.texts: list[list[str]] = []
        self.dimension: int = dimension
        self.error: Exception | None = error
        self.fail_times: int = fail_times
        self.error_factory: type[Exception] | None = error_factory
        self.raised_count: int = 0

    async def encode(self, texts: Sequence[str], *, dimensions: int | None = None) -> EncodeResult:
        self.calls.append({"texts": list(texts), "dimensions": dimensions})
        self.texts.append(list(texts))
        if self.fail_times > 0 and self.raised_count < self.fail_times:
            self.raised_count += 1
            code = self.error_factory or EmbeddingTimeoutError
            raise code(f"第 {self.raised_count} 次失败")
        if self.error is not None:
            raise self.error
        size = self.dimension if dimensions is None else dimensions
        items = [
            EncodedVector(index=index, vector=[float(index)] * size) for index in range(len(texts))
        ]
        return EncodeResult(items=tuple(items), prompt_tokens=None)

    async def aclose(self) -> None:
        return None


def _embedding_service(kernel: _FakeKernel) -> EmbeddingService:
    """造一个真 `EmbeddingService`，白盒塞进假内核。

    内核替身与真内核类型不重叠，先转 object 再转目标类型，否则撞 reportInvalidCast。
    """
    service = EmbeddingService(EmbeddingSettings(api_key="k", model="embed-m"))
    service._encoder = cast("RemoteEncoder", cast("object", kernel))  # pyright: ignore[reportPrivateUsage]
    return service


def _memory_config(
    tmp_path: Path, *, retry: dict[str, object] | None = None, **overrides: object
) -> MemorySettings:
    """默认把库与死信都落在 tmp_path 下；`retry` 里的键与默认值合并，其余覆写透传。

    死信**必须**显式指向 tmp_path：默认路径是仓库里的 `data/memory/deadletter.jsonl`，
    多个用例共用它会让「死信条数」互相串味（上一例的残留被下一例读到）。
    """
    retry_settings: dict[str, object] = {
        "max_attempts": 3,
        "backoff": 0.01,
        "deadletter_path": str(tmp_path / "死信.jsonl"),
    }
    retry_settings.update(retry or {})
    base: dict[str, object] = {
        "db_path": str(tmp_path / "db"),
        "dimensions": _DIM,
        "queue_size": 4,
        "retry": retry_settings,
    }
    base.update(overrides)
    return MemorySettings(**base)  # pyright: ignore[reportArgumentType]


def _service(
    tmp_path: Path,
    *,
    kernel: _FakeKernel | None = None,
    config: MemorySettings | None = None,
) -> MemoryService:
    """造一个真 `MemoryService`，三个依赖都塞假货（本期只有嵌入被真正调用）。"""
    return MemoryService(
        _embedding_service(kernel or _FakeKernel()),
        LLMService.__new__(LLMService),
        DecisionService.__new__(DecisionService),
        config or _memory_config(tmp_path),
    )


async def _utterance_count(service: MemoryService) -> int:
    """直接问库：这是端到端断言，不经服务的任何包装（服务没暴露计数口）。"""
    session = service._session  # pyright: ignore[reportPrivateUsage]
    assert session is not None
    rows = await session.run(UTTERANCE_COUNT)
    return cast("int", rows[0][0])


async def _utterance_and_vector(
    service: MemoryService, text: str
) -> tuple[str, list[float]] | None:
    """按文本查一条原文的文本与向量；库里没有就返回 None。"""
    session = service._session  # pyright: ignore[reportPrivateUsage]
    assert session is not None
    statement = (
        f"MATCH (u:Utterance) WHERE u.text = '{escape_literal(text)}' RETURN u.text, u.embedding"
    )
    rows = await session.run(statement)
    if not rows:
        return None
    first = rows[0]
    return cast("str", first[0]), cast("list[float]", first[1])


async def _fetch_utterance_by_id(service: MemoryService, utterance_id: str) -> str | None:
    """按主键查一条原文的文本；库里没有就返回 None（契约用例的落库探针）。"""
    session = service._session  # pyright: ignore[reportPrivateUsage]
    assert session is not None
    statement = f"MATCH (u:Utterance) WHERE u.id = '{escape_literal(utterance_id)}' RETURN u.id"
    rows = await session.run(statement)
    if not rows:
        return None
    return cast("str", rows[0][0])


async def _wait_ingested(service: MemoryService, *, expected_count: int = 1) -> None:
    """轮询等 worker 把观测落进库。

    不用固定 sleep：worker 是单协程串行的，库写入完成后计数才变。
    给 5 秒上限，正常情况几十毫秒内就满足。
    """
    for _ in range(500):
        if await _utterance_count(service) >= expected_count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"等不到原文入库（期望 {expected_count} 条）")


def _deadletters(service: MemoryService) -> list[DeadLetter]:
    """读回当前死信清单；未启动时为空（白盒取箱，省得每处都写可选判断）。"""
    box = service._deadletters  # pyright: ignore[reportPrivateUsage]
    assert box is not None
    return box.load()


async def _wait_deadletter(service: MemoryService, *, expected_count: int = 1) -> None:
    """轮询等死信条落盘。"""
    for _ in range(500):
        if len(_deadletters(service)) >= expected_count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"等不到死信落盘（期望 {expected_count} 条）")


def _log_settings(tmp_path: Path) -> LogSettings:
    """构造只关心落盘位置的日志配置：布局钉成行，断言写的是行内字段。"""
    return LogSettings(level="DEBUG", dir=str(tmp_path / "logs"), retention="1 day", layout="line")


# ---- 契约与生命周期 ----


def test_构造器声明与依赖一致() -> None:
    """容器按注解注入，dependencies 与 __init__ 形参必须逐一对齐。"""
    hints = get_type_hints(MemoryService.__init__)

    assert set(MemoryService.dependencies) == {hints["embedding"], hints["llm"], hints["decision"]}


def test_服务名与依赖类型() -> None:
    """展示名固定为 memory；依赖就是嵌入、LLM、判断三支。"""
    assert MemoryService.name == "memory"
    # 依赖顺序即构造器形参顺序，容器按类型注入、不靠位置，但顺序仍须一一对齐
    assert MemoryService.dependencies == (EmbeddingService, LLMService, DecisionService)


async def test_start幂等(tmp_path: Path) -> None:
    """重复 start 不重建会话、不换 worker（前一个会被丢弃、没人收殓）。"""
    service = _service(tmp_path)
    await service.start()
    session = service._session  # pyright: ignore[reportPrivateUsage]
    worker = service._worker  # pyright: ignore[reportPrivateUsage]

    await service.start()

    # 幂等判据在「资源有没有换」：会话与 worker 都得是第一次那一个
    assert service._session is session  # pyright: ignore[reportPrivateUsage]
    assert service._worker is worker  # pyright: ignore[reportPrivateUsage]
    assert service._worker is not None  # pyright: ignore[reportPrivateUsage]
    await service.stop()


async def test_建表失败会关闭已开的会话(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """start() 半途失败（如库的 schema 版本不符）必须关掉已开的会话，不能泄漏。

    `ensure_schema` 抛错是设计支持的失败路径。本用例**直接调 start()**（不经容器回滚），
    验证实现自己兜住了关闭：否则已开的 Kùzu 句柄与单线程执行器会泄漏。
    证伪开关：把 `self._session = session` 挪回 `ensure_schema` 之后，会话不会被关。
    """
    service = _service(tmp_path)
    closed_flags: list[bool] = []
    original_close = MemorySession.close

    async def _record_close(instance: MemorySession) -> None:
        await original_close(instance)
        closed_flags.append(True)

    async def _schema_boom(_session: MemorySession) -> None:
        raise MemoryStoreError("库的 schema 版本不符", operation="ensure_schema")

    monkeypatch.setattr(MemorySession, "close", _record_close)
    monkeypatch.setattr("core.memory.service.ensure_schema", _schema_boom)

    with pytest.raises(MemoryStoreError):
        await service.start()

    assert closed_flags == [True]
    assert service._session is None  # pyright: ignore[reportPrivateUsage]


async def test_未启动时健康检查报未运行(tmp_path: Path) -> None:
    """还没 start：不健康，且详情指出是「未运行」而不是别的。"""
    service = _service(tmp_path)

    status = await service.health()

    assert status.healthy is False
    assert status.state is ServiceState.CREATED
    assert status.detail == not_running_detail(ServiceState.CREATED)
    assert status.extra["库路径"] == str(tmp_path / "db")
    assert status.extra["角色数"] == 0
    assert status.extra["原文数"] == 0
    assert status.extra["死信"] == 0
    assert status.extra["嵌入就绪"] is False


async def test_启动后健康检查与计数(tmp_path: Path) -> None:
    """启动后：会话已开、库可查；extra 里三类计数都对。"""
    service = _service(tmp_path)
    await service.start()
    try:
        status = await service.health()

        assert status.healthy is True
        assert status.detail == ""
        assert status.extra["库路径"] == str(tmp_path / "db")
        assert status.extra["角色数"] == 0
        assert status.extra["原文数"] == 0
        assert status.extra["死信"] == 0
        # 本用例的嵌入器注入了假内核，故依赖就绪为真（健康只看库，但 extra 会报依赖视角）
        assert status.extra["嵌入就绪"] is True
    finally:
        await service.stop()


async def test_依赖端点没配密钥时start只警告不炸(tmp_path: Path) -> None:
    """脚手架不该因为依赖没密钥就起不来：管理器照常把它拉到 RUNNING。"""
    settings = Settings(
        memory=_memory_config(tmp_path),
        embedding=EmbeddingSettings(api_key="", model="embed-m"),
        llm=LLMSettings(chat=LLMEndpointSettings(api_key="")),
    )
    manager = ServiceManager(settings)
    manager.register(EmbeddingService)
    manager.register(LLMService)
    manager.register(DecisionService)
    manager.register(MemoryService)

    await manager.start_all()
    try:
        service = manager.get(MemoryService)
        assert service.running is True
        status = await service.health()
        # 依赖没密钥不影响记忆服务自身健康：库是开着的
        assert status.healthy is True
        # 但依赖视角在 extra 里暴露出来：嵌入不可用 ⇒ 写入会全进死信（降级态可见）
        assert status.extra["嵌入就绪"] is False
    finally:
        await manager.stop_all()


async def test_停服务会取消worker(tmp_path: Path) -> None:
    """stop() 必须 cancel + await 收殓 worker，且取消**不被吞掉**。

    worker 必须**已经在循环体内**（阻塞在嵌入上）再取消：若它在启动前就取消，
    `cancelled()` 无论实现怎样都为真，用例区分不出「上抛」与「吞掉」。
    进入体内后，吞掉取消（`except CancelledError: return`）会让 `cancelled()`
    为假，上抛才是真——这条差别正是本用例的证伪开关。
    """
    gate = asyncio.Event()

    class _BlockingKernel(_FakeKernel):
        @override
        async def encode(
            self, texts: Sequence[str], *, dimensions: int | None = None
        ) -> EncodeResult:
            _ = await gate.wait()
            return await super().encode(texts, dimensions=dimensions)

    service = _service(tmp_path, kernel=_BlockingKernel())
    await service.start()
    worker = service._worker  # pyright: ignore[reportPrivateUsage]
    assert worker is not None
    _ = await service.observe(Observation(kind="dialogue", text="卡在嵌入里"))
    # 等 worker 进入循环体：深度归零说明它已经把这条取走、正卡在嵌入上
    for _ in range(500):
        if service._queue is not None and service._queue.depth == 0:  # pyright: ignore[reportPrivateUsage]
            break
        await asyncio.sleep(0.01)

    await service.stop()
    gate.set()

    assert worker.cancelled() is True
    assert service._worker is None  # pyright: ignore[reportPrivateUsage]
    assert service._session is None  # pyright: ignore[reportPrivateUsage]

    # 幂等：再停一次不炸
    await service.stop()


async def test_stop对从未启动的实例安全(tmp_path: Path) -> None:
    """回滚场景会这么调：start() 半途失败了，stop() 仍会被作用到。"""
    service = _service(tmp_path)

    await service.stop()

    assert service.running is False


# ---- 写入口：观测落原文 ----


async def test_观测落原文(tmp_path: Path) -> None:
    """observe 入队即返回一个 id；worker 消费后原文与向量都在库里。"""
    kernel = _FakeKernel()
    service = _service(tmp_path, kernel=kernel)
    await service.start()
    try:
        utterance_id = await service.observe(
            Observation(kind="dialogue", text="风起了", speaker="alice")
        )

        assert isinstance(utterance_id, str)
        assert utterance_id != ""

        await _wait_ingested(service)
        utterance = await _utterance_and_vector(service, "风起了")
        assert utterance is not None
        text, vector = utterance
        assert text == "风起了"
        assert len(vector) == _DIM
        assert kernel.texts[0] == ["风起了"]
    finally:
        await service.stop()


async def test_observe返回值就是落库主键(tmp_path: Path) -> None:
    """契约：`observe` 返回的 id **等于**库里那条 `Utterance` 的主键（设计文档 §6.1）。

    这是本任务要钉死的契约。**证伪开关**：若把 `_ingest` 改回「自己生成一个 id 落库」，
    `observe` 的返回值在库里就查不到——`_按id取原文` 返回 None，本条当场变红。
    """
    service = _service(tmp_path)
    await service.start()
    try:
        utterance_id = await service.observe(
            Observation(kind="dialogue", text="风起了", speaker="alice")
        )
        await _wait_ingested(service)

        # 用返回的 id 直接查库：必须命中，且取回的就是这个 id
        matched = await _fetch_utterance_by_id(service, utterance_id)
        assert matched == utterance_id
        # 反向确认：全库只有这一条，且它正是 observe 返回的那条（不是 worker 另造的）
        assert await _utterance_count(service) == 1
    finally:
        await service.stop()


async def test_死信载荷的utterance_id与observe返回值一致(tmp_path: Path) -> None:
    """契约：进死信的那条，其 `DeadLetter.utterance_id` **等于** `observe` 返回值。

    **证伪开关**：若 `_ingest` 在消费时另生成 id 写死信，死信里的 id 就与调用方
    拿到的对不上——本条当场变红。死信是失败兜底，主键对不齐就等于无法按 id 溯源。
    """
    config = _memory_config(
        tmp_path,
        retry={
            "max_attempts": 1,
            "backoff": 0.01,
            "deadletter_path": str(tmp_path / "死信.jsonl"),
        },
    )
    kernel = _FakeKernel(fail_times=99)  # 每次必抛 → 必进死信
    service = _service(tmp_path, kernel=kernel, config=config)
    await service.start()
    try:
        utterance_id = await service.observe(Observation(kind="dialogue", text="这句不能丢"))
        await _wait_deadletter(service)
        read_back = _deadletters(service)

        assert len(read_back) == 1
        assert read_back[0].utterance_id == utterance_id
        # 失败路径：库里没有这条（原文没落），死信是唯一副本，但主键与返回一致
        assert await _fetch_utterance_by_id(service, utterance_id) is None
    finally:
        await service.stop()


async def test_观测载荷非法立刻报错(tmp_path: Path) -> None:
    """类型对但语义不可用的载荷（全空白文本）在入队之前就被拦下。"""
    service = _service(tmp_path)
    await service.start()
    try:
        with pytest.raises(MemoryInputError):
            _ = await service.observe(Observation.model_construct(kind="scene", text="   "))

        # 队列里一条都没有：拦在了入队之前
        assert service._queue is not None  # pyright: ignore[reportPrivateUsage]
        assert service._queue.depth == 0  # pyright: ignore[reportPrivateUsage]
    finally:
        await service.stop()


async def test_未启动时观测报错(tmp_path: Path) -> None:
    """观察口是请求路径：没启动就调用要明确报错，而不是静默丢弃。"""
    service = _service(tmp_path)

    with pytest.raises(MemoryStoreError) as captured:
        _ = await service.observe(Observation(kind="dialogue", text="风起了"))

    assert captured.value.operation == "observe"


async def test_队列满丢弃并告警(tmp_path: Path) -> None:
    """队列满时丢弃本回合、记 ERROR，且**不给调用方抛错**（绝不拖慢对话）。"""
    cfg = _log_settings(tmp_path)
    files = setup_logging(cfg)
    config = _memory_config(tmp_path, queue_size=1)
    # 注入一个永不完成的内核：worker 取走第一条后卡在嵌入上，队列瞬间填满
    gate = asyncio.Event()

    class _BlockingKernel(_FakeKernel):
        @override
        async def encode(
            self, texts: Sequence[str], *, dimensions: int | None = None
        ) -> EncodeResult:
            _ = await gate.wait()
            return await super().encode(texts, dimensions=dimensions)

    service = _service(tmp_path, kernel=_BlockingKernel(), config=config)
    await service.start()
    try:
        first = await service.observe(Observation(kind="dialogue", text="第一条"))
        # 等 worker 把第一条取走，把队列腾空
        for _ in range(500):
            if service._queue is not None and service._queue.depth == 0:  # pyright: ignore[reportPrivateUsage]
                break
            await asyncio.sleep(0.01)

        two = await service.observe(Observation(kind="dialogue", text="第二条"))
        three = await service.observe(Observation(kind="dialogue", text="第三条"))  # 满：丢弃
        gate.set()
        logger.remove()

        assert isinstance(first, str)
        assert isinstance(two, str)
        assert isinstance(three, str)
        assert service._queue is not None  # pyright: ignore[reportPrivateUsage]
        assert service._queue.dropped >= 1  # pyright: ignore[reportPrivateUsage]
        text = files.app.read_text(encoding="utf-8")
        assert "[memory] 观测队列已满，丢弃本回合" in text
    finally:
        gate.set()
        await service.stop()


# ---- 失败补偿 ----


async def test_嵌入失败重试后成功(tmp_path: Path) -> None:
    """瞬时故障按策略重试：第二次才落库，日志里的尝试数为 2。"""
    cfg = _log_settings(tmp_path)
    files = setup_logging(cfg)
    kernel = _FakeKernel(fail_times=1)  # 第一次必抛，第二次通
    # backoff 取一个极小但非零的值：走真退避（不是 0 的短路），又不拖慢用例
    config = _memory_config(tmp_path, retry={"max_attempts": 3, "backoff": 0.01})
    service = _service(tmp_path, kernel=kernel, config=config)
    await service.start()
    try:
        _ = await service.observe(Observation(kind="dialogue", text="风起了"))
        await _wait_ingested(service)
        logger.remove()

        assert kernel.raised_count == 1
        text = files.app.read_text(encoding="utf-8")
        assert "[memory] 原文已入库" in text
        assert "尝试=2" in text
        assert service._deadletters is not None  # pyright: ignore[reportPrivateUsage]
        assert _deadletters(service) == []
    finally:
        await service.stop()


async def test_嵌入一直失败进死信(tmp_path: Path) -> None:
    """重试用尽：死信条带完整观测载荷，原文绝不因失败而消失。"""
    config = _memory_config(
        tmp_path,
        retry={
            "max_attempts": 2,
            "backoff": 0.01,
            "deadletter_path": str(tmp_path / "死信.jsonl"),
        },
    )
    kernel = _FakeKernel(fail_times=99)  # 每次必抛
    service = _service(tmp_path, kernel=kernel, config=config)
    await service.start()
    try:
        _ = await service.observe(
            Observation(kind="dialogue", text="这句不能丢", speaker="alice", world_time=7)
        )
        await _wait_deadletter(service)
        read_back = _deadletters(service)

        assert len(read_back) == 1
        entry = read_back[0]
        assert entry.stage == STAGE_UTTERANCE
        assert entry.error_type == "EmbeddingTimeoutError"
        assert entry.attempts == 3  # 首次 + 2 次重试
        assert entry.payload is not None
        assert entry.payload["text"] == "这句不能丢"
        assert entry.payload["kind"] == "dialogue"
        assert entry.payload["speaker"] == "alice"
        assert entry.payload["world_time"] == 7
        # 原文没进库（失败），载荷是唯一副本
        assert await _utterance_count(service) == 0
    finally:
        await service.stop()


async def test_不可重试的永久故障直接进死信(tmp_path: Path) -> None:
    """Kùzu / 校验类永久故障不重试：一次就死信。"""
    config = _memory_config(
        tmp_path,
        retry={
            "max_attempts": 5,
            "backoff": 0.01,
            "deadletter_path": str(tmp_path / "死信.jsonl"),
        },
    )
    # EmbeddingInputError 不在九类可重试里 → 零重试
    kernel = _FakeKernel(error=RuntimeError("不该重试的形态错误"))
    service = _service(tmp_path, kernel=kernel, config=config)
    await service.start()
    try:
        _ = await service.observe(Observation(kind="dialogue", text="永久坏"))
        await _wait_deadletter(service)
        read_back = _deadletters(service)

        assert len(read_back) == 1
        assert read_back[0].attempts == 1  # 零重试
        assert read_back[0].error_type == "RuntimeError"
    finally:
        await service.stop()


async def test_死信可重放剔除(tmp_path: Path) -> None:
    """重放成功的条目被剔除，失败的留下——死信得自己收敛。"""
    deadletter_path = tmp_path / "死信.jsonl"
    config = _memory_config(
        tmp_path,
        retry={"max_attempts": 1, "backoff": 0.01, "deadletter_path": str(deadletter_path)},
    )
    kernel = _FakeKernel(fail_times=99)
    service = _service(tmp_path, kernel=kernel, config=config)
    await service.start()
    try:
        utterance_id = await service.observe(Observation(kind="dialogue", text="先失败"))
        await _wait_deadletter(service)
        original_entry = _deadletters(service)
        assert len(original_entry) == 1
        assert original_entry[0].utterance_id == utterance_id

        # 内核转好：重放应成功并把死信剔除
        kernel.fail_times = 0
        kernel.raised_count = 0
        removed_count = await service.replay_deadletters()

        assert removed_count == 1
        assert _deadletters(service) == []
        assert await _utterance_count(service) == 1
        # 契约：重放落库的主键就是 observe 当初返回、也是死信条记下的那个 id。
        # 证伪开关：若 `_try_stage_a` 另造新 id，这里按原 id 查库会落空、本条变红。
        assert await _fetch_utterance_by_id(service, utterance_id) == utterance_id
    finally:
        await service.stop()


async def test_重放失败的条目留在死信(tmp_path: Path) -> None:
    """重放仍失败：条目不剔除、留在死信等下一轮（重放不必成功）。"""
    config = _memory_config(
        tmp_path,
        retry={
            "max_attempts": 1,
            "backoff": 0.01,
            "deadletter_path": str(tmp_path / "死信.jsonl"),
        },
    )
    kernel = _FakeKernel(fail_times=99)
    service = _service(tmp_path, kernel=kernel, config=config)
    await service.start()
    try:
        _ = await service.observe(Observation(kind="dialogue", text="一直坏"))
        await _wait_deadletter(service)

        removed_count = await service.replay_deadletters()

        assert removed_count == 0
        assert len(_deadletters(service)) == 1
    finally:
        await service.stop()


async def test_维度不符进死信(tmp_path: Path) -> None:
    """嵌入端点返回的维度与配置不符：抛存储错误，同样不丢原文。"""
    config = _memory_config(
        tmp_path,
        retry={
            "max_attempts": 1,
            "backoff": 0.01,
            "deadletter_path": str(tmp_path / "死信.jsonl"),
        },
    )
    kernel = _FakeKernel(dimension=8)  # 与 _维度=4 不符
    service = _service(tmp_path, kernel=kernel, config=config)
    await service.start()
    try:
        _ = await service.observe(Observation(kind="dialogue", text="维度不对"))
        await _wait_deadletter(service)
        read_back = _deadletters(service)

        assert len(read_back) == 1
        assert read_back[0].payload is not None
        assert read_back[0].payload["text"] == "维度不对"
    finally:
        await service.stop()


async def test_worker单条失败不中断循环(tmp_path: Path) -> None:
    """一条坏观测不该拖垮 worker：后面的照常处理。"""
    config = _memory_config(tmp_path, retry={"max_attempts": 1, "backoff": 0.01})
    kernel = _FakeKernel()
    service = _service(tmp_path, kernel=kernel, config=config)
    await service.start()
    try:
        # 第一条：内核抛不可重试的错 → 进死信
        kernel.error = RuntimeError("单条坏")
        _ = await service.observe(Observation(kind="dialogue", text="坏的那条"))
        await _wait_deadletter(service)
        # 第二条：内核恢复 → 应该照常入库
        kernel.error = None
        _ = await service.observe(Observation(kind="dialogue", text="好的那条"))

        await _wait_ingested(service)
        assert await _utterance_count(service) == 1
        assert len(_deadletters(service)) == 1
    finally:
        await service.stop()


async def test_嵌入连接错误也重试(tmp_path: Path) -> None:
    """连接类瞬时故障同样重试，不只超时。"""
    config = _memory_config(tmp_path, retry={"max_attempts": 2, "backoff": 0.01})
    kernel = _FakeKernel(fail_times=2, error_factory=EmbeddingConnectionError)
    service = _service(tmp_path, kernel=kernel, config=config)
    await service.start()
    try:
        _ = await service.observe(Observation(kind="dialogue", text="连了三次"))

        await _wait_ingested(service)
        assert kernel.raised_count == 2
        assert service._deadletters is not None  # pyright: ignore[reportPrivateUsage]
        assert _deadletters(service) == []
    finally:
        await service.stop()


# ---- 死信重放的边界 ----


async def test_重放跳过阶段与载荷缺失的死信(tmp_path: Path) -> None:
    """阶段不符或没载荷的死信原样保留：重放只认阶段 A、且必须带载荷。

    这不是「坏数据」——阶段 B 的死信按设计就没有载荷（原文已在库），
    重放器不该拿它当失败品删掉，也不该硬凑。
    """
    service = _service(tmp_path)
    box = service._deadletters  # pyright: ignore[reportPrivateUsage]
    assert box is None  # 未启动：还没有箱子
    service._deadletters = DeadLetterBox(tmp_path / "死信.jsonl")  # pyright: ignore[reportPrivateUsage]
    alpha = DeadLetter(
        utterance_id="u-阶段乙",
        stage=STAGE_STRUCTURED,
        error_type="LLMSchemaError",
        error_message="阶段 B 的失败",
        attempts=1,
        payload=None,
        failed_at=1,
    )
    beta = DeadLetter(
        utterance_id="u-阶段甲无载荷",
        stage=STAGE_UTTERANCE,
        error_type="MemoryStoreError",
        error_message="阶段 A 但没带载荷",
        attempts=1,
        payload=None,
        failed_at=2,
    )
    _ = (service._deadletters).append(alpha)  # pyright: ignore[reportPrivateUsage]
    _ = (service._deadletters).append(beta)  # pyright: ignore[reportPrivateUsage]

    removed = await service.replay_deadletters()

    assert removed == 0
    assert [entry.utterance_id for entry in _deadletters(service)] == ["u-阶段乙", "u-阶段甲无载荷"]


async def test_重放跳过载荷不合法的死信(tmp_path: Path) -> None:
    """载荷解不成 `Observation` 的死信原样保留，等人工处理，不炸整轮。"""
    service = _service(tmp_path)
    service._deadletters = DeadLetterBox(tmp_path / "死信.jsonl")  # pyright: ignore[reportPrivateUsage]
    bad_entry = DeadLetter(
        utterance_id="u-坏载荷",
        stage=STAGE_UTTERANCE,
        error_type="EmbeddingTimeoutError",
        error_message="载荷字段不对",
        attempts=1,
        payload={"kind": "不认识", "text": "还是空白"},
        failed_at=3,
    )
    _ = (service._deadletters).append(bad_entry)  # pyright: ignore[reportPrivateUsage]

    removed = await service.replay_deadletters()

    assert removed == 0
    assert [entry.utterance_id for entry in _deadletters(service)] == ["u-坏载荷"]


# ---- 未启动时的内部守卫 ----


async def test_未启动时重放报错(tmp_path: Path) -> None:
    """没启动就没有死信箱：重放要明确报错，而不是空转成功。"""
    service = _service(tmp_path)

    with pytest.raises(MemoryStoreError) as captured:
        _ = await service.replay_deadletters()

    assert captured.value.operation == "deadletter"


async def test_未启动时取会话报错(tmp_path: Path) -> None:
    """白盒：未启动时取会话要报存储错误（而非裸 AttributeError）。"""
    service = _service(tmp_path)

    with pytest.raises(MemoryStoreError) as captured:
        _ = service._require_session()  # pyright: ignore[reportPrivateUsage]

    assert captured.value.operation == "ingest"


async def test_未启动时取死信箱报错(tmp_path: Path) -> None:
    """白盒：未启动时取死信箱要报存储错误。"""
    service = _service(tmp_path)

    with pytest.raises(MemoryStoreError) as captured:
        _ = service._require_deadletters()  # pyright: ignore[reportPrivateUsage]

    assert captured.value.operation == "deadletter"


async def test_worker在队列被抽走时退出(tmp_path: Path) -> None:
    """白盒：worker 取到空队列引用时干净退出，不空转。

    `stop()` 会把 `_queue` 置空；万一 worker 在那一刻被调度到，它得能收住，
    而不是对 None 调 `get()` 崩掉。
    """
    service = _service(tmp_path)
    await service.start()
    try:
        service._queue = None  # pyright: ignore[reportPrivateUsage]
        # 直接跑一遍消费循环：取不到队列应立即返回
        await service._consume()  # pyright: ignore[reportPrivateUsage]
    finally:
        await service.stop()


async def test_worker吞掉处理异常继续跑(tmp_path: Path) -> None:
    """白盒：`_ingest` 意外上抛时 worker 只记日志、不中断循环。

    这是防御性分支——正常路径里 `_ingest` 自己把阶段 A 失败收进死信，不会上抛。
    但死信写盘本身可能失败（磁盘满），那时不能让整条流水线倒掉。
    """
    service = _service(tmp_path)
    await service.start()
    try:
        call_count = 0

        async def _boom(_pending: PendingObservation) -> bool:
            nonlocal call_count
            call_count += 1
            raise RuntimeError("处理时炸了")

        service._ingest = _boom  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
        queue = service._queue  # pyright: ignore[reportPrivateUsage]
        assert queue is not None
        _ = await queue.put(
            PendingObservation(
                utterance_id="u-会炸的一条",
                observation=Observation(kind="dialogue", text="会炸的一条"),
            )
        )
        # 等 worker 跑过这一条：异常被吞、循环还在（否则下面 stop 会卡在未消费的 get 上）
        for _ in range(500):
            if call_count >= 1:
                break
            await asyncio.sleep(0.01)

        assert call_count == 1
    finally:
        await service.stop()
