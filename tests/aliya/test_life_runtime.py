"""Runtime 生活层接线测试：当下文案、开关与上下文上报。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from agent.aliya.life.models import Activity, ActivityState
from agent.aliya.runtime import AliyaRuntime, _moment_text

NOW = datetime(2026, 11, 28, 14, 0, tzinfo=UTC)


def test_当下文案优先读真实活动() -> None:
    state = ActivityState(updated_at=NOW)
    state.current = Activity(
        id="act_1", title="把二舱压力阀拧到底", place="engine", kind="repair", started_at=NOW
    )
    assert (
        _moment_text(NOW, state, lambda _: "动力室")
        == "你正在把二舱压力阀拧到底（在动力室，约 30 分钟）"
    )
    assert "这个点" in _moment_text(NOW)


def test_生活开关关闭时不创建活动队列() -> None:
    from core.config import AgentAliyaSettings

    config = AgentAliyaSettings()
    config.life.enabled = False
    runtime = AliyaRuntime(control=cast("Any", None), llm=cast("Any", None), config=config)
    assert runtime._life_queue is None  # type: ignore[attr-defined]


async def test_用户消息只标记打断不取消活动(tmp_path: Path) -> None:
    from agent.aliya.life.store import ActivityStore

    store = ActivityStore(tmp_path)
    state = ActivityState(updated_at=NOW)
    state.current = Activity(
        id="act_1", title="修阀门", place="engine", kind="repair", started_at=NOW
    )
    await store.save(state)
    state.current.interrupted_at = NOW
    await store.save(state)
    loaded = await store.load()
    assert loaded.current is not None
    assert loaded.current.interrupted_at == NOW


def test_alya_context_带地点与活动() -> None:
    from agent.cosmos.contracts import AliyaContext

    ctx = AliyaContext(
        location="engine", activity="把二舱压力阀拧到底", comms="none", free_minutes=12
    )
    assert ctx.model_dump()["comms"] == "none"


def test_用户回合上下文含刚才在做什么() -> None:
    from agent.aliya.turn.context import TurnContext

    turn = TurnContext(user_text="在吗", now=NOW)
    turn.memory = "你刚才在动力室，被叫了一下；正在把二舱压力阀拧到底。"
    assert "动力室" in turn.context_text()


def test_生活tick可缩短检查周期() -> None:
    from core.config import AgentAliyaSettings

    config = AgentAliyaSettings()
    config.initiative.tick_seconds = 60
    config.life.tick_seconds = 10
    runtime = AliyaRuntime(control=cast("Any", None), llm=cast("Any", None), config=config)
    assert runtime._tick_interval() == 10

    config.life.enabled = False
    assert runtime._tick_interval() == 60


async def test_反思失败后记录日期不再重试(tmp_path: Path) -> None:
    from agent.aliya.life.store import ActivityStore
    from core.config import AgentAliyaSettings

    class FailingLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def chat_structured(self, messages: Any, schema: Any, **kwargs: Any) -> Any:
            self.calls += 1
            raise RuntimeError("timeout")

    config = AgentAliyaSettings()
    config.memory.dir = str(tmp_path)
    llm = FailingLLM()
    runtime = AliyaRuntime(control=cast("Any", None), llm=cast("Any", llm), config=config)
    runtime._life_store = ActivityStore(tmp_path)
    state = ActivityState(updated_at=NOW)
    state.current = Activity(
        id="act_1", title="修阀门", place="engine", kind="repair", started_at=NOW
    )
    await runtime._life_store.append(state, ended_at=NOW, notes="卡住了")
    runtime._life_state = await runtime._life_store.load()

    await runtime._reflect_if_needed(NOW)
    assert (await runtime._life_store.load()).last_reflection_date == "2026-11-28"
    assert llm.calls == 1

    await runtime._reflect_if_needed(NOW)
    assert llm.calls == 1
