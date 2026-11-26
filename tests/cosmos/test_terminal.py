"""通信终端：收发、直答、分段、主动消息节流与链路重放。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from agent.cosmos.contracts import (
    AgentCard,
    AgentContext,
    AgentRequest,
    AgentResult,
    DesktopMeta,
    ExecutionPolicy,
    PresenceInput,
    TerminalMessage,
)
from agent.cosmos.services import (
    AgentRegistryService,
    AgentRuntimeService,
    ConversationService,
    PresenceService,
    TerminalService,
)
from agent.cosmos.transport import BusService, MemoryUserChannel
from core.config import Settings
from core.decision.service import DecisionService
from core.llm.service import LLMService
from core.service import ClockService


class FakeLLM:
    async def chat(self, _messages: object, **_kwargs: object) -> str:
        return "摘要"


def make_card(agent_id: str, capability: str) -> AgentCard:
    return AgentCard(
        id=agent_id,
        name="编辑台" if agent_id == "editor" else "回声台",
        summary="测试用",
        description="用于终端测试的最小卡片。",
        capabilities=[capability],
        params_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        desktop=DesktopMeta(),
        execution=ExecutionPolicy(queue=True),
    )


async def ok_handler(_request: AgentRequest, _ctx: AgentContext) -> AgentResult:
    return AgentResult.ok(summary="完成")


@dataclass
class Env:
    terminal: TerminalService
    runtime: AgentRuntimeService
    registry: AgentRegistryService
    channel: MemoryUserChannel

    async def close(self) -> None:
        await self.terminal.stop()
        await self.runtime.stop()


@pytest.fixture
async def env(
    settings: Settings,
    clock: ClockService,
    bus: BusService,
    tmp_path: Path,
) -> AsyncIterator[Env]:
    settings.agent.cosmos.contract.state_dir = str(tmp_path)
    llm = FakeLLM()
    registry = AgentRegistryService(settings.agent)
    runtime = AgentRuntimeService(registry, bus, clock, settings.agent)
    await runtime.start()
    conversation = ConversationService(
        cast("LLMService", cast("object", llm)), clock, settings.agent
    )
    presence = PresenceService(
        clock,
        cast("DecisionService", cast("object", None)),
        cast("LLMService", cast("object", llm)),
        bus,
        settings.agent,
    )
    terminal = TerminalService(conversation, presence, runtime, bus, clock, settings.agent)
    channel = MemoryUserChannel()
    terminal.set_user_channel(channel)
    await terminal.start()
    registry.register(make_card("echo", "echo"), ok_handler)
    registry.register(make_card("editor", "code_edit"), ok_handler)
    env = Env(terminal, runtime, registry, channel)
    yield env
    await env.close()


def user_message(text: str = "在吗") -> TerminalMessage:
    return TerminalMessage(
        direction="user_to_aliya",
        kind="chat",
        text=text,
        at=datetime(2026, 11, 7, 21, 0, tzinfo=UTC),
        session_id="ses_1",
    )


async def test_纯聊天信封与发布(env: Env, bus: BusService) -> None:
    iterator = bus.messages.subscribe("aliya")
    try:
        envelope = await env.terminal.ingest(user_message("在吗，今天有点累"))
        assert envelope.presence is not None
        received = await asyncio.wait_for(anext(iterator), 1.0)
        assert received.message.text == "在吗，今天有点累"
    finally:
        await iterator.aclose()


async def test_连发短句逐条投递(env: Env) -> None:
    for text in ("嗯。", "在啊。", "怎么了？"):
        await env.terminal.deliver(text, session_id="ses_1", kind="chat")
    messages = env.channel.drain()
    assert [message.text for message in messages] == ["嗯。", "在啊。", "怎么了？"]


async def test_超长文本分段并标记(env: Env, settings: Settings) -> None:
    settings.agent.cosmos.desktop.max_content_chars = 20
    text = "这是第一句。" * 6
    await env.terminal.deliver(text, session_id="ses_1", kind="chat")
    messages = env.channel.drain()
    assert len(messages) > 1
    assert messages[0].meta.part_index == 1
    assert messages[0].meta.part_total == len(messages)
    assert "".join(message.text for message in messages) == text


async def test_主动消息间隔与每日上限(env: Env, settings: Settings) -> None:
    settings.agent.cosmos.terminal.proactive.min_interval_seconds = 0
    settings.agent.cosmos.terminal.proactive.daily_limit = 1

    first = await env.terminal.send_proactive("在吗")
    assert first.accepted is True
    second = await env.terminal.send_proactive("又想起一件事")
    assert second.accepted is False
    assert second.reason == "daily_limit"


async def test_最小间隔拒绝(env: Env, settings: Settings) -> None:
    settings.agent.cosmos.terminal.proactive.min_interval_seconds = 3600
    assert (await env.terminal.send_proactive("在吗")).accepted is True
    receipt = await env.terminal.send_proactive("再说一句")
    assert receipt.reason == "too_frequent"


async def test_用户输入中延后投递(env: Env, settings: Settings) -> None:
    settings.agent.cosmos.terminal.proactive.min_interval_seconds = 0
    _ = await env.terminal.ingest(user_message("在吗"))
    _ = await env.terminal._presence.apply(  # pyright: ignore[reportPrivateUsage]
        PresenceInput(online="online", is_typing=True)
    )
    receipt = await env.terminal.send_proactive("说点啥")
    assert receipt.accepted is False
    assert receipt.reason == "user_active"
    assert receipt.deliver_after is not None


async def test_用户发言取消待发主动消息(env: Env, settings: Settings) -> None:
    settings.agent.cosmos.terminal.proactive.min_interval_seconds = 0
    settings.agent.cosmos.terminal.proactive.cancel_window_seconds = 0.05
    _ = await env.terminal.ingest(user_message("在吗"))
    receipt = await env.terminal.send_proactive("想说一句")
    assert receipt.accepted is True
    assert receipt.deliver_after is not None

    _ = await env.terminal.ingest(user_message("我又来了"))
    await asyncio.sleep(0.15)
    assert env.channel.drain() == []


async def test_链路中断进队列恢复后重放(env: Env) -> None:
    await env.terminal.set_link_state("down")
    await env.terminal.deliver("在啊", session_id="ses_1", kind="chat")
    assert env.channel.drain() == []

    await env.terminal.set_link_state("up")
    messages = env.channel.drain()
    assert [message.text for message in messages] == ["在啊"]


async def test_历史不含进度与结果(env: Env) -> None:
    await env.terminal.deliver("在啊", session_id="ses_1", kind="chat")
    _ = await env.terminal.notify("还在弄")
    await env.terminal.deliver("改好了", session_id="ses_1", kind="result")

    messages = await env.terminal.history("ses_1")
    assert [message.text for message in messages] == ["在啊"]
    assert all(message.kind not in {"progress", "result"} for message in messages)


async def test_系统提示不进对话历史与统计(env: Env) -> None:
    """失败提示以 system 投递：用户看得到，但不算 Aliya 说过的话。"""
    await env.terminal.deliver("回复失败，请稍后再试。", session_id="ses_1", kind="system")

    messages = env.channel.drain()
    assert [message.kind for message in messages] == ["system"]
    assert await env.terminal.history("ses_1") == []
    assert env.terminal.last_aliya_at() is None


async def test_免打扰时段进入延迟队列(env: Env, settings: Settings) -> None:
    settings.agent.cosmos.terminal.proactive.quiet_hours = ["00:00-23:59"]
    receipt = await env.terminal.notify("还在弄")
    assert receipt.accepted is False
    assert receipt.reason == "quiet_hours"
    assert receipt.deliver_after is not None
    assert env.channel.drain() == []

    proactive = await env.terminal.send_proactive("说点啥")
    assert proactive.reason == "quiet_hours"


async def test_非法免打扰窗口被忽略(env: Env, settings: Settings) -> None:
    settings.agent.cosmos.terminal.proactive.quiet_hours = ["bad-window"]
    receipt = await env.terminal.notify("还在弄")
    assert receipt.accepted is True
    assert env.channel.drain()[0].text == "还在弄"


async def test_输入中通知延后投递(env: Env, settings: Settings) -> None:
    settings.agent.cosmos.terminal.proactive.input_delay_seconds = 0.02
    _ = await env.terminal._presence.apply(  # pyright: ignore[reportPrivateUsage]
        PresenceInput(online="online", is_typing=True)
    )
    receipt = await env.terminal.notify("还在弄")
    assert receipt.reason == "user_active"

    await asyncio.sleep(0.08)
    assert [message.text for message in env.channel.drain()] == ["还在弄"]


async def test_链路观察者与积压深度(env: Env) -> None:
    states: list[str] = []
    env.terminal.set_link_observer(states.append)
    await env.terminal.set_link_state("down")
    await env.terminal.deliver("在啊", session_id="ses_1", kind="chat")
    assert env.terminal.queue_depth() == (0, 1)
    await env.terminal.set_link_state("up")
    assert states == ["down", "up"]
    assert env.terminal.queue_depth() == (0, 0)


async def test_历史限制条数(env: Env) -> None:
    for index in range(5):
        await env.terminal.deliver(f"第 {index} 条。", session_id="ses_1", kind="chat")
    messages = await env.terminal.history("ses_1", limit=2)
    assert [message.text for message in messages] == ["第 3 条。", "第 4 条。"]


async def test_未回应降频(env: Env, settings: Settings) -> None:
    settings.agent.cosmos.terminal.proactive.min_interval_seconds = 10
    settings.agent.cosmos.terminal.proactive.unanswered_backoff = True
    env.terminal._proactive_unanswered = 2  # pyright: ignore[reportPrivateUsage]
    assert env.terminal._effective_interval() == 20  # pyright: ignore[reportPrivateUsage]


async def test_损坏计数文件不阻断(env: Env) -> None:
    path = env.terminal._proactive_path()  # pyright: ignore[reportPrivateUsage]
    _ = path.write_text("{bad", encoding="utf-8")
    env.terminal._load_proactive()  # pyright: ignore[reportPrivateUsage]
    assert env.terminal._proactive_count == 0  # pyright: ignore[reportPrivateUsage]


async def test_重复停止不报错(env: Env) -> None:
    await env.terminal.stop()
    await env.terminal.stop()


async def test_用户消息发布_active_事件(env: Env, bus: BusService) -> None:
    iterator = bus.events.subscribe(["user.active"])
    try:
        _ = await env.terminal.ingest(user_message("在吗"))
        event = await asyncio.wait_for(anext(iterator), 1.0)
        assert event.topic == "user.active"
        assert event.payload["session_id"] == "ses_1"
    finally:
        await iterator.aclose()


async def test_空文本不投递(env: Env) -> None:
    await env.terminal.deliver("   ", session_id="ses_1", kind="chat")
    assert env.channel.drain() == []


async def test_链路中断时用户消息排队后重放(env: Env, bus: BusService) -> None:
    await env.terminal.set_link_state("down")
    _ = await env.terminal.ingest(user_message("在吗"))
    assert env.terminal.queue_depth() == (1, 0)

    # 恢复时没有订阅者：消息进总线积压，随后订阅仍能拿到
    await env.terminal.set_link_state("up")
    iterator = bus.messages.subscribe("aliya")
    try:
        received = await asyncio.wait_for(anext(iterator), 1.0)
        assert received.message.text == "在吗"
        assert env.terminal.queue_depth() == (0, 0)
    finally:
        await iterator.aclose()


async def test_历史画像与主动回复(env: Env, settings: Settings) -> None:
    settings.agent.cosmos.terminal.proactive.min_interval_seconds = 0
    settings.agent.cosmos.terminal.proactive.cancel_window_seconds = 0
    _ = await env.terminal.ingest(user_message("在吗"))
    await env.terminal.deliver("在啊", session_id="ses_1", kind="chat")
    _ = await env.terminal.ingest(user_message("嗯"))

    pattern = env.terminal.history_pattern()
    assert pattern.sample_days == 1
    assert pattern.active_hours
    assert pattern.response_latency_p50_seconds is not None

    receipt = await env.terminal.send_proactive("说点啥")
    assert receipt.accepted is True
    _ = await env.terminal.ingest(user_message("怎么了"))
    pattern = env.terminal.history_pattern()
    assert pattern.proactive_sent >= 1
    assert pattern.proactive_replied >= 1
    assert pattern.last_proactive_minutes_ago is not None


async def test_小时打扰预算(env: Env, settings: Settings) -> None:
    settings.agent.cosmos.terminal.proactive.min_interval_seconds = 0
    settings.agent.cosmos.terminal.proactive.hourly_limit = 1
    settings.agent.cosmos.terminal.proactive.daily_limit = 5

    assert (await env.terminal.send_proactive("第一条")).accepted is True
    second = await env.terminal.send_proactive("第二条")
    assert second.accepted is False
    assert second.reason == "hourly_limit"

    urgent = await env.terminal.send_proactive("急事", urgency=0.9)
    assert urgent.accepted is True


async def test_统计可写盘并重读(env: Env) -> None:
    _ = await env.terminal.ingest(user_message("在吗"))
    path = env.terminal._stats_path  # pyright: ignore[reportPrivateUsage]
    assert path.exists()

    env.terminal._stats.clear()  # pyright: ignore[reportPrivateUsage]
    env.terminal._load_stats()  # pyright: ignore[reportPrivateUsage]
    pattern = env.terminal.history_pattern()
    assert pattern.sample_days == 1


async def test_出站文本实际遮蔽内部标识(env: Env) -> None:
    await env.terminal.deliver("任务 tsk_01H 完成了", session_id="ses_1", kind="result")
    messages = env.channel.drain()
    assert len(messages) == 1
    assert "[任务标识]" in messages[0].text
    assert "tsk_01H" not in messages[0].text


class _FlakyChannel:
    def __init__(self, failures: int) -> None:
        self.failures: int = failures
        self.calls: int = 0
        self.sent: list[TerminalMessage] = []

    async def send(self, message: TerminalMessage) -> None:
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("通道故障")
        self.sent.append(message)


async def test_投递失败会重试(env: Env, settings: Settings) -> None:
    settings.agent.cosmos.terminal.delivery.retry = 2
    settings.agent.cosmos.terminal.delivery.retry_backoff_seconds = 0
    channel = _FlakyChannel(failures=2)
    env.terminal.set_user_channel(channel)

    await env.terminal.deliver("在吗", session_id="ses_1")
    assert channel.calls == 3
    assert len(channel.sent) == 1
    assert env.terminal.dead_letters() == []


async def test_重试耗尽写死信(env: Env, settings: Settings) -> None:
    settings.agent.cosmos.terminal.delivery.retry = 1
    settings.agent.cosmos.terminal.delivery.retry_backoff_seconds = 0
    channel = _FlakyChannel(failures=99)
    env.terminal.set_user_channel(channel)

    await env.terminal.deliver("在吗", session_id="ses_1")
    letters = env.terminal.dead_letters()
    assert len(letters) == 1
    assert letters[0].text == "在吗"
    path = Path(settings.agent.cosmos.contract.state_dir) / "dead_letters.jsonl"
    assert path.exists()
    assert "在吗" in path.read_text(encoding="utf-8")


async def test_链路心跳恢复(env: Env) -> None:
    await env.terminal.set_link_state("down")
    assert env.terminal.link_state() == "down"
    await env.terminal.note_link_heartbeat()
    assert env.terminal.link_state() == "up"


async def test_心跳超时判断链后用户发言自动恢复并重放(
    env: Env, settings: Settings, bus: BusService
) -> None:
    settings.agent.cosmos.link.heartbeat_seconds = 1
    settings.agent.cosmos.link.down_after_seconds = 1
    await env.terminal.stop()
    await env.terminal.start()

    iterator = bus.messages.subscribe("aliya")
    try:
        _ = await env.terminal.ingest(user_message("第一句"))
        assert (await asyncio.wait_for(anext(iterator), 1.0)).message.text == "第一句"

        await asyncio.sleep(2.2)
        assert env.terminal.link_state() == "down"

        await env.terminal.deliver("断链期的回复", session_id="ses_1", kind="chat")
        assert env.channel.drain() == []
        assert env.terminal.queue_depth() == (0, 1)

        _ = await env.terminal.ingest(user_message("恢复后的一句"))

        assert [message.text for message in env.channel.drain()] == ["断链期的回复"]
        assert (await asyncio.wait_for(anext(iterator), 1.0)).message.text == "恢复后的一句"
        assert env.terminal.link_state() == "up"
        assert env.terminal.queue_depth() == (0, 0)
    finally:
        await iterator.aclose()


async def test_终端可接收通道状态上报(env: Env) -> None:
    snapshot = await env.terminal.report_presence(
        PresenceInput(online="online", is_typing=True, device="mobile")
    )
    assert snapshot.fields.typing.is_typing is True
    assert snapshot.fields.env.device == "mobile"
