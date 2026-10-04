"""记忆召回测试：语义 + 结构化 + 预算裁剪 + 访问回写。

约定沿用 `test_memory_service.py`：依赖全假（这里只需要一枚「定向」嵌入替身，
它把任何文本都映成同一枚预设向量，好把「距离」写死），Kùzu 用真库（`tmp_path` 下每例
一个库）。原文直接经会话写进库，绕过 worker——本文件测的是**读路径**。

测试名刻意不含英文断言关键词，否则会被日志文件内容的 `tmp_path` 命中而假红。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import pytest
from loguru import logger

from core.config import EmbeddingSettings, LogSettings, MemorySettings
from core.decision import DecisionService
from core.embedding import EmbeddingService, EncodedVector, EncodeResult, RemoteEncoder
from core.llm import LLMService
from core.logger import setup_logging
from core.memory import MemoryStoreError
from core.memory.service import MemoryService
from core.memory.store import escape_literal
from core.memory.store.writes import merge_character, record_state_change

#: 测试用的向量维度：小一点，断言里好写。
_DIM = 4


class _FixedKernel:
    """嵌入替身：任何文本都返回同一枚预设向量，便于把「距离」写死。"""

    def __init__(self, vector: Sequence[float]) -> None:
        self._vector: list[float] = list(vector)
        self.texts: list[list[str]] = []
        self.dimensions_args: list[int | None] = []

    async def encode(self, texts: Sequence[str], *, dimensions: int | None = None) -> EncodeResult:
        self.dimensions_args.append(dimensions)
        self.texts.append(list(texts))
        return EncodeResult(
            items=tuple(
                EncodedVector(index=index, vector=list(self._vector)) for index in range(len(texts))
            ),
            prompt_tokens=None,
        )

    async def aclose(self) -> None:
        return None


def _embedding_service(kernel: _FixedKernel) -> EmbeddingService:
    """造一个真 `EmbeddingService`，白盒塞进定向内核。"""
    service = EmbeddingService(EmbeddingSettings(api_key="k", model="embed-m"))
    service._encoder = cast("RemoteEncoder", cast("object", kernel))  # pyright: ignore[reportPrivateUsage]
    return service


def _config(tmp_path: Path) -> MemorySettings:
    """默认把库与死信都落在 tmp_path 下，避免用例之间串味。"""
    base: dict[str, object] = {
        "db_path": str(tmp_path / "db"),
        "dimensions": _DIM,
        "queue_size": 8,
        "retry": {
            "max_attempts": 1,
            "backoff": 0.01,
            "deadletter_path": str(tmp_path / "死信.jsonl"),
        },
    }
    return MemorySettings(**base)  # pyright: ignore[reportArgumentType]


def _service(tmp_path: Path, *, kernel: _FixedKernel | None = None) -> MemoryService:
    """造一个真 `MemoryService`，三个依赖都塞假货（本文件只真正用嵌入）。"""
    return MemoryService(
        _embedding_service(kernel or _FixedKernel([1.0, 0.0, 0.0, 0.0])),
        LLMService.__new__(LLMService),
        DecisionService.__new__(DecisionService),
        _config(tmp_path),
    )


def _log_settings(tmp_path: Path) -> LogSettings:
    """构造只关心落盘位置的日志配置：布局钉成行，断言写的是行内字段。"""
    return LogSettings(level="DEBUG", dir=str(tmp_path / "logs"), retention="1 day", layout="line")


async def _insert_utterance(
    service: MemoryService,
    *,
    uid: str,
    text: str,
    vector: Sequence[float],
    importance: float = 0.0,
    world_time_literal: int | None = None,
) -> None:
    """直接经会话写一条带向量的原文（绕过 worker，专供读路径用例）。"""
    session = service._session  # pyright: ignore[reportPrivateUsage]
    assert session is not None
    component = ", ".join(repr(float(value)) for value in vector)
    world = "null" if world_time_literal is None else str(world_time_literal)
    statement = (
        f"CREATE (n:Utterance {{id:'{escape_literal(uid)}', text:'{escape_literal(text)}', "
        f"importance:{importance}, access_count:0, last_access:null, "
        f"world_time:{world}, embedding:[{component}]}})"
    )
    _ = await session.run(statement)


# --------------------------------------------------------------------------- 语义


async def test_语义召回按向量排序(tmp_path: Path) -> None:
    """近的那条排前面；条目带来源 id，世界时间原样解析出来。"""
    service = _service(tmp_path, kernel=_FixedKernel([1.0, 0.0, 0.0, 0.0]))
    await service.start()
    try:
        await _insert_utterance(
            service, uid="远", text="远", vector=[0.0, 1.0, 0.0, 0.0], importance=0.5
        )
        await _insert_utterance(
            service,
            uid="近",
            text="近",
            vector=[1.0, 0.0, 0.0, 0.0],
            importance=1.0,
            world_time_literal=3,
        )

        context = await service.recall("问问")

        assert [entry.source_id for entry in context.items] == ["近", "远"]
        assert context.items[0].text == "近"
        assert context.items[0].kind == "utterance"
        assert context.items[0].world_time == 3
        assert any(step.source == "vector" for step in context.trace)
    finally:
        await service.stop()


async def test_空库召回返回空块(tmp_path: Path) -> None:
    """记忆为空是合法状态：空块、空条目、空依据，不报错。"""
    service = _service(tmp_path)
    await service.start()
    try:
        context = await service.recall("随便问问")

        assert context.block == ""
        assert context.items == []
        assert context.trace == []
    finally:
        await service.stop()


# --------------------------------------------------------------------------- 结构化


async def test_召回同时给状态与回忆(tmp_path: Path) -> None:
    """语义与结构化两路都在结果里：既召回原文，也带出 focus 的当前状态。"""
    service = _service(tmp_path, kernel=_FixedKernel([1.0, 0.0, 0.0, 0.0]))
    await service.start()
    try:
        session = service._session  # pyright: ignore[reportPrivateUsage]
        assert session is not None
        await merge_character(session, character_id="c1", name="阿黎", kind="person")
        await record_state_change(
            session, character_id="c1", key="心境", value="平静", t_from=1, source_utterance="u0"
        )
        await _insert_utterance(
            service, uid="u1", text="风起了", vector=[1.0, 0.0, 0.0, 0.0], importance=1.0
        )

        # focus 里带一个查无此人的角色：它只是查不到状态、静默落空，不报错
        context = await service.recall("风", focus=["c1", "查无此人"])

        assert context.block.splitlines()[0] == "【此时的状态】"
        assert "心境" in context.block
        assert "风起了" in context.block
        assert any(step.source == "structured" for step in context.trace)
    finally:
        await service.stop()


# --------------------------------------------------------------------------- 预算


async def test_超预算按关联度与重要度截断(tmp_path: Path) -> None:
    """预算只装得下一条时，留 `相关度 × 重要度` 更高的那条，而不是更近的那条。"""
    service = _service(tmp_path, kernel=_FixedKernel([1.0, 0.0, 0.0, 0.0]))
    await service.start()
    try:
        # 很近但不重要 → 乘积 0；较远但重要 → 乘积 0.5
        await _insert_utterance(
            service, uid="很近", text="近", vector=[1.0, 0.0, 0.0, 0.0], importance=0.0
        )
        await _insert_utterance(
            service, uid="重要", text="要", vector=[0.0, 1.0, 0.0, 0.0], importance=1.0
        )

        context = await service.recall("问", budget=1)

        assert [entry.source_id for entry in context.items] == ["重要"]
    finally:
        await service.stop()


# --------------------------------------------------------------------------- 边界


async def test_召回维度不符报存储错误(tmp_path: Path) -> None:
    """查询向量维度与建表维度不符时明确报错，不把坏向量喂给索引。"""
    service = _service(tmp_path, kernel=_FixedKernel([1.0, 0.0, 0.0, 0.0, 0.0, 0.0]))
    await service.start()
    try:
        with pytest.raises(MemoryStoreError) as captured:
            _ = await service.recall("问")

        assert captured.value.operation == "recall"
    finally:
        await service.stop()


async def test_未启动召回报错(tmp_path: Path) -> None:
    """召回也是请求路径：没启动就调用要明确报错，而不是空转成功。"""
    service = _service(tmp_path)

    with pytest.raises(MemoryStoreError) as captured:
        _ = await service.recall("问")

    assert captured.value.operation == "ingest"


# --------------------------------------------------------------------------- 访问回写


async def test_召回命中会回写访问计数(tmp_path: Path) -> None:
    """命中条目经 worker 回写：`access_count` 加一、`last_access` 记本次世界时间。"""
    service = _service(tmp_path, kernel=_FixedKernel([1.0, 0.0, 0.0, 0.0]))
    await service.start()
    try:
        await _insert_utterance(
            service, uid="u1", text="甲", vector=[1.0, 0.0, 0.0, 0.0], importance=1.0
        )

        _ = await service.recall("问", world_time=42)

        session = service._session  # pyright: ignore[reportPrivateUsage]
        assert session is not None
        rows: list[list[object]] = []
        for _ in range(500):
            rows = await session.run(
                "MATCH (u:Utterance {id:'u1'}) RETURN u.access_count, u.last_access"
            )
            if rows[0][0] == 1:
                break
            await asyncio.sleep(0.01)

        assert rows[0] == [1, 42]
    finally:
        await service.stop()


async def test_回写队列满记错误但不抛(tmp_path: Path) -> None:
    """回写入队失败（队列已关）只记 ERROR，召回结果照常返回。"""
    files = setup_logging(_log_settings(tmp_path))
    service = _service(tmp_path, kernel=_FixedKernel([1.0, 0.0, 0.0, 0.0]))
    await service.start()
    try:
        await _insert_utterance(
            service, uid="u1", text="甲", vector=[1.0, 0.0, 0.0, 0.0], importance=1.0
        )
        queue = service._queue  # pyright: ignore[reportPrivateUsage]
        assert queue is not None
        queue.close()  # 关闭后入队一律被拒：模拟回写入队失败

        context = await service.recall("问")
        logger.remove()

        assert [entry.source_id for entry in context.items] == ["u1"]
        assert "回写队列已满" in files.app.read_text(encoding="utf-8")
    finally:
        await service.stop()
