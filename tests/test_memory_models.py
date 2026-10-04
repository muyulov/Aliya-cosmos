"""记忆层错误树与公开模型测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.memory import (
    MemoryConfigError,
    MemoryContext,
    MemoryError,
    MemoryExtractError,
    MemoryInputError,
    MemoryItem,
    MemoryStoreError,
    Observation,
    TraceStep,
)
from core.memory.models import new_id


def test_错误树同源() -> None:
    """四个子类都继承 MemoryError，调用方一次 except 就能兜住整层。"""
    for exc in (MemoryConfigError, MemoryStoreError, MemoryInputError, MemoryExtractError):
        assert issubclass(exc, MemoryError)


def test_配置错误带依赖属性() -> None:
    """依赖端点未就绪时，把是哪一个记进属性，便于对账。"""
    error = MemoryConfigError("嵌入端点未就绪", dependency="embedding")

    assert error.dependency == "embedding"
    assert str(error) == "嵌入端点未就绪"


def test_存储错误带操作属性() -> None:
    """Kùzu 失败时，把是哪一步操作记进属性，便于对账。"""
    error = MemoryStoreError("建表失败", operation="schema_init")

    assert error.operation == "schema_init"
    assert str(error) == "建表失败"


def test_原始异常挂在原因链上() -> None:
    """下层异常包进来时挂 __cause__，顺着原因链能找回去。"""
    cause = RuntimeError("kuzu boom")

    try:
        raise MemoryStoreError("查询失败", operation="query") from cause
    except MemoryStoreError as error:
        assert error.__cause__ is cause


def test_观测拒绝空文本() -> None:
    """空观测没有意义，且会让后续逐条校验白白跑一遍。"""
    with pytest.raises(ValidationError):
        _ = Observation(kind="dialogue", text="   ")


def test_观测默认值() -> None:
    obs = Observation(kind="narration", text="风起了")

    assert obs.speaker is None
    assert obs.participants == []
    assert obs.world_time is None


def test_观测拒绝未知类型() -> None:
    """kind 只认设计文档规定的三种。"""
    with pytest.raises(ValidationError):
        _ = Observation(kind="monologue", text="风起了")  # pyright: ignore[reportArgumentType]


def test_观测拒绝多余字段() -> None:
    """调用方传的是动作载荷，多写字段是调用方的错，应当立刻炸出来。"""
    with pytest.raises(ValidationError):
        _ = Observation(kind="dialogue", text="风起了", mood="calm")  # pyright: ignore[reportCallIssue]


def test_记忆条目带溯源字段() -> None:
    item = MemoryItem(kind="utterance", text="风起了", source_id="u1", score=0.9)

    assert item.world_time is None


def test_记忆条目保留全部字段() -> None:
    item = MemoryItem(
        kind="event",
        text="城门开了",
        source_id="e1",
        score=0.75,
        world_time=12,
    )

    assert item.kind == "event"
    assert item.text == "城门开了"
    assert item.source_id == "e1"
    assert item.score == 0.75
    assert item.world_time == 12


def test_记忆条目来源三类都能构造() -> None:
    """来源只认设计文档 §7.1 规定的那三类：对话原文、事件、反思。"""
    for kind in ("utterance", "event", "reflection"):
        item = MemoryItem(kind=kind, text="风起了", source_id="s1", score=0.5)
        assert item.kind == kind


def test_记忆条目拒绝未知来源() -> None:
    with pytest.raises(ValidationError):
        _ = MemoryItem(kind="不存在的来源", text="风起了", source_id="s1", score=0.5)  # pyright: ignore[reportArgumentType]


def test_召回步骤来源标记合法取值() -> None:
    """trace 的来源标记只认设计文档 §7.1 规定的那几种。"""
    for source in ("vector", "graph", "structured"):
        step = TraceStep(source=source, detail="依据")
        assert step.source == source


def test_召回步骤来源标记拒绝未知取值() -> None:
    with pytest.raises(ValidationError):
        _ = TraceStep(source="guess", detail="依据")  # pyright: ignore[reportArgumentType]


def test_记忆上下文默认值() -> None:
    """无命中返回空块，不报错——记忆为空是合法状态。"""
    context = MemoryContext()

    assert context.block == ""
    assert context.items == []
    assert context.trace == []


def test_主键生成长度为三十二() -> None:
    """uuid4().hex 是 32 位十六进制串：零依赖、零发明，长度可预期。"""
    pk = new_id()

    assert len(pk) == 32
    assert pk == pk.lower()
    assert all(chars in "0123456789abcdef" for chars in pk)


def test_主键互不相同() -> None:
    """连造一批不重样，够当主键用。"""
    batch = {new_id() for _ in range(1000)}

    assert len(batch) == 1000
