"""用户状态线索：可用性、在线判定、可解读表述、去抖与隐私。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import cast

from loguru import logger

from agent.cosmos.contracts import CosmosEvent, MoodInfo, PresenceInput
from agent.cosmos.services import PresenceService
from agent.cosmos.transport import BusService
from core.config import Settings
from core.decision.service import DecisionService
from core.llm.service import LLMService
from core.service import ClockService


class FakeClock:
    """可推进的固定时钟。"""

    def __init__(self) -> None:
        self.value: datetime = datetime(2026, 11, 7, 21, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


def make_presence(
    settings: Settings,
    clock: FakeClock,
    bus: BusService,
) -> PresenceService:
    return PresenceService(
        cast("ClockService", cast("object", clock)),
        DecisionService(settings.decision),
        LLMService(settings.llm),
        bus,
        settings.agent,
    )


def test_默认可用性与逐项关闭(settings: Settings, bus: BusService) -> None:
    clock = FakeClock()
    presence = make_presence(settings, clock, bus)
    availability = presence.snapshot().availability
    for field in ("online", "activity", "typing", "task_context", "env"):
        assert availability[field].available is True
    # 没有文本证据时情绪不可用
    assert availability["mood"].available is False
    assert availability["mood"].reason == "no_evidence"

    for field in ("online", "activity", "typing", "env", "task_context"):
        configured = settings.agent.cosmos.presence.model_copy(deep=True)
        setattr(configured.fields, field, False)
        settings.agent.cosmos.presence = configured
        closed = make_presence(settings, clock, bus)
        assert closed.snapshot().availability[field].available is False
        assert closed.snapshot().availability[field].reason == "disabled_by_config"


async def test_在线三态按时间推进(settings: Settings, bus: BusService) -> None:
    clock = FakeClock()
    presence = make_presence(settings, clock, bus)
    await presence.touch()
    assert presence.snapshot().fields.online.state == "online"

    clock.advance(400)
    assert presence.snapshot().fields.online.state == "away"

    clock.advance(600)
    assert presence.snapshot().fields.online.state == "offline"


async def test_可解读表述覆盖主要分支(settings: Settings, bus: BusService) -> None:
    clock = FakeClock()
    presence = make_presence(settings, clock, bus)

    # 无任何信号
    assert presence.snapshot().readable.summary == "不知道你现在什么情况"

    # 正在输入
    _ = await presence.apply(PresenceInput(online="online", is_typing=True))
    readable = presence.snapshot().readable
    assert readable.summary == "你在线，正在输入"
    assert readable.tone == "engaged"
    assert readable.hint is not None

    # 刚发过消息
    await presence.touch()
    assert presence.snapshot().readable.summary == "你在线，刚发过消息"

    # 在等结果
    clock.advance(120)
    presence.set_awaiting("tsk_1")
    assert presence.snapshot().readable.summary == "你在线，还在等结果"

    # 在线但一阵没说话
    presence.set_awaiting(None)
    assert "分钟没说话了" in presence.snapshot().readable.summary

    # 在线且长时间没动静（把 idle 阈值拉长，仍在 online 区间）
    settings.agent.cosmos.presence.idle_after_seconds = 600
    clock.advance(200)
    assert "你还在线" in presence.snapshot().readable.summary

    # away
    clock.advance(400)
    assert "你暂时离开了" in presence.snapshot().readable.summary

    # offline + 深夜
    clock.advance(1000)
    clock.value = clock.value.replace(hour=23)
    assert "现在是深夜" in presence.snapshot().readable.summary

    # offline + 白天
    clock.value = clock.value.replace(hour=14)
    assert "现在是深夜" not in presence.snapshot().readable.summary


async def test_情绪低置信度不给结论(settings: Settings, bus: BusService) -> None:
    clock = FakeClock()
    presence = make_presence(settings, clock, bus)
    presence._mood = MoodInfo(  # pyright: ignore[reportPrivateUsage]
        valence=0.8,
        energy=0.9,
        confidence=0.1,
        source="llm",
        evidence_count=3,
        updated_at=clock.now(),
    )
    snapshot = presence.snapshot()
    assert snapshot.availability["mood"].available is False
    assert snapshot.fields.mood.source == "none"
    assert "情绪" not in snapshot.readable.summary


async def test_情绪数值不进日志(settings: Settings, bus: BusService) -> None:
    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(str(message)), level="DEBUG")
    clock = FakeClock()
    presence = make_presence(settings, clock, bus)
    try:
        presence.set_explicit_mood(valence=0.83, energy=0.71)
        _ = presence.snapshot()
    finally:
        logger.remove(sink_id)

    joined = "\n".join(messages)
    assert "valence" not in joined
    assert "energy" not in joined
    assert "0.83" not in joined


async def test_采集关闭返回未知快照(settings: Settings, bus: BusService) -> None:
    settings.agent.cosmos.presence.enabled = False
    clock = FakeClock()
    presence = make_presence(settings, clock, bus)
    snapshot = presence.snapshot()
    assert snapshot.readable.summary == "用户状态未知"
    assert snapshot.readable.tone == "unknown"


async def test_typing_抖动合并为一次(settings: Settings, bus: BusService) -> None:
    settings.agent.cosmos.presence.push.typing_settle_seconds = 0.05
    fake_clock = FakeClock()
    presence = make_presence(settings, fake_clock, bus)
    await presence.start()
    events: list[CosmosEvent] = []

    async def consume() -> None:
        async for event in bus.events.subscribe(["presence.changed"]):
            events.append(event)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    try:
        for typing in (True, False, True, False):
            _ = await presence.apply(PresenceInput(online="online", is_typing=typing))
        await asyncio.sleep(0.15)
        assert len(events) == 1
        payload = events[0].payload
        assert "typing" in cast("list[str]", payload["changed"])
        snapshot = cast("dict[str, object]", payload["snapshot"])
        fields = cast("dict[str, object]", snapshot["fields"])
        typing_info = cast("dict[str, object]", fields["typing"])
        assert typing_info["is_typing"] is False
    finally:
        _ = task.cancel()
        _ = await asyncio.gather(task, return_exceptions=True)
        await presence.stop()


async def test_隐藏字段不再推送(settings: Settings, bus: BusService) -> None:
    settings.agent.cosmos.presence.push.typing_settle_seconds = 0.01
    presence = make_presence(settings, FakeClock(), bus)
    await presence.start()
    events: list[CosmosEvent] = []

    async def consume() -> None:
        async for event in bus.events.subscribe(["presence.changed"]):
            events.append(event)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    try:
        presence.set_visibility(["typing"])
        _ = await presence.apply(PresenceInput(is_typing=True))
        await asyncio.sleep(0.05)
        assert events == []
    finally:
        _ = task.cancel()
        _ = await asyncio.gather(task, return_exceptions=True)
        await presence.stop()


async def test_状态查询不抛错(settings: Settings, bus: BusService) -> None:
    presence = make_presence(settings, FakeClock(), bus)
    snapshot = presence.snapshot()
    assert isinstance(snapshot.fields.online.state, str)
    assert snapshot.ttl_seconds == 30


def test_关闭字段不泄露原始值(settings: Settings, bus: BusService) -> None:
    clock = FakeClock()
    settings.agent.cosmos.presence.fields.mood = False
    settings.agent.cosmos.presence.fields.typing = False
    presence = make_presence(settings, clock, bus)
    presence.set_explicit_mood(0.8, 0.9)
    snapshot = presence.snapshot()
    assert snapshot.fields.mood.source == "none"
    assert snapshot.fields.mood.valence == 0.0
    assert snapshot.fields.typing.is_typing is False
    assert snapshot.availability["mood"].reason == "disabled_by_config"


def test_时区隐私开关(settings: Settings, bus: BusService) -> None:
    clock = FakeClock()
    settings.agent.cosmos.presence.privacy.allow_timezone = False
    presence = make_presence(settings, clock, bus)
    assert presence.snapshot().fields.env.timezone == "UTC"


async def test_文案模板可覆盖在线静默(settings: Settings, bus: BusService) -> None:
    clock = FakeClock()
    settings.agent.cosmos.presence.wording.online_idle_template = "安静了 {minutes} 分钟"
    presence = make_presence(settings, clock, bus)
    await presence.touch()
    clock.advance(100)
    assert presence.snapshot().readable.summary == "安静了 1 分钟"
