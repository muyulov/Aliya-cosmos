"""进程内总线与用户通道。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from loguru import logger

from agent.cosmos.contracts import (
    ConversationContext,
    InboundEnvelope,
    TerminalMessage,
)
from agent.cosmos.errors import MessageDeliveryError
from agent.cosmos.transport import (
    MAX_REPLAY,
    BusService,
    MemoryEventBus,
    MemoryMessageBus,
    MemoryUserChannel,
)
from core.config import Settings
from core.service import ClockService


def make_envelope(text: str) -> InboundEnvelope:
    return InboundEnvelope(
        message=TerminalMessage(
            message_id=f"msg_{text}",
            direction="user_to_aliya",
            kind="chat",
            text=text,
            at=datetime.now(UTC),
            session_id="ses_1",
        ),
        conversation=ConversationContext(session_id="ses_1"),
    )


async def take[T](iterator: AsyncIterator[T], count: int) -> list[T]:
    """从异步迭代器取 count 条，超时即失败。"""
    items: list[T] = []
    for _ in range(count):
        items.append(await asyncio.wait_for(anext(iterator), 1.0))
    return items


async def test_消息顺序与广播() -> None:
    bus = MemoryMessageBus()
    first = bus.subscribe("aliya")
    second = bus.subscribe("aliya")
    try:
        for text in ("a", "b", "c"):
            await bus.publish("aliya", make_envelope(text))

        one = await take(first, 3)
        two = await take(second, 3)
        assert [item.message.text for item in one] == ["a", "b", "c"]
        assert [item.message.text for item in two] == ["a", "b", "c"]
    finally:
        await first.aclose()
        await second.aclose()


async def test_无订阅者时先进积压() -> None:
    bus = MemoryMessageBus(limit=2)
    await bus.publish("aliya", make_envelope("a"))
    await bus.publish("aliya", make_envelope("b"))
    await bus.publish("aliya", make_envelope("c"))

    iterator = bus.subscribe("aliya")
    try:
        items = await take(iterator, 2)
        # 积压上限 2：最旧的 a 被丢弃
        assert [item.message.text for item in items] == ["b", "c"]
    finally:
        await iterator.aclose()


async def test_事件序列按主题递增() -> None:
    bus = MemoryEventBus(now=lambda: datetime.now(UTC))
    first = await bus.publish("agent.progress", {"progress": 1})
    second = await bus.publish("agent.progress", {"progress": 2})
    other = await bus.publish("presence.changed", {"revision": 1})

    assert (first.sequence, second.sequence) == (1, 2)
    assert other.sequence == 1
    assert first.event_id != second.event_id


async def test_按序列补发缺口() -> None:
    bus = MemoryEventBus(now=lambda: datetime.now(UTC))
    for index in range(3):
        _ = await bus.publish("agent.progress", {"progress": index})

    iterator = bus.subscribe(["agent.progress"], since_sequence=1)
    try:
        events = await take(iterator, 2)
        assert [event.sequence for event in events] == [2, 3]
    finally:
        await iterator.aclose()


async def test_缺口过大时报错() -> None:
    bus = MemoryEventBus(now=lambda: datetime.now(UTC), buffer_limit=MAX_REPLAY + 10)
    for _ in range(MAX_REPLAY + 1):
        _ = await bus.publish("agent.progress", {})

    with pytest.raises(MessageDeliveryError) as info:
        _ = bus.subscribe(["agent.progress"], since_sequence=0)
    assert info.value.code == "gap_too_large"


async def test_慢消费丢最旧并告警() -> None:
    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    bus = MemoryEventBus(now=lambda: datetime.now(UTC), subscriber_limit=1)
    iterator = bus.subscribe(["agent.progress"])
    try:
        for index in range(3):
            _ = await bus.publish("agent.progress", {"progress": index})
        assert any("过慢" in message for message in messages)
    finally:
        await iterator.aclose()
        logger.remove(sink_id)


async def test_用户通道取走后清空() -> None:
    channel = MemoryUserChannel()
    await channel.send(
        TerminalMessage(
            message_id="msg_1",
            direction="aliya_to_user",
            kind="chat",
            text="在啊",
            at=datetime.now(UTC),
            session_id="ses_1",
        )
    )
    assert len(channel.drain()) == 1
    assert channel.drain() == []


async def test_总线服务重复启停不报错(settings: Settings, clock: ClockService) -> None:
    service = BusService(settings.agent, clock)
    await service.start()
    await service.start()
    await service.stop()
    await service.stop()

    # stop 后再 start 仍可用
    await service.start()
    iterator = service.messages.subscribe("aliya")
    try:
        await service.messages.publish("aliya", make_envelope("x"))
        assert len(await take(iterator, 1)) == 1
    finally:
        await iterator.aclose()


async def test_时间取时钟(clock: ClockService) -> None:
    bus = MemoryEventBus(now=lambda: clock.now() - timedelta(seconds=1))
    event = await bus.publish("x", {})
    assert event.at < clock.now()
