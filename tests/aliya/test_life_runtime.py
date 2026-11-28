"""Runtime 生活层接线测试：当下文案、开关与上下文上报。"""

from __future__ import annotations

from datetime import UTC, datetime
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
