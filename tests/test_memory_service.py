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
_维度 = 4


class _假内核:
    """嵌入内核替身：记录入参，按预设返回向量或抛错。

    与 `tests/test_embedding_service.py` 的 `_FakeEncoder` 同构，但只实现本文件
    用到的部分：默认兑现声明的维度，`第一次就抛` 用来造「瞬时故障重试」的场景。
    """

    def __init__(
        self,
        *,
        dimension: int = _维度,
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
        self.已抛次数: int = 0

    async def encode(self, texts: Sequence[str], *, dimensions: int | None = None) -> EncodeResult:
        self.calls.append({"texts": list(texts), "dimensions": dimensions})
        self.texts.append(list(texts))
        if self.fail_times > 0 and self.已抛次数 < self.fail_times:
            self.已抛次数 += 1
            code = self.error_factory or EmbeddingTimeoutError
            raise code(f"第 {self.已抛次数} 次失败")
        if self.error is not None:
            raise self.error
        size = self.dimension if dimensions is None else dimensions
        items = [
            EncodedVector(index=index, vector=[float(index)] * size) for index in range(len(texts))
        ]
        return EncodeResult(items=tuple(items), prompt_tokens=None)

    async def aclose(self) -> None:
        return None


def _嵌入服务(内核: _假内核) -> EmbeddingService:
    """造一个真 `EmbeddingService`，白盒塞进假内核。

    内核替身与真内核类型不重叠，先转 object 再转目标类型，否则撞 reportInvalidCast。
    """
    服务 = EmbeddingService(EmbeddingSettings(api_key="k", model="embed-m"))
    服务._encoder = cast("RemoteEncoder", cast("object", 内核))  # pyright: ignore[reportPrivateUsage]
    return 服务


def _记忆配置(
    tmp_path: Path, *, retry: dict[str, object] | None = None, **覆写: object
) -> MemorySettings:
    """默认把库与死信都落在 tmp_path 下；`retry` 里的键与默认值合并，其余覆写透传。

    死信**必须**显式指向 tmp_path：默认路径是仓库里的 `data/memory/deadletter.jsonl`，
    多个用例共用它会让「死信条数」互相串味（上一例的残留被下一例读到）。
    """
    retry设置: dict[str, object] = {
        "max_attempts": 3,
        "backoff": 0.01,
        "deadletter_path": str(tmp_path / "死信.jsonl"),
    }
    retry设置.update(retry or {})
    基础: dict[str, object] = {
        "db_path": str(tmp_path / "db"),
        "dimensions": _维度,
        "queue_size": 4,
        "retry": retry设置,
    }
    基础.update(覆写)
    return MemorySettings(**基础)  # pyright: ignore[reportArgumentType]


def _服务(
    tmp_path: Path,
    *,
    内核: _假内核 | None = None,
    配置: MemorySettings | None = None,
) -> MemoryService:
    """造一个真 `MemoryService`，三个依赖都塞假货（本期只有嵌入被真正调用）。"""
    return MemoryService(
        _嵌入服务(内核 or _假内核()),
        LLMService.__new__(LLMService),
        DecisionService.__new__(DecisionService),
        配置 or _记忆配置(tmp_path),
    )


async def _原文条数(服务: MemoryService) -> int:
    """直接问库：这是端到端断言，不经服务的任何包装（服务没暴露计数口）。"""
    会话 = 服务._session  # pyright: ignore[reportPrivateUsage]
    assert 会话 is not None
    行 = await 会话.run(UTTERANCE_COUNT)
    return cast("int", 行[0][0])


async def _向量与文本(服务: MemoryService, 文本: str) -> tuple[str, list[float]] | None:
    """按文本查一条原文的文本与向量；库里没有就返回 None。"""
    会话 = 服务._session  # pyright: ignore[reportPrivateUsage]
    assert 会话 is not None
    语句 = f"MATCH (u:Utterance) WHERE u.text = '{escape_literal(文本)}' RETURN u.text, u.embedding"
    行 = await 会话.run(语句)
    if not 行:
        return None
    首 = 行[0]
    return cast("str", 首[0]), cast("list[float]", 首[1])


async def _按id取原文(服务: MemoryService, utterance_id: str) -> str | None:
    """按主键查一条原文的文本；库里没有就返回 None（契约用例的落库探针）。"""
    会话 = 服务._session  # pyright: ignore[reportPrivateUsage]
    assert 会话 is not None
    语句 = f"MATCH (u:Utterance) WHERE u.id = '{escape_literal(utterance_id)}' RETURN u.id"
    行 = await 会话.run(语句)
    if not 行:
        return None
    return cast("str", 行[0][0])


async def _等入库(服务: MemoryService, *, 期望条数: int = 1) -> None:
    """轮询等 worker 把观测落进库。

    不用固定 sleep：worker 是单协程串行的，库写入完成后计数才变。
    给 5 秒上限，正常情况几十毫秒内就满足。
    """
    for _ in range(500):
        if await _原文条数(服务) >= 期望条数:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"等不到原文入库（期望 {期望条数} 条）")


