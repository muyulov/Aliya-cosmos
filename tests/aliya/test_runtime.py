import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent.aliya.runtime import AliyaRuntime
from agent.cosmos.contracts import (
    AliyaContext,
    ConversationContext,
    CosmosCapabilities,
    CosmosEvent,
    DesktopSnapshot,
    InboundEnvelope,
    TerminalMessage,
)
from core.config.settings import (
    AgentAliyaMemorySettings,
    AgentAliyaReplySettings,
    AgentAliyaSettings,
)
from core.llm import AssistantReply


class FakeControl:
    def __init__(self) -> None:
        self.inbound_queue: asyncio.Queue[InboundEnvelope] = asyncio.Queue()
        self.delivered: list[str] = []
        self.contexts: list[AliyaContext] = []

    async def handshake(self) -> CosmosCapabilities:
        return CosmosCapabilities(api_version="1.0", contract_version="1.0.0")

    async def inbound(self) -> AsyncIterator[InboundEnvelope]:
        while True:
            yield await self.inbound_queue.get()

    async def subscribe(
        self, topics: list[str], *, since_sequence: int | None = None
    ) -> AsyncIterator[CosmosEvent]:
        if False:
            yield CosmosEvent(
                event_id="e0",
                topic="agent.result",
                sequence=0,
                at=datetime.now(UTC),
            )

    async def deliver(self, text: str, *, session_id: str, kind: str = "chat") -> None:
        self.delivered.append(text)

    async def set_aliya_context(self, ctx: AliyaContext) -> None:
        self.contexts.append(ctx)

    async def describe_desktop(self) -> DesktopSnapshot:
        return DesktopSnapshot(generated_at=datetime.now(UTC), summary="桌面")

    async def list_tasks(self, agent_id: str | None = None, limit: int = 20) -> list:
        return []

    async def cancel_task(self, task_id: str) -> None:
        return None

    async def retry_task(self, task_id: str) -> None:
        return None

    async def refresh_world(self) -> None:
        return None


class FakeLLM:
    def __init__(self, content: str = "在。", delay: float = 0.0) -> None:
        self.content = content
        self.delay = delay

    async def chat_tools(self, messages: object, tools: object) -> AssistantReply:
        if self.delay:
            await asyncio.sleep(self.delay)
        return AssistantReply(content=self.content, tool_calls=())


def make_envelope(message_id: str) -> InboundEnvelope:
    return InboundEnvelope(
        message=TerminalMessage(
            message_id=message_id,
            direction="user_to_aliya",
            kind="chat",
            text="在吗",
            session_id="s1",
        ),
        conversation=ConversationContext(session_id="s1"),
    )


def make_config(tmp_path: Path, *, timeout: float = 120) -> AgentAliyaSettings:
    return AgentAliyaSettings(
        memory=AgentAliyaMemorySettings(dir=str(tmp_path)),
        reply=AgentAliyaReplySettings(turn_timeout_seconds=timeout),
    )


@pytest.mark.asyncio
async def test_inbound_turn_is_serial(tmp_path: Path) -> None:
    control = FakeControl()
    runtime = AliyaRuntime(
        control=control,  # type: ignore[arg-type]
        llm=FakeLLM(),  # type: ignore[arg-type]
        config=make_config(tmp_path),
    )
    await runtime.start()
    try:
        control.inbound_queue.put_nowait(make_envelope("m1"))
        await asyncio.wait_for(runtime.wait_idle(), timeout=2)
        assert control.delivered == ["在。"]
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_turn_timeout_falls_back(tmp_path: Path) -> None:
    control = FakeControl()
    runtime = AliyaRuntime(
        control=control,  # type: ignore[arg-type]
        llm=FakeLLM(delay=1.0),  # type: ignore[arg-type]
        config=make_config(tmp_path, timeout=0.05),
    )
    await runtime.start()
    try:
        control.inbound_queue.put_nowait(make_envelope("m1"))
        await asyncio.wait_for(runtime.wait_idle(), timeout=3)
        assert control.delivered == [
            AgentAliyaSettings().reply.fallback_text
        ]
    finally:
        await runtime.stop()
