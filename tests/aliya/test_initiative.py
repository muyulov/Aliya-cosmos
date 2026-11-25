from datetime import UTC, datetime, timedelta

from agent.aliya.initiative import Initiative, PreferenceGate, TimeSignals
from agent.cosmos.contracts import ReadablePresence
from core.config.settings import AgentAliyaInitiativeSettings


def now() -> datetime:
    return datetime.now(UTC)


def test_default_is_wait() -> None:
    gate = Initiative(AgentAliyaInitiativeSettings())
    decision = gate.should_speak(now=now(), presence=None, link="up", last_user_at=now())
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


def test_unanswered_proactive_doubles_gap() -> None:
    gate = Initiative(AgentAliyaInitiativeSettings(min_gap_seconds=100, daily_soft_limit=10))
    start = now()
    gate.record_proactive(start)
    gate.record_proactive(start + timedelta(seconds=101))
    gate.record_proactive(start + timedelta(seconds=202))
    decision = gate.should_speak(
        now=start + timedelta(seconds=302),
        presence=None,
        link="up",
        last_user_at=start - timedelta(hours=3),
    )
    assert decision.should_speak is False
    assert decision.reason == "min_gap"

    decision = gate.should_speak(
        now=start + timedelta(seconds=402),
        presence=None,
        link="up",
        last_user_at=start - timedelta(hours=3),
    )
    assert decision.should_speak is True


def test_user_reply_resets_unanswered_gap() -> None:
    gate = Initiative(AgentAliyaInitiativeSettings(min_gap_seconds=100, daily_soft_limit=10))
    start = now()
    gate.record_proactive(start)
    gate.record_proactive(start + timedelta(seconds=101))
    gate.record_proactive(start + timedelta(seconds=202))
    gate.record_user_reply()
    gate.record_proactive(start + timedelta(seconds=303))
    decision = gate.should_speak(
        now=start + timedelta(seconds=404),
        presence=None,
        link="up",
        last_user_at=start - timedelta(hours=3),
    )
    assert decision.should_speak is True


def test_can_ignore_link_and_presence_hints() -> None:
    gate = Initiative(
        AgentAliyaInitiativeSettings(
            respect_link_state=False,
            respect_presence_hint=False,
        )
    )
    decision = gate.should_speak(
        now=now(),
        presence=ReadablePresence(summary="对方在忙", tone="away"),
        link="down",
        last_user_at=now() - timedelta(hours=3),
    )
    assert decision.should_speak is True
    assert decision.reason == "long_gap"


def test_default_action_can_speak() -> None:
    gate = Initiative(AgentAliyaInitiativeSettings(default_action="speak"))
    decision = gate.should_speak(now=now(), presence=None, link="up", last_user_at=now())
    assert decision.should_speak is True
    assert decision.reason == "default_speak"


def test_preference_gate_matches_open_topic_after_six_hours() -> None:
    gate = PreferenceGate(["有未收尾的话题且间隔超过 6 小时"])
    current = now()
    decision = gate.match(
        TimeSignals(
            now=current,
            last_user_at=current - timedelta(hours=7),
            open_topics=("上次没说完的排障",),
        )
    )
    assert decision.matched is True
    assert decision.reason == "open_topic"


def test_preference_gate_rejects_short_open_topic_gap() -> None:
    gate = PreferenceGate(["有未收尾的话题且间隔超过 6 小时"])
    current = now()
    decision = gate.match(
        TimeSignals(
            now=current,
            last_user_at=current - timedelta(hours=1),
            open_topics=("上次没说完的排障",),
        )
    )
    assert decision.matched is False
    assert decision.reason == "no_prefer_when"


def test_preference_gate_insomnia_uses_world_or_night() -> None:
    gate = PreferenceGate(["失眠且对方超过 2 小时没来"])
    current = now().replace(hour=14, minute=0, second=0, microsecond=0)
    decision = gate.match(
        TimeSignals(
            now=current,
            last_user_at=current - timedelta(hours=3),
            world="她可能失眠",
        )
    )
    assert decision.matched is True
    assert decision.reason == "insomnia"

    no_signal = gate.match(
        TimeSignals(
            now=current,
            last_user_at=current - timedelta(hours=3),
            world="她在工作",
        )
    )
    assert no_signal.matched is False

    night = gate.match(
        TimeSignals(
            now=current.replace(hour=23, minute=30),
            last_user_at=current - timedelta(hours=3),
        )
    )
    assert night.matched is True
    assert night.reason == "insomnia"


def test_preference_gate_repair_requires_recent_context() -> None:
    gate = PreferenceGate(["刚修好某样东西，想被夸"])
    current = now()
    fresh = gate.match(
        TimeSignals(
            now=current,
            world="刚修好引擎",
            world_at=current - timedelta(minutes=10),
        )
    )
    assert fresh.matched is True
    assert fresh.reason == "repair"

    stale = gate.match(
        TimeSignals(
            now=current,
            world="刚修好引擎",
            world_at=current - timedelta(hours=3),
        )
    )
    assert stale.matched is False


def test_preference_gate_late_night_and_lexical_fallback() -> None:
    current = now().replace(hour=23, minute=30, second=0, microsecond=0)
    late = PreferenceGate(["深夜刷信息刷到想说话"])
    assert late.match(TimeSignals(now=current)).matched is True

    lexical = PreferenceGate(["外面下雨了"])
    decision = lexical.match(TimeSignals(now=current.replace(hour=14), world="外面在下雨"))
    assert decision.matched is True
    assert decision.reason == "lexical"


def test_preference_gate_allows_when_no_conditions() -> None:
    decision = PreferenceGate([]).match(TimeSignals(now=now()))
    assert decision.matched is True
    assert decision.reason == "no_prefer_when"