def _死信条(服务: MemoryService) -> list[DeadLetter]:
    """读回当前死信清单；未启动时为空（白盒取箱，省得每处都写可选判断）。"""
    箱 = 服务._deadletters  # pyright: ignore[reportPrivateUsage]
    assert 箱 is not None
    return 箱.load()


async def _等死信(服务: MemoryService, *, 期望条数: int = 1) -> None:
    """轮询等死信条落盘。"""
    for _ in range(500):
        if len(_死信条(服务)) >= 期望条数:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"等不到死信落盘（期望 {期望条数} 条）")


def _日志配置(tmp_path: Path) -> LogSettings:
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
    服务 = _服务(tmp_path)
    await 服务.start()
    会话 = 服务._session  # pyright: ignore[reportPrivateUsage]
    worker = 服务._worker  # pyright: ignore[reportPrivateUsage]

    await 服务.start()

    # 幂等判据在「资源有没有换」：会话与 worker 都得是第一次那一个
    assert 服务._session is 会话  # pyright: ignore[reportPrivateUsage]
    assert 服务._worker is worker  # pyright: ignore[reportPrivateUsage]
    assert 服务._worker is not None  # pyright: ignore[reportPrivateUsage]
    await 服务.stop()


async def test_建表失败会关闭已开的会话(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """start() 半途失败（如库的 schema 版本不符）必须关掉已开的会话，不能泄漏。

    `ensure_schema` 抛错是设计支持的失败路径。本用例**直接调 start()**（不经容器回滚），
    验证实现自己兜住了关闭：否则已开的 Kùzu 句柄与单线程执行器会泄漏。
    证伪开关：把 `self._session = session` 挪回 `ensure_schema` 之后，会话不会被关。
    """
    服务 = _服务(tmp_path)
    已关: list[bool] = []
    原关闭 = MemorySession.close

    async def _记录关闭(自身: MemorySession) -> None:
        await 原关闭(自身)
        已关.append(True)

    async def _建表炸(_会话: MemorySession) -> None:
        raise MemoryStoreError("库的 schema 版本不符", operation="ensure_schema")

    monkeypatch.setattr(MemorySession, "close", _记录关闭)
    monkeypatch.setattr("core.memory.service.ensure_schema", _建表炸)

    with pytest.raises(MemoryStoreError):
        await 服务.start()

    assert 已关 == [True]
    assert 服务._session is None  # pyright: ignore[reportPrivateUsage]


async def test_未启动时健康检查报未运行(tmp_path: Path) -> None:
    """还没 start：不健康，且详情指出是「未运行」而不是别的。"""
    服务 = _服务(tmp_path)

    状态 = await 服务.health()

    assert 状态.healthy is False
    assert 状态.state is ServiceState.CREATED
    assert 状态.detail == not_running_detail(ServiceState.CREATED)
    assert 状态.extra["库路径"] == str(tmp_path / "db")
    assert 状态.extra["角色数"] == 0
    assert 状态.extra["原文数"] == 0
    assert 状态.extra["死信"] == 0
    assert 状态.extra["嵌入就绪"] is False


async def test_启动后健康检查与计数(tmp_path: Path) -> None:
    """启动后：会话已开、库可查；extra 里三类计数都对。"""
    服务 = _服务(tmp_path)
    await 服务.start()
    try:
        状态 = await 服务.health()

        assert 状态.healthy is True
        assert 状态.detail == ""
        assert 状态.extra["库路径"] == str(tmp_path / "db")
        assert 状态.extra["角色数"] == 0
        assert 状态.extra["原文数"] == 0
        assert 状态.extra["死信"] == 0
        # 本用例的嵌入器注入了假内核，故依赖就绪为真（健康只看库，但 extra 会报依赖视角）
        assert 状态.extra["嵌入就绪"] is True
    finally:
        await 服务.stop()


async def test_依赖端点没配密钥时start只警告不炸(tmp_path: Path) -> None:
    """脚手架不该因为依赖没密钥就起不来：管理器照常把它拉到 RUNNING。"""
    基座 = Settings(
        memory=_记忆配置(tmp_path),
        embedding=EmbeddingSettings(api_key="", model="embed-m"),
        llm=LLMSettings(chat=LLMEndpointSettings(api_key="")),
    )
    管理器 = ServiceManager(基座)
    管理器.register(EmbeddingService)
    管理器.register(LLMService)
    管理器.register(DecisionService)
    管理器.register(MemoryService)

    await 管理器.start_all()
    try:
        服务 = 管理器.get(MemoryService)
        assert 服务.running is True
        状态 = await 服务.health()
        # 依赖没密钥不影响记忆服务自身健康：库是开着的
        assert 状态.healthy is True
        # 但依赖视角在 extra 里暴露出来：嵌入不可用 ⇒ 写入会全进死信（降级态可见）
        assert 状态.extra["嵌入就绪"] is False
    finally:
        await 管理器.stop_all()


async def test_停服务会取消worker(tmp_path: Path) -> None:
    """stop() 必须 cancel + await 收殓 worker，且取消**不被吞掉**。

    worker 必须**已经在循环体内**（阻塞在嵌入上）再取消：若它在启动前就取消，
    `cancelled()` 无论实现怎样都为真，用例区分不出「上抛」与「吞掉」。
    进入体内后，吞掉取消（`except CancelledError: return`）会让 `cancelled()`
    为假，上抛才是真——这条差别正是本用例的证伪开关。
    """
    闸门 = asyncio.Event()

    class _卡住内核(_假内核):
        @override
        async def encode(
            self, texts: Sequence[str], *, dimensions: int | None = None
        ) -> EncodeResult:
            _ = await 闸门.wait()
            return await super().encode(texts, dimensions=dimensions)

    服务 = _服务(tmp_path, 内核=_卡住内核())
    await 服务.start()
    worker = 服务._worker  # pyright: ignore[reportPrivateUsage]
    assert worker is not None
    _ = await 服务.observe(Observation(kind="dialogue", text="卡在嵌入里"))
    # 等 worker 进入循环体：深度归零说明它已经把这条取走、正卡在嵌入上
    for _ in range(500):
        if 服务._queue is not None and 服务._queue.depth == 0:  # pyright: ignore[reportPrivateUsage]
            break
        await asyncio.sleep(0.01)

    await 服务.stop()
    闸门.set()

    assert worker.cancelled() is True
    assert 服务._worker is None  # pyright: ignore[reportPrivateUsage]
    assert 服务._session is None  # pyright: ignore[reportPrivateUsage]

    # 幂等：再停一次不炸
    await 服务.stop()


async def test_stop对从未启动的实例安全(tmp_path: Path) -> None:
    """回滚场景会这么调：start() 半途失败了，stop() 仍会被作用到。"""
    服务 = _服务(tmp_path)

    await 服务.stop()

    assert 服务.running is False


# ---- 写入口：观测落原文 ----


async def test_观测落原文(tmp_path: Path) -> None:
    """observe 入队即返回一个 id；worker 消费后原文与向量都在库里。"""
    内核 = _假内核()
    服务 = _服务(tmp_path, 内核=内核)
    await 服务.start()
    try:
        回合 = await 服务.observe(Observation(kind="dialogue", text="风起了", speaker="alice"))

        assert isinstance(回合, str)
        assert 回合 != ""

        await _等入库(服务)
        原文 = await _向量与文本(服务, "风起了")
        assert 原文 is not None
        文本, 向量 = 原文
        assert 文本 == "风起了"
        assert len(向量) == _维度
        assert 内核.texts[0] == ["风起了"]
    finally:
        await 服务.stop()


async def test_observe返回值就是落库主键(tmp_path: Path) -> None:
    """契约：`observe` 返回的 id **等于**库里那条 `Utterance` 的主键（设计文档 §6.1）。

    这是本任务要钉死的契约。**证伪开关**：若把 `_ingest` 改回「自己生成一个 id 落库」，
    `observe` 的返回值在库里就查不到——`_按id取原文` 返回 None，本条当场变红。
    """
    服务 = _服务(tmp_path)
    await 服务.start()
    try:
        回合 = await 服务.observe(Observation(kind="dialogue", text="风起了", speaker="alice"))
        await _等入库(服务)

        # 用返回的 id 直接查库：必须命中，且取回的就是这个 id
        命中 = await _按id取原文(服务, 回合)
        assert 命中 == 回合
        # 反向确认：全库只有这一条，且它正是 observe 返回的那条（不是 worker 另造的）
        assert await _原文条数(服务) == 1
    finally:
        await 服务.stop()


async def test_死信载荷的utterance_id与observe返回值一致(tmp_path: Path) -> None:
    """契约：进死信的那条，其 `DeadLetter.utterance_id` **等于** `observe` 返回值。

    **证伪开关**：若 `_ingest` 在消费时另生成 id 写死信，死信里的 id 就与调用方
    拿到的对不上——本条当场变红。死信是失败兜底，主键对不齐就等于无法按 id 溯源。
    """
    配置 = _记忆配置(
        tmp_path,
        retry={
            "max_attempts": 1,
            "backoff": 0.01,
            "deadletter_path": str(tmp_path / "死信.jsonl"),
        },
    )
    内核 = _假内核(fail_times=99)  # 每次必抛 → 必进死信
    服务 = _服务(tmp_path, 内核=内核, 配置=配置)
    await 服务.start()
    try:
        回合 = await 服务.observe(Observation(kind="dialogue", text="这句不能丢"))
        await _等死信(服务)
        读回 = _死信条(服务)

        assert len(读回) == 1
        assert 读回[0].utterance_id == 回合
        # 失败路径：库里没有这条（原文没落），死信是唯一副本，但主键与返回一致
        assert await _按id取原文(服务, 回合) is None
    finally:
        await 服务.stop()


async def test_观测载荷非法立刻报错(tmp_path: Path) -> None:
    """类型对但语义不可用的载荷（全空白文本）在入队之前就被拦下。"""
    服务 = _服务(tmp_path)
    await 服务.start()
    try:
        with pytest.raises(MemoryInputError):
            _ = await 服务.observe(Observation.model_construct(kind="scene", text="   "))

        # 队列里一条都没有：拦在了入队之前
        assert 服务._queue is not None  # pyright: ignore[reportPrivateUsage]
        assert 服务._queue.depth == 0  # pyright: ignore[reportPrivateUsage]
    finally:
        await 服务.stop()


async def test_未启动时观测报错(tmp_path: Path) -> None:
    """观察口是请求路径：没启动就调用要明确报错，而不是静默丢弃。"""
    服务 = _服务(tmp_path)

    with pytest.raises(MemoryStoreError) as 捕获:
        _ = await 服务.observe(Observation(kind="dialogue", text="风起了"))

    assert 捕获.value.operation == "observe"


async def test_队列满丢弃并告警(tmp_path: Path) -> None:
    """队列满时丢弃本回合、记 ERROR，且**不给调用方抛错**（绝不拖慢对话）。"""
    cfg = _日志配置(tmp_path)
    files = setup_logging(cfg)
    配置 = _记忆配置(tmp_path, queue_size=1)
    # 注入一个永不完成的内核：worker 取走第一条后卡在嵌入上，队列瞬间填满
    闸门 = asyncio.Event()

    class _卡住内核(_假内核):
        @override
        async def encode(
            self, texts: Sequence[str], *, dimensions: int | None = None
        ) -> EncodeResult:
            _ = await 闸门.wait()
            return await super().encode(texts, dimensions=dimensions)

    服务 = _服务(tmp_path, 内核=_卡住内核(), 配置=配置)
    await 服务.start()
    try:
        首 = await 服务.observe(Observation(kind="dialogue", text="第一条"))
        # 等 worker 把第一条取走，把队列腾空
        for _ in range(500):
            if 服务._queue is not None and 服务._queue.depth == 0:  # pyright: ignore[reportPrivateUsage]
                break
            await asyncio.sleep(0.01)

        二 = await 服务.observe(Observation(kind="dialogue", text="第二条"))
        三 = await 服务.observe(Observation(kind="dialogue", text="第三条"))  # 满：丢弃
        闸门.set()
        logger.remove()

        assert isinstance(首, str)
        assert isinstance(二, str)
        assert isinstance(三, str)
        assert 服务._queue is not None  # pyright: ignore[reportPrivateUsage]
        assert 服务._queue.dropped >= 1  # pyright: ignore[reportPrivateUsage]
        文本 = files.app.read_text(encoding="utf-8")
        assert "[memory] 观测队列已满，丢弃本回合" in 文本
    finally:
        闸门.set()
        await 服务.stop()


# ---- 失败补偿 ----


async def test_嵌入失败重试后成功(tmp_path: Path) -> None:
    """瞬时故障按策略重试：第二次才落库，日志里的尝试数为 2。"""
    cfg = _日志配置(tmp_path)
    files = setup_logging(cfg)
    内核 = _假内核(fail_times=1)  # 第一次必抛，第二次通
    # backoff 取一个极小但非零的值：走真退避（不是 0 的短路），又不拖慢用例
    配置 = _记忆配置(tmp_path, retry={"max_attempts": 3, "backoff": 0.01})
    服务 = _服务(tmp_path, 内核=内核, 配置=配置)
    await 服务.start()
    try:
        _ = await 服务.observe(Observation(kind="dialogue", text="风起了"))
        await _等入库(服务)
        logger.remove()

        assert 内核.已抛次数 == 1
        文本 = files.app.read_text(encoding="utf-8")
        assert "[memory] 原文已入库" in 文本
        assert "尝试=2" in 文本
        assert 服务._deadletters is not None  # pyright: ignore[reportPrivateUsage]
        assert _死信条(服务) == []
    finally:
        await 服务.stop()


async def test_嵌入一直失败进死信(tmp_path: Path) -> None:
    """重试用尽：死信条带完整观测载荷，原文绝不因失败而消失。"""
    配置 = _记忆配置(
        tmp_path,
        retry={
            "max_attempts": 2,
            "backoff": 0.01,
            "deadletter_path": str(tmp_path / "死信.jsonl"),
        },
    )
    内核 = _假内核(fail_times=99)  # 每次必抛
    服务 = _服务(tmp_path, 内核=内核, 配置=配置)
    await 服务.start()
    try:
        _ = await 服务.observe(
            Observation(kind="dialogue", text="这句不能丢", speaker="alice", world_time=7)
        )
        await _等死信(服务)
        读回 = _死信条(服务)

        assert len(读回) == 1
        条 = 读回[0]
        assert 条.stage == STAGE_UTTERANCE
        assert 条.error_type == "EmbeddingTimeoutError"
        assert 条.attempts == 3  # 首次 + 2 次重试
        assert 条.payload is not None
        assert 条.payload["text"] == "这句不能丢"
        assert 条.payload["kind"] == "dialogue"
        assert 条.payload["speaker"] == "alice"
        assert 条.payload["world_time"] == 7
        # 原文没进库（失败），载荷是唯一副本
        assert await _原文条数(服务) == 0
    finally:
        await 服务.stop()


async def test_不可重试的永久故障直接进死信(tmp_path: Path) -> None:
    """Kùzu / 校验类永久故障不重试：一次就死信。"""
    配置 = _记忆配置(
        tmp_path,
        retry={
            "max_attempts": 5,
            "backoff": 0.01,
            "deadletter_path": str(tmp_path / "死信.jsonl"),
        },
    )
    # EmbeddingInputError 不在九类可重试里 → 零重试
    内核 = _假内核(error=RuntimeError("不该重试的形态错误"))
    服务 = _服务(tmp_path, 内核=内核, 配置=配置)
    await 服务.start()
    try:
        _ = await 服务.observe(Observation(kind="dialogue", text="永久坏"))
        await _等死信(服务)
        读回 = _死信条(服务)

        assert len(读回) == 1
        assert 读回[0].attempts == 1  # 零重试
        assert 读回[0].error_type == "RuntimeError"
    finally:
        await 服务.stop()


async def test_死信可重放剔除(tmp_path: Path) -> None:
    """重放成功的条目被剔除，失败的留下——死信得自己收敛。"""
    死信路径 = tmp_path / "死信.jsonl"
    配置 = _记忆配置(
        tmp_path,
        retry={"max_attempts": 1, "backoff": 0.01, "deadletter_path": str(死信路径)},
    )
    内核 = _假内核(fail_times=99)
    服务 = _服务(tmp_path, 内核=内核, 配置=配置)
    await 服务.start()
    try:
        回合 = await 服务.observe(Observation(kind="dialogue", text="先失败"))
        await _等死信(服务)
        原条 = _死信条(服务)
        assert len(原条) == 1
        assert 原条[0].utterance_id == 回合

        # 内核转好：重放应成功并把死信剔除
        内核.fail_times = 0
        内核.已抛次数 = 0
        剔除数 = await 服务.replay_deadletters()

        assert 剔除数 == 1
        assert _死信条(服务) == []
        assert await _原文条数(服务) == 1
        # 契约：重放落库的主键就是 observe 当初返回、也是死信条记下的那个 id。
        # 证伪开关：若 `_try_stage_a` 另造新 id，这里按原 id 查库会落空、本条变红。
        assert await _按id取原文(服务, 回合) == 回合
    finally:
        await 服务.stop()


async def test_重放失败的条目留在死信(tmp_path: Path) -> None:
    """重放仍失败：条目不剔除、留在死信等下一轮（重放不必成功）。"""
    配置 = _记忆配置(
        tmp_path,
        retry={
            "max_attempts": 1,
            "backoff": 0.01,
            "deadletter_path": str(tmp_path / "死信.jsonl"),
        },
    )
    内核 = _假内核(fail_times=99)
    服务 = _服务(tmp_path, 内核=内核, 配置=配置)
    await 服务.start()
    try:
        _ = await 服务.observe(Observation(kind="dialogue", text="一直坏"))
        await _等死信(服务)

        剔除数 = await 服务.replay_deadletters()

        assert 剔除数 == 0
        assert len(_死信条(服务)) == 1
    finally:
        await 服务.stop()


async def test_维度不符进死信(tmp_path: Path) -> None:
    """嵌入端点返回的维度与配置不符：抛存储错误，同样不丢原文。"""
    配置 = _记忆配置(
        tmp_path,
        retry={
            "max_attempts": 1,
            "backoff": 0.01,
            "deadletter_path": str(tmp_path / "死信.jsonl"),
        },
    )
    内核 = _假内核(dimension=8)  # 与 _维度=4 不符
    服务 = _服务(tmp_path, 内核=内核, 配置=配置)
    await 服务.start()
    try:
        _ = await 服务.observe(Observation(kind="dialogue", text="维度不对"))
        await _等死信(服务)
        读回 = _死信条(服务)

        assert len(读回) == 1
        assert 读回[0].payload is not None
        assert 读回[0].payload["text"] == "维度不对"
    finally:
        await 服务.stop()


async def test_worker单条失败不中断循环(tmp_path: Path) -> None:
    """一条坏观测不该拖垮 worker：后面的照常处理。"""
    配置 = _记忆配置(tmp_path, retry={"max_attempts": 1, "backoff": 0.01})
    内核 = _假内核()
    服务 = _服务(tmp_path, 内核=内核, 配置=配置)
    await 服务.start()
    try:
        # 第一条：内核抛不可重试的错 → 进死信
        内核.error = RuntimeError("单条坏")
        _ = await 服务.observe(Observation(kind="dialogue", text="坏的那条"))
        await _等死信(服务)
        # 第二条：内核恢复 → 应该照常入库
        内核.error = None
        _ = await 服务.observe(Observation(kind="dialogue", text="好的那条"))

        await _等入库(服务)
        assert await _原文条数(服务) == 1
        assert len(_死信条(服务)) == 1
    finally:
        await 服务.stop()


async def test_嵌入连接错误也重试(tmp_path: Path) -> None:
    """连接类瞬时故障同样重试，不只超时。"""
    配置 = _记忆配置(tmp_path, retry={"max_attempts": 2, "backoff": 0.01})
    内核 = _假内核(fail_times=2, error_factory=EmbeddingConnectionError)
    服务 = _服务(tmp_path, 内核=内核, 配置=配置)
    await 服务.start()
    try:
        _ = await 服务.observe(Observation(kind="dialogue", text="连了三次"))

        await _等入库(服务)
        assert 内核.已抛次数 == 2
        assert 服务._deadletters is not None  # pyright: ignore[reportPrivateUsage]
        assert _死信条(服务) == []
    finally:
        await 服务.stop()


# ---- 死信重放的边界 ----


async def test_重放跳过阶段与载荷缺失的死信(tmp_path: Path) -> None:
    """阶段不符或没载荷的死信原样保留：重放只认阶段 A、且必须带载荷。

    这不是「坏数据」——阶段 B 的死信按设计就没有载荷（原文已在库），
    重放器不该拿它当失败品删掉，也不该硬凑。
    """
    服务 = _服务(tmp_path)
    箱 = 服务._deadletters  # pyright: ignore[reportPrivateUsage]
    assert 箱 is None  # 未启动：还没有箱子
    服务._deadletters = DeadLetterBox(tmp_path / "死信.jsonl")  # pyright: ignore[reportPrivateUsage]
    甲 = DeadLetter(
        utterance_id="u-阶段乙",
        stage=STAGE_STRUCTURED,
        error_type="LLMSchemaError",
        error_message="阶段 B 的失败",
        attempts=1,
        payload=None,
        failed_at=1,
    )
    乙 = DeadLetter(
        utterance_id="u-阶段甲无载荷",
        stage=STAGE_UTTERANCE,
        error_type="MemoryStoreError",
        error_message="阶段 A 但没带载荷",
        attempts=1,
        payload=None,
        failed_at=2,
    )
    _ = (服务._deadletters).append(甲)  # pyright: ignore[reportPrivateUsage]
    _ = (服务._deadletters).append(乙)  # pyright: ignore[reportPrivateUsage]

    剔除 = await 服务.replay_deadletters()

    assert 剔除 == 0
    assert [条.utterance_id for 条 in _死信条(服务)] == ["u-阶段乙", "u-阶段甲无载荷"]


async def test_重放跳过载荷不合法的死信(tmp_path: Path) -> None:
    """载荷解不成 `Observation` 的死信原样保留，等人工处理，不炸整轮。"""
    服务 = _服务(tmp_path)
    服务._deadletters = DeadLetterBox(tmp_path / "死信.jsonl")  # pyright: ignore[reportPrivateUsage]
    坏条 = DeadLetter(
        utterance_id="u-坏载荷",
        stage=STAGE_UTTERANCE,
        error_type="EmbeddingTimeoutError",
        error_message="载荷字段不对",
        attempts=1,
        payload={"kind": "不认识", "text": "还是空白"},
        failed_at=3,
    )
    _ = (服务._deadletters).append(坏条)  # pyright: ignore[reportPrivateUsage]

    剔除 = await 服务.replay_deadletters()

    assert 剔除 == 0
    assert [条.utterance_id for 条 in _死信条(服务)] == ["u-坏载荷"]


# ---- 未启动时的内部守卫 ----


async def test_未启动时重放报错(tmp_path: Path) -> None:
    """没启动就没有死信箱：重放要明确报错，而不是空转成功。"""
    服务 = _服务(tmp_path)

    with pytest.raises(MemoryStoreError) as 捕获:
        _ = await 服务.replay_deadletters()

    assert 捕获.value.operation == "deadletter"


async def test_未启动时取会话报错(tmp_path: Path) -> None:
    """白盒：未启动时取会话要报存储错误（而非裸 AttributeError）。"""
    服务 = _服务(tmp_path)

    with pytest.raises(MemoryStoreError) as 捕获:
        _ = 服务._require_session()  # pyright: ignore[reportPrivateUsage]

    assert 捕获.value.operation == "ingest"


async def test_未启动时取死信箱报错(tmp_path: Path) -> None:
    """白盒：未启动时取死信箱要报存储错误。"""
    服务 = _服务(tmp_path)

    with pytest.raises(MemoryStoreError) as 捕获:
        _ = 服务._require_deadletters()  # pyright: ignore[reportPrivateUsage]

    assert 捕获.value.operation == "deadletter"


async def test_worker在队列被抽走时退出(tmp_path: Path) -> None:
    """白盒：worker 取到空队列引用时干净退出，不空转。

    `stop()` 会把 `_queue` 置空；万一 worker 在那一刻被调度到，它得能收住，
    而不是对 None 调 `get()` 崩掉。
    """
    服务 = _服务(tmp_path)
    await 服务.start()
    try:
        服务._queue = None  # pyright: ignore[reportPrivateUsage]
        # 直接跑一遍消费循环：取不到队列应立即返回
        await 服务._consume()  # pyright: ignore[reportPrivateUsage]
    finally:
        await 服务.stop()


async def test_worker吞掉处理异常继续跑(tmp_path: Path) -> None:
    """白盒：`_ingest` 意外上抛时 worker 只记日志、不中断循环。

    这是防御性分支——正常路径里 `_ingest` 自己把阶段 A 失败收进死信，不会上抛。
    但死信写盘本身可能失败（磁盘满），那时不能让整条流水线倒掉。
    """
    服务 = _服务(tmp_path)
    await 服务.start()
    try:
        调用次数 = 0

        async def _炸(_待处理: PendingObservation) -> bool:
            nonlocal 调用次数
            调用次数 += 1
            raise RuntimeError("处理时炸了")

        服务._ingest = _炸  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
        队列 = 服务._queue  # pyright: ignore[reportPrivateUsage]
        assert 队列 is not None
        _ = await 队列.put(
            PendingObservation(
                utterance_id="u-会炸的一条",
                observation=Observation(kind="dialogue", text="会炸的一条"),
            )
        )
        # 等 worker 跑过这一条：异常被吞、循环还在（否则下面 stop 会卡在未消费的 get 上）
        for _ in range(500):
            if 调用次数 >= 1:
                break
            await asyncio.sleep(0.01)

        assert 调用次数 == 1
    finally:
        await 服务.stop()
