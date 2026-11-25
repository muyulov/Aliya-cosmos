from datetime import UTC, datetime

from agent.aliya.runtime import phase_for


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 1, 1, hour, minute, tzinfo=UTC)


def test_phase_rules() -> None:
    assert phase_for(now=at(23, 30), tasks=0, crisis=False) == "rest"
    assert phase_for(now=at(12, 0), tasks=1, crisis=False) == "maintenance"
    assert phase_for(now=at(12, 0), tasks=0, crisis=True) == "crisis"
    assert phase_for(now=at(12, 0), tasks=0, crisis=False) == "daily"


def test_rest_covers_midnight_and_early_morning() -> None:
    assert phase_for(now=at(0, 30), tasks=0, crisis=False) == "rest"
    assert phase_for(now=at(4, 59), tasks=0, crisis=False) == "rest"
    assert phase_for(now=at(5, 0), tasks=0, crisis=False) == "daily"
