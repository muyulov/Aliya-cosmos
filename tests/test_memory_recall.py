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
_维度 = 4


class _定向内核:
    """嵌入替身：任何文本都返回同一枚预设向量，便于把「距离」写死。"""

    def __init__(self, 向量: Sequence[float]) -> None:
        self._向量: list[float] = list(向量)
        self.texts: list[list[str]] = []
        self.维度参数: list[int | None] = []

    async def encode(self, texts: Sequence[str], *, dimensions: int | None = None) -> EncodeResult:
        self.维度参数.append(dimensions)
        self.texts.append(list(texts))
        return EncodeResult(
            items=tuple(
                EncodedVector(index=序号, vector=list(self._向量)) for 序号 in range(len(texts))
            ),
            prompt_tokens=None,
        )

    async def aclose(self) -> None:
        return None


def _嵌入服务(内核: _定向内核) -> EmbeddingService:
    """造一个真 `EmbeddingService`，白盒塞进定向内核。"""
    服务 = EmbeddingService(EmbeddingSettings(api_key="k", model="embed-m"))
    服务._encoder = cast("RemoteEncoder", cast("object", 内核))  # pyright: ignore[reportPrivateUsage]
    return 服务


def _配置(tmp_path: Path) -> MemorySettings:
    """默认把库与死信都落在 tmp_path 下，避免用例之间串味。"""
    基础: dict[str, object] = {
        "db_path": str(tmp_path / "db"),
        "dimensions": _维度,
        "queue_size": 8,
        "retry": {
            "max_attempts": 1,
            "backoff": 0.01,
            "deadletter_path": str(tmp_path / "死信.jsonl"),
        },
    }
    return MemorySettings(**基础)  # pyright: ignore[reportArgumentType]


def _服务(tmp_path: Path, *, 内核: _定向内核 | None = None) -> MemoryService:
    """造一个真 `MemoryService`，三个依赖都塞假货（本文件只真正用嵌入）。"""
    return MemoryService(
        _嵌入服务(内核 or _定向内核([1.0, 0.0, 0.0, 0.0])),
        LLMService.__new__(LLMService),
        DecisionService.__new__(DecisionService),
        _配置(tmp_path),
    )


def _日志配置(tmp_path: Path) -> LogSettings:
    """构造只关心落盘位置的日志配置：布局钉成行，断言写的是行内字段。"""
    return LogSettings(level="DEBUG", dir=str(tmp_path / "logs"), retention="1 day", layout="line")


async def _插原文(
    服务: MemoryService,
    *,
    编号: str,
    文本: str,
    向量: Sequence[float],
    重要度: float = 0.0,
    世界时间: int | None = None,
) -> None:
    """直接经会话写一条带向量的原文（绕过 worker，专供读路径用例）。"""
    会话 = 服务._session  # pyright: ignore[reportPrivateUsage]
    assert 会话 is not None
    分量 = ", ".join(repr(float(值)) for 值 in 向量)
    世界 = "null" if 世界时间 is None else str(世界时间)
    语句 = (
        f"CREATE (n:Utterance {{id:'{escape_literal(编号)}', text:'{escape_literal(文本)}', "
        f"importance:{重要度}, access_count:0, last_access:null, "
        f"world_time:{世界}, embedding:[{分量}]}})"
    )
    _ = await 会话.run(语句)


# --------------------------------------------------------------------------- 语义


async def test_语义召回按向量排序(tmp_path: Path) -> None:
    """近的那条排前面；条目带来源 id，世界时间原样解析出来。"""
    服务 = _服务(tmp_path, 内核=_定向内核([1.0, 0.0, 0.0, 0.0]))
    await 服务.start()
    try:
        await _插原文(服务, 编号="远", 文本="远", 向量=[0.0, 1.0, 0.0, 0.0], 重要度=0.5)
        await _插原文(服务, 编号="近", 文本="近", 向量=[1.0, 0.0, 0.0, 0.0], 重要度=1.0, 世界时间=3)

        情境 = await 服务.recall("问问")

        assert [条.source_id for 条 in 情境.items] == ["近", "远"]
        assert 情境.items[0].text == "近"
        assert 情境.items[0].kind == "utterance"
        assert 情境.items[0].world_time == 3
        assert any(步.source == "vector" for 步 in 情境.trace)
    finally:
        await 服务.stop()


async def test_空库召回返回空块(tmp_path: Path) -> None:
    """记忆为空是合法状态：空块、空条目、空依据，不报错。"""
    服务 = _服务(tmp_path)
    await 服务.start()
    try:
        情境 = await 服务.recall("随便问问")

        assert 情境.block == ""
        assert 情境.items == []
        assert 情境.trace == []
    finally:
        await 服务.stop()


# --------------------------------------------------------------------------- 结构化


