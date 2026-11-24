"""控制面：协商、订阅、桌面与调度转发、权限边界与超时。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

import agent.cosmos.services.control as control_module
from agent.cosmos.contracts import (
    AgentCard,
    AgentContext,
    AgentRequest,
    AgentResult,
    AliyaContext,
    DesktopLayoutPatch,
    DesktopMeta,
    ExecutionPolicy,
    PresenceInput,
    TerminalMessage,
)
from agent.cosmos.errors import ContractVersionError, CosmosError
from agent.cosmos.services import (
    AgentRegistryService,
    AgentRuntimeService,
    ControlService,
    ConversationService,
    DesktopService,
    DispatchService,
    PresenceService,
    TerminalService,
    WorldBridgeService,
)
from agent.cosmos.transport import BusService, MemoryUserChannel
from core.config import Settings
from core.decision.service import DecisionService
from core.llm.service import LLMService
from core.service import ClockService


class FakeLLM:
    async def chat(self, _messages: object, **_kwargs: object) -> str:
        return "摘要"


async def ok_handler(_request: AgentRequest, _ctx: AgentContext) -> AgentResult:
    return AgentResult.ok(summary="完成")


def make_card() -> AgentCard:
    return AgentCard(
        id="echo",
        name="回声台",
        summary="测试用",
        description="用于控制面测试的最小卡片。",
        capabilities=["echo"],
        params_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        desktop=DesktopMeta(),
        execution=ExecutionPolicy(queue=True),
    )


class Env:
    def __init__(self) -> None:
        self.control: ControlService
        self.terminal: TerminalService
        self.runtime: AgentRuntimeService
        self.registry: AgentRegistryService
        self.desktop: DesktopService
        self.world: WorldBridgeService
        self.channel: MemoryUserChannel

    async def close(self) -> None:
        await self.desktop.stop()
        await self.world.stop()
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
    dispatch = DispatchService(
        registry,
        runtime,
        conversation,
        presence,
        cast("LLMService", cast("object", llm)),
        bus,
        clock,
        settings.agent,
    )
    desktop = DesktopService(registry, runtime, bus, clock, settings.agent)
    await desktop.start()
    world = WorldBridgeService(clock, presence, runtime, registry, desktop, bus, settings.agent)
    await world.start()
    terminal = TerminalService(conversation, presence, runtime, bus, clock, settings.agent)
    channel = MemoryUserChannel()
    terminal.set_user_channel(channel)
    await terminal.start()
    registry.register(make_card(), ok_handler)

    control = ControlService(
        terminal, desktop, dispatch, presence, world, runtime, bus, clock, settings.agent
    )
    result = Env()
    result.control = control
    result.terminal = terminal
    result.runtime = runtime
    result.registry = registry
    result.desktop = desktop
    result.world = world
    result.channel = channel
    yield result
    await result.close()


def user_message(text: str = "在吗") -> TerminalMessage:
    return TerminalMessage(
        direction="user_to_aliya",
        kind="chat",
        text=text,
        at=datetime(2026, 11, 7, 21, 0, tzinfo=UTC),
        session_id="ses_1",
    )


async def test_协商返回十个主题(env: Env) -> None:
    caps = await env.control.handshake()
    assert len(caps.event_topics) == 10
    assert caps.api_version == "1.0"
    assert caps.limits["max_parallel"] == 3
    assert "presence.mood" in caps.features
    assert "world" not in caps.features


async def test_能力随配置变化(env: Env, settings: Settings) -> None:
    settings.agent.cosmos.world.enabled = True
    settings.agent.cosmos.dispatch.mode = "auto"
    caps = await env.control.handshake()
    assert "world" in caps.features
    assert "dispatch.auto" in caps.features


async def test_契约主版本不一致报错(env: Env, settings: Settings) -> None:
    settings.agent.cosmos.contract.contract_version = "2.0.0"
    with pytest.raises(ContractVersionError):
        _ = await env.control.handshake()


async def test_入站流能收到信封(env: Env) -> None:
    stream = env.control.inbound()
    try:
        envelope = await env.terminal.ingest(user_message())
        received = await asyncio.wait_for(anext(stream), 1.0)
        assert received.message.text == envelope.message.text
    finally:
        await stream.aclose()


async def test_订阅非法主题报错(env: Env) -> None:
    with pytest.raises(ValueError):
        _ = env.control.subscribe(["nope"])


async def test_订阅合法主题可收到事件(env: Env, bus: BusService) -> None:
    stream = env.control.subscribe(["presence.changed"])
    try:
        _ = await bus.events.publish("presence.changed", {"revision": 1})
        event = await asyncio.wait_for(anext(stream), 1.0)
        assert event.topic == "presence.changed"
    finally:
        await stream.aclose()


async def test_桌面转发(env: Env) -> None:
    snapshot = await env.control.describe_desktop()
    assert [entry.card.id for entry in snapshot.entries] == ["echo"]

    detail = await env.control.get_agent("echo")
    assert detail.card.name == "回声台"

    patched = await env.control.patch_layout(DesktopLayoutPatch(pin={"echo": True}))
    assert patched.revision == 1
    assert patched.entries[0].card.desktop.pinned is False  # 卡片元数据不变，布局覆盖在排序里


async def test_显式调用返回任务句柄(env: Env) -> None:
    handle = await env.control.invoke_agent("echo", {"text": "hi"}, session_id="ses_1")
    assert handle.task_id.startswith("tsk_")
    result = await env.runtime.wait(handle.task_id)
    assert result.status == "ok"


async def test_消息转发到终端(env: Env, settings: Settings) -> None:
    settings.agent.cosmos.terminal.proactive.min_interval_seconds = 0
    await env.control.deliver("在啊", session_id="ses_1")
    assert env.channel.drain()[0].text == "在啊"

    receipt = await env.control.notify("还在弄")
    assert receipt.accepted is True
    assert env.channel.drain()[0].text == "还在弄"

    proactive = await env.control.send_proactive("说点啥")
    assert proactive.accepted is True
    assert env.channel.drain()[0].text == "说点啥"


async def test_状态读取转发(env: Env) -> None:
    snapshot = env.control.get_presence()
    assert snapshot.ttl_seconds == 30
    await env.control.set_presence_visibility(["mood"])
    assert await env.control.get_link_state() == "up"
    assert await env.control.get_world_view() is None
    await env.control.set_aliya_context(AliyaContext(phase="daily"))


async def test_转发超时抛可重试错误(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    async def slow() -> object:
        await asyncio.sleep(0.05)
        return object()

    monkeypatch.setitem(control_module.TIMEOUTS, "describe_desktop", 0.001)
    monkeypatch.setattr(env.control._desktop, "describe", slow)  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(CosmosError) as info:
        _ = await env.control.describe_desktop()
    assert info.value.code == "timeout"
    assert info.value.retriable is True


async def test_参数不足时显式调用报错(env: Env) -> None:
    from agent.cosmos.errors import AgentInvokeError

    with pytest.raises(AgentInvokeError):
        _ = await env.control.invoke_agent("echo", {}, session_id="ses_1")


async def test_事件缺口过大改推快照(env: Env, bus: BusService) -> None:
    for index in range(101):
        _ = await bus.events.publish("presence.changed", {"index": index})
    stream = env.control.subscribe(["presence.changed"], since_sequence=0)
    try:
        event = await asyncio.wait_for(anext(stream), 1.0)
        assert event.sequence == 0
        assert "snapshot" in event.payload
    finally:
        await stream.aclose()


async def test_打开agent只读详情(env: Env) -> None:
    detail = await env.control.open_agent("echo")
    assert detail.card.id == "echo"


async def test_控制面可上报通道状态(env: Env) -> None:
    snapshot = await env.control.report_presence(PresenceInput(online="online", is_typing=True))
    assert snapshot.fields.typing.is_typing is True
