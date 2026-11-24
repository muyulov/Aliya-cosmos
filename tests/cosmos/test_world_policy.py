"""一步 EFE 行动选择：低打扰优先、信息增益与成本分解。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import pytest

from agent.cosmos.contracts import (
    ActionWindow,
    BeliefState,
    Hypothesis,
    PresenceSignal,
    WorldInference,
)
from agent.cosmos.services.world_policy import WorldActionSelector


def _inference(windows: list[ActionWindow] | None = None) -> WorldInference:
    return WorldInference(
        inference_id="inf_policy",
        generated_at=datetime.now(UTC),
        snapshot="测试",
        hypotheses=[Hypothesis(statement="可能在线", confidence=0.6, basis="presence")],
        action_windows=windows or [],
        signals_used=["presence"],
        confidence=0.6,
    )


def _presence(
    state: Literal["online", "away", "busy", "offline", "unknown"],
    *,
    typing: bool = False,
) -> PresenceSignal:
    return PresenceSignal(online_state=state, is_typing=typing)


def test_离线时低打扰行动优先() -> None:
    selector = WorldActionSelector()
    choice = selector.select(
        _inference(),
        BeliefState(online_prob=0.0, readiness=0.6, uncertainty=0.4),
        _presence("offline"),
        confidence=0.7,
        urgency=0.8,
        interruptibility=0.1,
    )
    assert choice.action in {"wait", "observe"}
    assert choice.cost <= 0.2


def test_在线且不确定时倾向确认() -> None:
    selector = WorldActionSelector()
    choice = selector.select(
        _inference(),
        BeliefState(online_prob=1.0, readiness=0.3, uncertainty=0.8),
        _presence("online"),
        confidence=0.5,
        urgency=0.4,
        interruptibility=0.9,
    )
    assert choice.action in {"check_in", "observe"}
    assert choice.epistemic > 0


def test_行动窗口按类型与期望价值选择() -> None:
    selector = WorldActionSelector()
    window = ActionWindow(
        window="now",
        suggestion="提醒你休息",
        rationale="久坐",
        kind="notify",
        expected_value=0.9,
    )
    choice = selector.select(
        _inference([window]),
        BeliefState(readiness=0.7, uncertainty=0.3),
        _presence("online"),
        confidence=0.8,
        urgency=0.9,
        interruptibility=0.9,
    )
    assert choice.action == "notify"
    assert choice.window == "now"
    assert choice.expected_value == pytest.approx(0.9)
    assert choice.efe == pytest.approx(choice.pragmatic + choice.epistemic - choice.cost, abs=1e-3)


def test_关键词回退推断行动类型() -> None:
    selector = WorldActionSelector(horizon=1)
    window = ActionWindow(
        window="later",
        suggestion="先问一下你现在方便吗",
        rationale="状态不明",
        expected_value=1.0,
    )
    choice = selector.select(
        _inference([window]),
        BeliefState(uncertainty=0.9),
        _presence("online"),
        confidence=1.0,
        urgency=0.4,
        interruptibility=0.9,
    )
    assert choice.action == "check_in"
    assert choice.window == "later"