async def test_召回同时给状态与回忆(tmp_path: Path) -> None:
    """语义与结构化两路都在结果里：既召回原文，也带出 focus 的当前状态。"""
    服务 = _服务(tmp_path, 内核=_定向内核([1.0, 0.0, 0.0, 0.0]))
    await 服务.start()
    try:
        会话 = 服务._session  # pyright: ignore[reportPrivateUsage]
        assert 会话 is not None
        await merge_character(会话, character_id="c1", name="阿黎", kind="person")
        await record_state_change(
            会话, character_id="c1", key="心境", value="平静", t_from=1, source_utterance="u0"
        )
        await _插原文(服务, 编号="u1", 文本="风起了", 向量=[1.0, 0.0, 0.0, 0.0], 重要度=1.0)

        # focus 里带一个查无此人的角色：它只是查不到状态、静默落空，不报错
        情境 = await 服务.recall("风", focus=["c1", "查无此人"])

        assert 情境.block.splitlines()[0] == "【此时的状态】"
        assert "心境" in 情境.block
        assert "风起了" in 情境.block
        assert any(步.source == "structured" for 步 in 情境.trace)
    finally:
        await 服务.stop()


# --------------------------------------------------------------------------- 预算


async def test_超预算按关联度与重要度截断(tmp_path: Path) -> None:
    """预算只装得下一条时，留 `相关度 × 重要度` 更高的那条，而不是更近的那条。"""
    服务 = _服务(tmp_path, 内核=_定向内核([1.0, 0.0, 0.0, 0.0]))
    await 服务.start()
    try:
        # 很近但不重要 → 乘积 0；较远但重要 → 乘积 0.5
        await _插原文(服务, 编号="很近", 文本="近", 向量=[1.0, 0.0, 0.0, 0.0], 重要度=0.0)
        await _插原文(服务, 编号="重要", 文本="要", 向量=[0.0, 1.0, 0.0, 0.0], 重要度=1.0)

        情境 = await 服务.recall("问", budget=1)

        assert [条.source_id for 条 in 情境.items] == ["重要"]
    finally:
        await 服务.stop()


# --------------------------------------------------------------------------- 边界


async def test_召回维度不符报存储错误(tmp_path: Path) -> None:
    """查询向量维度与建表维度不符时明确报错，不把坏向量喂给索引。"""
    服务 = _服务(tmp_path, 内核=_定向内核([1.0, 0.0, 0.0, 0.0, 0.0, 0.0]))
    await 服务.start()
    try:
        with pytest.raises(MemoryStoreError) as 捕获:
            _ = await 服务.recall("问")

        assert 捕获.value.operation == "recall"
    finally:
        await 服务.stop()


async def test_未启动召回报错(tmp_path: Path) -> None:
    """召回也是请求路径：没启动就调用要明确报错，而不是空转成功。"""
    服务 = _服务(tmp_path)

    with pytest.raises(MemoryStoreError) as 捕获:
        _ = await 服务.recall("问")

    assert 捕获.value.operation == "ingest"


# --------------------------------------------------------------------------- 访问回写


async def test_召回命中会回写访问计数(tmp_path: Path) -> None:
    """命中条目经 worker 回写：`access_count` 加一、`last_access` 记本次世界时间。"""
    服务 = _服务(tmp_path, 内核=_定向内核([1.0, 0.0, 0.0, 0.0]))
    await 服务.start()
    try:
        await _插原文(服务, 编号="u1", 文本="甲", 向量=[1.0, 0.0, 0.0, 0.0], 重要度=1.0)

        _ = await 服务.recall("问", world_time=42)

        会话 = 服务._session  # pyright: ignore[reportPrivateUsage]
        assert 会话 is not None
        行: list[list[object]] = []
        for _ in range(500):
            行 = await 会话.run(
                "MATCH (u:Utterance {id:'u1'}) RETURN u.access_count, u.last_access"
            )
            if 行[0][0] == 1:
                break
            await asyncio.sleep(0.01)

        assert 行[0] == [1, 42]
    finally:
        await 服务.stop()


async def test_回写队列满记错误但不抛(tmp_path: Path) -> None:
    """回写入队失败（队列已关）只记 ERROR，召回结果照常返回。"""
    files = setup_logging(_日志配置(tmp_path))
    服务 = _服务(tmp_path, 内核=_定向内核([1.0, 0.0, 0.0, 0.0]))
    await 服务.start()
    try:
        await _插原文(服务, 编号="u1", 文本="甲", 向量=[1.0, 0.0, 0.0, 0.0], 重要度=1.0)
        队列 = 服务._queue  # pyright: ignore[reportPrivateUsage]
        assert 队列 is not None
        队列.close()  # 关闭后入队一律被拒：模拟回写入队失败

        情境 = await 服务.recall("问")
        logger.remove()

        assert [条.source_id for 条 in 情境.items] == ["u1"]
        assert "回写队列已满" in files.app.read_text(encoding="utf-8")
    finally:
        await 服务.stop()
