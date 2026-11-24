"""内置规则世界推理端口：能产出可校验的保守结论。"""

from __future__ import annotations

from datetime import UTC, datetime

from agent.cosmos.contracts import (
    PresenceSignal,
    TimeSignal,
    WorldInput,
)
from agent.cosmos.world.reasoning import RuleWorldReasoningPort, build


def _input(presence: PresenceSignal | None = None) -> WorldInput:
    now = datetime(2026, 11, 25, 22, 0, tzinfo=UTC)
    return WorldInput(
        generated_at=now,
        time=TimeSignal(now=now, timezone="UTC", period="night"),
        presence=presence,
    )


async def test_离线时给出低打扰结论() -> None:
    result = await RuleWorldReasoningPort().infer(
        _input(PresenceSignal(online_state="offline", silence_minutes=120))
    )
    assert result.hypotheses
    assert result.signals_used
    assert any(window.kind == "wait" for window in result.action_windows)
    assert all(
        set(hypothesis.basis_signals) <= set(result.signals_used)
        for hypothesis in result.hypotheses
    )


async def test_在线输入时倾向观察() -> None:
    result = await RuleWorldReasoningPort().infer(
        _input(
            PresenceSignal(
                online_state="online",
                is_typing=True,
                messages_last_15m=2,
            )
        )
    )
    assert any(window.kind == "observe" for window in result.action_windows)
    assert 0 < result.confidence <= 1


async def test_无信号时回退到保守假设() -> None:
    result = await RuleWorldReasoningPort().infer(_input())
    assert len(result.hypotheses) >= 1
    assert result.hypotheses[0].confidence <= 0.3
    assert result.signals_used == ["time"]


def test_工厂入口() -> None:
    assert isinstance(build(), RuleWorldReasoningPort)
