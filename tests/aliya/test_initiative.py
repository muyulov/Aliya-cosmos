from datetime import UTC, datetime, timedelta

from agent.aliya.initiative import Initiative
from agent.cosmos.contracts import ReadablePresence
from core.config.settings import AgentAliyaInitiativeSettings


def now() -> datetime:
    return datetime.now(UTC)


def test_default_is_wait() -> None:
    gate = Initiative(AgentAliyaInitiativeSettings())
    decision = gate.should_speak(
        now=now(), presence=None, link="up", last_user_at=now()
    )
    assert decision.should_speak is False
    assert decision.reason == "default_wait"


def test_speaks_after_long_gap() -> None:
    gate = Initiative(AgentAliyaInitiativeSettings())
    decision = gate.should_speak(
        now=now(),
        presence=None,
        link="up",
        last_user_at=now() - timedelta(hours=3),
    )
    assert decision.should_speak is True
    assert decision.reason == "long_gap"


def test_link_down_blocks() -> None:
    gate = Initiative(AgentAliyaInitiativeSettings())
    decision = gate.should_speak(
        now=now(),
        presence=None,
        link="down",
        last_user_at=now() - timedelta(hours=3),
    )
    assert decision.should_speak is False
    assert decision.reason == "link_down"


def test_presence_away_blocks() -> None:
    gate = Initiative(AgentAliyaInitiativeSettings())
    decision = gate.should_speak(
        now=now(),
        presence=ReadablePresence(summary="对方在忙", tone="away"),
        link="up",
        last_user_at=now() - timedelta(hours=3),
    )
    assert decision.should_speak is False
    assert decision.reason == "presence"


def test_min_gap_blocks() -> None:
    gate = Initiative(AgentAliyaInitiativeSettings())
    gate.record_proactive(now())
    decision = gate.should_speak(
        now=now(),
        presence=None,
        link="up",
        last_user_at=now() - timedelta(hours=3),
    )
    assert decision.should_speak is False
    assert decision.reason == "min_gap"


def test_daily_limit_blocks() -> None:
    gate = Initiative(AgentAliyaInitiativeSettings(daily_soft_limit=1))
    gate.record_proactive(now())
    decision = gate.should_speak(
        now=now() + timedelta(hours=2),
        presence=None,
        link="up",
        last_user_at=now() - timedelta(hours=3),
    )
    assert decision.should_speak is False
    assert decision.reason == "daily_limit"
