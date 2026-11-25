import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent.aliya.memory.store import MemoryStore
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
        self.acked: list[str] = []
        self.contexts: list[AliyaContext] = []
        self.since: int | None = None
        self.cursors: dict[str, int] | None = None

    async def handshake(self) -> CosmosCapabilities:
        return CosmosCapabilities(api_version="1.0", contract_version="1.0.0")

    async def inbound(self) -> AsyncIterator[InboundEnvelope]:
        while True:
            yield await self.inbound_queue.get()

    async def ack_inbound(self, message_id: str) -> None:
        self.acked.append(message_id)

    async def subscribe(
        self,
        topics: list[str],
        *,
        since_sequence: int | None = None,
        cursors: dict[str, int] | None = None,
    ) -> AsyncIterator[CosmosEvent]:
        self.since = since_sequence
        self.cursors = cursors
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
        self.messages: object = None

    async def chat_tools(self, messages: object, tools: object) -> AssistantReply:
        self.messages = messages
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
        assert control.acked == ["m1"]
        turns = MemoryStore(tmp_path).recent_turns()
        assert [turn.role for turn in turns] == ["user", "aliya"]
        assert turns[0].message_id == "m1"
        assert turns[1].message_id == "m1:reply"
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
        assert control.delivered == [AgentAliyaSettings().reply.fallback_text]
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_event_subscription_resumes_from_min_cursor(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    store.save_cursor(3, topic="agent.result")
    store.save_cursor(1, topic="presence.changed")

    control = FakeControl()
    runtime = AliyaRuntime(
        control=control,  # type: ignore[arg-type]
        llm=FakeLLM(),  # type: ignore[arg-type]
        config=make_config(tmp_path),
    )
    await runtime.start()
    try:
        for _ in range(50):
            if control.since is not None:
                break
            await asyncio.sleep(0.01)
        assert control.since == 1
        assert control.cursors == {"agent.result": 3, "presence.changed": 1}
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_long_term_memory_is_injected(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    store.save_summary("她最近在整理旧照片")
    store.save_anchors(["风信子"])
    store.save_open_topics(["海边的约定"])
    store.save_unspoken(["只对你说的话"])

    control = FakeControl()
    llm = FakeLLM()
    runtime = AliyaRuntime(
        control=control,  # type: ignore[arg-type]
        llm=llm,  # type: ignore[arg-type]
        config=make_config(tmp_path),
    )
    await runtime.start()
    try:
        control.inbound_queue.put_nowait(make_envelope("m1"))
        await asyncio.wait_for(runtime.wait_idle(), timeout=2)
        system = llm.messages[0]["content"]  # type: ignore[index]
        assert "她最近在整理旧照片" in system
        assert "风信子" in system
        assert "海边的约定" in system
        assert "只对你说的话" in system
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_startup_recovers_unanswered_turn(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    store.record_inbound("m0", "在吗", session_id="s1")

    control = FakeControl()
    runtime = AliyaRuntime(
        control=control,  # type: ignore[arg-type]
        llm=FakeLLM(),  # type: ignore[arg-type]
        config=make_config(tmp_path),
    )
    await runtime.start()
    try:
        await asyncio.wait_for(runtime.wait_idle(), timeout=2)
        assert control.delivered == ["在。"]
        assert control.acked == []
        turns = store.recent_turns()
        assert [turn.role for turn in turns] == ["user", "aliya"]
        assert turns[1].message_id == "m0:reply"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_replayed_persisted_turn_is_acked_once(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    store.record_inbound("m0", "在吗", session_id="s1")

    control = FakeControl()
    runtime = AliyaRuntime(
        control=control,  # type: ignore[arg-type]
        llm=FakeLLM(),  # type: ignore[arg-type]
        config=make_config(tmp_path),
    )
    await runtime.start()
    try:
        await asyncio.wait_for(runtime.wait_idle(), timeout=2)
        control.inbound_queue.put_nowait(make_envelope("m0"))
        for _ in range(50):
            if control.acked == ["m0"]:
                break
            await asyncio.sleep(0.01)
        assert control.acked == ["m0"]
        turns = store.recent_turns()
        assert len([turn for turn in turns if turn.role == "user"]) == 1
    finally:
        await runtime.stop()
