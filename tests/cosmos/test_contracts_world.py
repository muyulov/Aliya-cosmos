"""契约模型（world）的假设数量约束与 round-trip。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from agent.cosmos.contracts import (
    ActionWindow,
    AgentSignals,
    AliyaContext,
    HistoryPattern,
    Hypothesis,
    LinkSignal,
    PresenceSignal,
    TaskSignals,
    TimeSignal,
    WorldInference,
    WorldInferenceEvent,
    WorldInput,
)


def make_time() -> TimeSignal:
    return TimeSignal(now=datetime.now(UTC), timezone="Asia/Shanghai", period="evening")


def make_inference(count: int = 2) -> WorldInference:
    return WorldInference(
        inference_id="inf_test",
        generated_at=datetime.now(UTC),
        snapshot="你离线 2 小时了",
        hypotheses=[
            Hypothesis(statement=f"可能{i}", confidence=0.5, basis="没有消息") for i in range(count)
        ],
        signals_used=["time", "presence"],
        confidence=0.6,
    )


def test_假设超三条报错() -> None:
    with pytest.raises(ValidationError):
        _ = make_inference(4)


def test_假设为空报错() -> None:
    with pytest.raises(ValidationError):
        _ = make_inference(0)


def test_行动窗口最多三条() -> None:
    with pytest.raises(ValidationError):
        _ = WorldInference(
            inference_id="inf",
            generated_at=datetime.now(UTC),
            snapshot="snapshot",
            hypotheses=[Hypothesis(statement="x", confidence=0.5, basis="y")],
            action_windows=[
                ActionWindow(window="w", suggestion="s", rationale="r") for _ in range(4)
            ],
        )


def test_世界输入_json_round_trip() -> None:
    payload = WorldInput(
        generated_at=datetime.now(UTC),
        time=make_time(),
        link=LinkSignal(state="down", queued_user_messages=2),
        presence=PresenceSignal(online_state="offline", unavailable_fields=["typing"]),
        tasks=TaskSignals(),
        agents=AgentSignals(total=1, idle=1),
        history=HistoryPattern(active_hours=[21, 22], sample_days=4),
        aliya_context=AliyaContext(phase="daily", open_topics=["排障"]),
    )
    restored = WorldInput.model_validate_json(payload.model_dump_json())
    assert restored == payload


def test_推理事件_json_round_trip() -> None:
    event = WorldInferenceEvent(inference=make_inference(), age_seconds=0.3, trigger="tick")
    restored = WorldInferenceEvent.model_validate_json(event.model_dump_json())
    assert restored == event


def test_采集关闭时_presence_为_None() -> None:
    payload = WorldInput(generated_at=datetime.now(UTC), time=make_time(), presence=None)
    assert payload.presence is None


def test_自我状态与船图信号可往返() -> None:
    payload = WorldInput.model_validate(
        {
            "generated_at": "2026-11-28T14:00:00+00:00",
            "time": {"now": "2026-11-28T14:00:00+00:00", "timezone": "Asia/Shanghai"},
            "self_state": {
                "location": "engine",
                "location_name": "动力室",
                "activity": "修阀门",
                "since_minutes": 12,
                "interruptibility": "low",
                "energy": 0.4,
                "comms": "none",
            },
            "ship": {
                "comms": "none",
                "nearest_signal_place": "galley",
                "reachable": [{"place": "galley", "name": "食堂", "minutes": 2}],
            },
        }
    )
    assert payload.self_state is not None and payload.self_state.energy == 0.4
    assert payload.ship is not None and payload.ship.reachable[0].minutes == 2
    assert WorldInput.model_validate_json(payload.model_dump_json()) == payload


def test_自我视角与行动目标可往返() -> None:
    from agent.cosmos.contracts import SelfView

    view = SelfView(
        situation="信号断了，手上的活不能撒手", suggested_next="stay", reason="阀门没修完"
    )
    window = ActionWindow(
        window="稍后", suggestion="修完再过去", rationale="别半途而废", target="engine"
    )
    assert window.target == "engine"
    assert SelfView.model_validate_json(view.model_dump_json()) == view
