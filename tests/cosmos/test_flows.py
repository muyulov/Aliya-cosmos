"""五条端到端流程：全部走真实服务，只替换端口与用户通道。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from agent.cosmos.assembly import build_cosmos_manager
from agent.cosmos.contracts import (
    ActionWindow,
    AliyaContext,
    CosmosEvent,
    DispatchIntent,
    Hypothesis,
    PresenceInput,
    TerminalMessage,
    WorldInference,
    WorldInput,
)
from agent.cosmos.services import (
    AgentRuntimeService,
    ControlService,
    DesktopService,
    PresenceService,
    TerminalService,
    WorldBridgeService,
)
from agent.cosmos.transport import MemoryUserChannel
from core.config import (
    AgentCardConfig,
    AgentRegistrationSettings,
    Settings,
)
from core.service import ServiceManager


class FakeWorldPort:
    """固定返回一份合法推演。"""

    def __init__(self) -> None:
        self.calls: int = 0

    async def infer(self, payload: WorldInput) -> WorldInference:
        self.calls += 1
        return WorldInference(
            inference_id="inf_flow",
            generated_at=payload.generated_at,
            snapshot="你离线 2 小时了，这个点你通常在线",
            hypotheses=[
                Hypothesis(statement="可能临时走开了", confidence=0.5, basis="没有消息"),
                Hypothesis(statement="可能已经休息", confidence=0.3, basis="时间不早了"),
            ],
            action_windows=[
                ActionWindow(
                    window="接下来 30 分钟", suggestion="适合留一句轻松的话", rationale="习惯时段"
                )
            ],
            signals_used=["time", "presence", "history"],
            confidence=0.6,
        )


@dataclass
class FlowEnv:
    manager: ServiceManager
    control: ControlService
    terminal: TerminalService
    runtime: AgentRuntimeService
    desktop: DesktopService
    presence: PresenceService
    world: WorldBridgeService
    channel: MemoryUserChannel
    events: list[CosmosEvent] = field(default_factory=list)
    _consumer: asyncio.Task[None] | None = None

    async def start_collector(self, bus_topics: list[str]) -> None:
        stream = self.control.subscribe(bus_topics)

        async def run() -> None:
            async for event in stream:
                self.events.append(event)

        self._consumer = asyncio.create_task(run())
        await asyncio.sleep(0)

    async def wait_for(self, topic: str, timeout: float = 2.0) -> CosmosEvent:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            for event in self.events:
                if event.topic == topic:
                    return event
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError(f"等待 {topic} 超时")
            await asyncio.sleep(0.005)

    async def stop_collector(self) -> None:
        if self._consumer is not None:
            _ = self._consumer.cancel()
            _ = await asyncio.gather(self._consumer, return_exceptions=True)
            self._consumer = None


def build_settings(tmp_path: Path) -> Settings:
    settings = Settings()
    settings.agent.cosmos.contract.state_dir = str(tmp_path)
    settings.agent.cosmos.presence.push.typing_settle_seconds = 0.05
    settings.agent.cosmos.world.enabled = True
    settings.agent.cosmos.agents = [
        AgentRegistrationSettings(
            id="echo",
            module="agent.cosmos.registry.echo",
            handler="handle",
            card=AgentCardConfig(
                name="回声台",
                icon="🔁",
                summary="原样返回指令",
                description="联调用的最小工具，把收到的文本原样返回。",
                capabilities=["echo"],
                params_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            ),
        )
    ]
    return settings


@pytest.fixture
async def flow(tmp_path: Path) -> AsyncIterator[FlowEnv]:
    settings = build_settings(tmp_path)
    manager = build_cosmos_manager(settings)
    world = manager.get(WorldBridgeService)
    world.set_port(FakeWorldPort())
    channel = MemoryUserChannel()

    async with manager.lifespan():
        terminal = manager.get(TerminalService)
        terminal.set_user_channel(channel)
        env = FlowEnv(
            manager=manager,
            control=manager.get(ControlService),
            terminal=terminal,
            runtime=manager.get(AgentRuntimeService),
            desktop=manager.get(DesktopService),
            presence=manager.get(PresenceService),
            world=world,
            channel=channel,
        )
        await env.start_collector(
            [
                "agent.result",
                "agent.progress",
                "dispatch.finished",
                "world.inference",
                "presence.changed",
                "agent.state.changed",
            ]
        )
        yield env
        await env.stop_collector()


def user_message(text: str) -> TerminalMessage:
    return TerminalMessage(
        direction="user_to_aliya",
        kind="chat",
        text=text,
        at=datetime.now(UTC),
        session_id="ses_flow",
    )


ENGINEERING_WORDS = ("tsk_", "dsp_", "agent", "用户", "模型", "调度", "task_id")


def assert_no_engineering(text: str) -> None:
    for word in ENGINEERING_WORDS:
        assert word not in text


async def test_流程一_聊天收发(flow: FlowEnv) -> None:
    envelope = await flow.terminal.ingest(user_message("在吗，今天有点累"))
    assert envelope.presence is not None
    assert envelope.presence.summary

    await flow.terminal.deliver("在啊，怎么了？", session_id="ses_flow", kind="chat")
    messages = flow.channel.drain()
    assert [message.text for message in messages] == ["在啊，怎么了？"]
    assert_no_engineering(messages[0].text)


async def test_流程二_桌面点击调用(flow: FlowEnv) -> None:
    handle = await flow.control.invoke_agent("echo", {"text": "hi"}, session_id="ses_flow")
    assert handle.task_id.startswith("tsk_")
    event = await flow.wait_for("agent.result")
    payload = cast("dict[str, object]", event.payload["result"])
    assert payload["status"] == "ok"
    assert payload["summary"] == "收到：hi"

    await asyncio.sleep(0.02)
    snapshot = await flow.desktop.describe()
    assert snapshot.entries[0].status == "idle"


async def test_流程三_自然语言任务(flow: FlowEnv) -> None:
    plan = await flow.control.plan(
        DispatchIntent(
            source="natural_language",
            session_id="ses_flow",
            user_text="帮我回显一句话",
            params={"text": "一句话"},
        )
    )
    assert plan.decision == "single"

    handle = await flow.control.execute_plan(plan)
    assert len(handle.task_ids) == 1
    finished = await flow.wait_for("dispatch.finished")
    outcome = cast("dict[str, object]", finished.payload["outcome"])
    assert outcome["status"] == "ok"


async def test_流程四_世界推理与主动消息(flow: FlowEnv) -> None:
    inference = await flow.control.refresh_world()
    assert inference is not None
    event = await flow.wait_for("world.inference")
    assert event.payload["trigger"] in {"manual", "tick"}
    assert event.payload["stale"] is False

    receipt = await flow.control.send_proactive("害，突然想起一件事")
    assert receipt.accepted is True
    messages = flow.channel.drain()
    assert messages
    assert_no_engineering(messages[0].text)


async def test_流程五_状态变化推送(flow: FlowEnv) -> None:
    snapshot = await flow.presence.apply(PresenceInput(online="online", is_typing=True))
    assert snapshot.readable.summary == "你在线，正在输入"

    event = await flow.wait_for("presence.changed")
    payload = event.payload
    readable = cast("dict[str, object]", payload["readable"])
    assert "正在输入" in str(readable["summary"])
    assert readable["hint"]
    assert flow.channel.drain() == []


async def test_连发两条短句逐条投递(flow: FlowEnv) -> None:
    await flow.terminal.deliver("嗯。", session_id="ses_flow", kind="chat")
    await flow.terminal.deliver("在啊。", session_id="ses_flow", kind="chat")
    assert [message.text for message in flow.channel.drain()] == ["嗯。", "在啊。"]


async def test_链路中断进队列恢复后重放(flow: FlowEnv) -> None:
    await flow.terminal.set_link_state("down")
    await flow.terminal.deliver("在啊", session_id="ses_flow", kind="chat")
    assert flow.channel.drain() == []

    await flow.terminal.set_link_state("up")
    assert [message.text for message in flow.channel.drain()] == ["在啊"]


async def test_阿里亚上下文可写入(flow: FlowEnv) -> None:
    await flow.control.set_aliya_context(AliyaContext(phase="rest", open_topics=["排障"]))
    _ = await flow.world.refresh(force=True)
    assert (await flow.control.get_world_view()) is not None
