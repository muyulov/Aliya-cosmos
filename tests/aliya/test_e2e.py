import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent.aliya.memory.store import MemoryStore
from agent.aliya.runtime import AliyaRuntime
from agent.aliya.transport import RemoteControl
from agent.cosmos.contracts import (
    AliyaContext,
    ConversationContext,
    CosmosCapabilities,
    CosmosEvent,
    DesktopSnapshot,
    DispatchHandle,
    DispatchIntent,
    DispatchPlan,
    InboundEnvelope,
    PlannedTask,
    TerminalMessage,
)
from agent.cosmos.services.control_server import ControlServerService
from core.config import ClockSettings, Settings
from core.llm import AssistantReply, ToolCall
from core.service.clock_service import ClockService


class FakeControl:
    def __init__(self) -> None:
        self.inbound_queue: asyncio.Queue[InboundEnvelope] = asyncio.Queue()
        self.event_queue: asyncio.Queue[CosmosEvent] = asyncio.Queue()
        self.delivered: list[str] = []
        self.dispatched: list[str] = []
        self.contexts: list[AliyaContext] = []

    async def handshake(self) -> CosmosCapabilities:
        return CosmosCapabilities(api_version="1.0", contract_version="1.0.0")

    async def inbound(self) -> AsyncIterator[InboundEnvelope]:
        while True:
            yield await self.inbound_queue.get()

    async def subscribe(
        self, topics: list[str], *, since_sequence: int | None = None
    ) -> AsyncIterator[CosmosEvent]:
        while True:
            yield await self.event_queue.get()

    async def deliver(self, text: str, *, session_id: str, kind: str = "chat") -> None:
        self.delivered.append(text)

    async def set_aliya_context(self, ctx: AliyaContext) -> None:
        self.contexts.append(ctx)

    async def describe_desktop(self) -> DesktopSnapshot:
        return DesktopSnapshot(generated_at=datetime.now(UTC), summary="桌面")

    async def plan(self, intent: DispatchIntent) -> DispatchPlan:
        return DispatchPlan(
            decision="single",
            tasks=[
                PlannedTask(
                    agent_id="echo",
                    params={"text": intent.user_text or ""},
                    label="查看登录",
                )
            ],
            rationale="ok",
            session_id=intent.session_id,
        )

    async def execute_plan(self, plan: DispatchPlan) -> DispatchHandle:
        self.dispatched.extend(task.agent_id for task in plan.tasks)
        return DispatchHandle(
            dispatch_id="d1",
            task_ids=["t1"],
            accepted_at=datetime.now(UTC),
        )

    async def list_tasks(self, agent_id: str | None = None, limit: int = 20) -> list:
        return []

    async def cancel_task(self, task_id: str) -> None:
        return None

    async def retry_task(self, task_id: str) -> None:
        return None

    async def refresh_world(self) -> None:
        return None

    async def push_event(self, topic: str, summary: str) -> None:
        self.event_queue.put_nowait(
            CosmosEvent(
                event_id=f"e-{topic}",
                topic=topic,
                sequence=1,
                at=datetime.now(UTC),
                payload={"summary": summary},
            )
        )


class ScriptedLLM:
    def __init__(self, replies: list[AssistantReply]) -> None:
        self.replies = list(replies)

    async def chat_tools(self, messages: object, tools: object) -> AssistantReply:
        if not self.replies:
            return AssistantReply(content="嗯。", tool_calls=())
        return self.replies.pop(0)


class E2E:
    def __init__(
        self,
        control: FakeControl,
        client: RemoteControl,
        runtime: AliyaRuntime,
    ) -> None:
        self.control = control
        self.client = client
        self.runtime = runtime

    @property
    def last_reply(self) -> str:
        return self.control.delivered[-1] if self.control.delivered else ""

    @property
    def dispatched(self) -> list[str]:
        return self.control.dispatched

    async def user_says(self, text: str) -> None:
        self.control.inbound_queue.put_nowait(make_envelope(text))
        await self.runtime.wait_idle()

    async def finish_task(self) -> None:
        await self.control.push_event("agent.result", "登录修好了")
        for _ in range(100):
            if "好了" in self.last_reply:
                return
            await asyncio.sleep(0.02)
        raise AssertionError(f"没有收到结果汇报：{self.control.delivered}")


def make_envelope(text: str) -> InboundEnvelope:
    return InboundEnvelope(
        message=TerminalMessage(
            message_id=f"m-{text}",
            direction="user_to_aliya",
            kind="chat",
            text=text,
            session_id="s1",
        ),
        conversation=ConversationContext(session_id="s1"),
    )


@pytest.fixture
async def e2e(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[E2E]:
    monkeypatch.setenv("ALIYA_CONTROL_TOKEN", "t")
    settings = Settings()
    settings.agent.control.socket_path = str(tmp_path / "aliya.sock")
    control = FakeControl()
    server = ControlServerService(
        control,  # type: ignore[arg-type]
        ClockService(ClockSettings()),
        settings.agent,
    )
    await server.start()
    client = RemoteControl(str(tmp_path / "aliya.sock"), token="t")
    llm = ScriptedLLM(
        [
            AssistantReply(
                content=None,
                tool_calls=(
                    ToolCall(
                        id="1",
                        name="dispatch_task",
                        arguments='{"task_text":"帮我看一下登录"}',
                    ),
                ),
            ),
            AssistantReply(content="我去看看。", tool_calls=()),
            AssistantReply(content="好了，登录能用了。", tool_calls=()),
        ]
    )
    runtime = AliyaRuntime(
        control=client,
        llm=llm,  # type: ignore[arg-type]
        config=settings.agent.aliya,
        memory=MemoryStore(tmp_path / "memory"),
    )
    await client.connect()
    await runtime.start()
    try:
        yield E2E(control, client, runtime)
    finally:
        await runtime.stop()
        await client.close()
        await server.stop()


@pytest.mark.asyncio
async def test_chat_dispatch_and_result(e2e: E2E) -> None:
    await e2e.user_says("帮我看一下登录")
    assert e2e.last_reply == "我去看看。"
    assert e2e.dispatched == ["echo"]
    await e2e.finish_task()
    assert "好了" in e2e.last_reply
