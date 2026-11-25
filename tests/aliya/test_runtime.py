import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent.aliya.memory.store import MemoryStore
from agent.aliya.runtime import AliyaRuntime, event_brief
from agent.aliya.turn.context import TurnContext
from agent.cosmos.contracts import (
    AgentProgress,
    AgentRuntimeState,
    AliyaContext,
    ConversationContext,
    CosmosCapabilities,
    CosmosEvent,
    DesktopSnapshot,
    DispatchPlan,
    InboundEnvelope,
    PresenceSnapshot,
    ProactiveReceipt,
    ReadablePresence,
    TerminalMessage,
    WorldInference,
)
from core.config.settings import (
    AgentAliyaInitiativeSettings,
    AgentAliyaMemorySettings,
    AgentAliyaPersonaSettings,
    AgentAliyaReplySettings,
    AgentAliyaSettings,
    AgentAliyaToolsSettings,
)
from core.llm import AssistantReply


class FakeControl:
    def __init__(self) -> None:
        self.inbound_queue: asyncio.Queue[InboundEnvelope] = asyncio.Queue()
        self.event_queue: asyncio.Queue[CosmosEvent] = asyncio.Queue()
        self.delivered: list[str] = []
        self.notified: list[str] = []
        self.proactive: list[str] = []
        self.acked: list[str] = []
        self.contexts: list[AliyaContext] = []
        self.since: int | None = None
        self.cursors: dict[str, int] | None = None
        self.intents: list[object] = []

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
        while True:
            yield await self.event_queue.get()

    async def deliver(self, text: str, *, session_id: str, kind: str = "chat") -> None:
        self.delivered.append(text)

    async def notify(self, text: str, *, dispatch_id: str | None = None) -> ProactiveReceipt:
        self.notified.append(text)
        return ProactiveReceipt(accepted=True, reason="ok")

    async def send_proactive(self, text: str, *, urgency: float = 0.5) -> ProactiveReceipt:
        self.proactive.append(text)
        return ProactiveReceipt(accepted=True, reason="ok")

    async def set_aliya_context(self, ctx: AliyaContext) -> None:
        self.contexts.append(ctx)

    async def describe_desktop(self) -> DesktopSnapshot:
        return DesktopSnapshot(generated_at=datetime.now(UTC), summary="桌面")

    async def plan(self, intent: object) -> DispatchPlan:
        self.intents.append(intent)
        return DispatchPlan(decision="reply_only", rationale="ok")

    async def list_tasks(self, agent_id: str | None = None, limit: int = 20) -> list:
        return []

    async def cancel_task(self, task_id: str) -> None:
        return None

    async def retry_task(self, task_id: str) -> None:
        return None

    async def refresh_world(self) -> None:
        return None


class StatefulControl(FakeControl):
    """启动时能返回桌面、世界与状态的控制面桩。"""

    def __init__(self) -> None:
        super().__init__()
        self.desktop = DesktopSnapshot(
            generated_at=datetime.now(UTC),
            summary="桌面上有两张卡",
        )
        self.world = WorldInference(
            inference_id="i1",
            generated_at=datetime.now(UTC),
            snapshot="夜里很安静",
            hypotheses=[
                {"statement": "她可能失眠", "confidence": 0.8, "basis": "时间"}
            ],
        )

    async def describe_desktop(self) -> DesktopSnapshot:
        return self.desktop

    async def get_world_view(self, force: bool = False) -> WorldInference | None:
        return self.world

    async def get_link_state(self) -> str:
        return "up"

    def get_presence(self) -> PresenceSnapshot:
        return PresenceSnapshot(
            captured_at=datetime.now(UTC),
            readable=ReadablePresence(summary="在电脑前"),
        )


class FakeLLM:
    def __init__(
        self,
        content: str = "在。",
        delay: float = 0.0,
        chat_reply: str = "滚动摘要",
    ) -> None:
        self.content = content
        self.delay = delay
        self.chat_reply = chat_reply
        self.messages: object = None
        self.chat_calls: list[object] = []

    async def chat_tools(self, messages: object, tools: object) -> AssistantReply:
        self.messages = messages
        if self.delay:
            await asyncio.sleep(self.delay)
        return AssistantReply(content=self.content, tool_calls=())

    async def chat(self, messages: object) -> str:
        self.chat_calls.append(messages)
        return self.chat_reply


def make_envelope(message_id: str, text: str = "在吗") -> InboundEnvelope:
    return InboundEnvelope(
        message=TerminalMessage(
            message_id=message_id,
            direction="user_to_aliya",
            kind="chat",
            text=text,
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
        assert control.delivered == ["在。"]
        assert control.acked == ["m1"]
        system = llm.messages[0]["content"]  # type: ignore[index]
        assert "send_message" in system
        assert "dispatch_task" in system
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


def test_event_brief_reads_nested_payloads() -> None:
    result = CosmosEvent(
        event_id="e1",
        topic="agent.result",
        sequence=1,
        at=datetime.now(UTC),
        payload={
            "agent_id": "echo",
            "task_id": "t1",
            "dispatch_id": "d1",
            "result": {"status": "ok", "summary": "登录修好了"},
        },
    )
    assert event_brief(result) == "任务完成：登录修好了"

    finished = CosmosEvent(
        event_id="e2",
        topic="dispatch.finished",
        sequence=1,
        at=datetime.now(UTC),
        payload={
            "outcome": {
                "status": "partial",
                "summary": "1 个完成，1 个失败",
                "results": [{"summary": "登录修好了"}],
            }
        },
    )
    assert "这一批部分完成：1 个完成，1 个失败" in event_brief(finished)
    assert "- 登录修好了" in event_brief(finished)

    abnormal = CosmosEvent(
        event_id="e3",
        topic="agent.abnormal",
        sequence=1,
        at=datetime.now(UTC),
        payload={
            "agent_id": "echo",
            "reason": "timeout",
            "error": {"message": "超过 60 秒", "user_hint": "可以重试"},
        },
    )
    assert "任务超时：超过 60 秒" in event_brief(abnormal)
    assert "提示：可以重试" in event_brief(abnormal)

    progress = CosmosEvent(
        event_id="e4",
        topic="agent.progress",
        sequence=1,
        at=datetime.now(UTC),
        payload={
            "task_id": "t1",
            "agent_id": "echo",
            "label": "整理照片",
            "progress": 0.4,
            "step": "正在筛选",
            "at": datetime.now(UTC).isoformat(),
        },
    )
    assert event_brief(progress) == "整理照片（40%）：正在筛选"


@pytest.mark.asyncio
async def test_world_inference_is_cached_and_injected(tmp_path: Path) -> None:
    control = FakeControl()
    llm = FakeLLM()
    runtime = AliyaRuntime(
        control=control,  # type: ignore[arg-type]
        llm=llm,  # type: ignore[arg-type]
        config=make_config(tmp_path),
    )
    await runtime.start()
    try:
        control.event_queue.put_nowait(
            CosmosEvent(
                event_id="e1",
                topic="world.inference",
                sequence=1,
                at=datetime.now(UTC),
                payload={
                    "inference": {
                        "inference_id": "i1",
                        "generated_at": datetime.now(UTC).isoformat(),
                        "snapshot": "夜里很安静",
                        "hypotheses": [
                            {
                                "statement": "她可能失眠",
                                "confidence": 0.8,
                                "basis": "时间",
                            }
                        ],
                        "action_windows": [
                            {
                                "window": "现在",
                                "suggestion": "轻声问候",
                                "rationale": "她还没睡",
                            }
                        ],
                    }
                },
            )
        )
        await asyncio.sleep(0.05)
        control.inbound_queue.put_nowait(make_envelope("m1"))
        await asyncio.wait_for(runtime.wait_idle(), timeout=2)
        system = llm.messages[0]["content"]  # type: ignore[index]
        assert "夜里很安静" in system
        assert "她可能失眠" in system
        assert "轻声问候" in system
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_event_report_uses_notify_with_real_payload(tmp_path: Path) -> None:
    control = FakeControl()
    llm = FakeLLM()
    runtime = AliyaRuntime(
        control=control,  # type: ignore[arg-type]
        llm=llm,  # type: ignore[arg-type]
        config=make_config(tmp_path),
    )
    await runtime.start()
    try:
        control.event_queue.put_nowait(
            CosmosEvent(
                event_id="e1",
                topic="agent.result",
                sequence=1,
                at=datetime.now(UTC),
                payload={
                    "agent_id": "echo",
                    "task_id": "t1",
                    "dispatch_id": "d1",
                    "result": {"status": "ok", "summary": "登录修好了"},
                },
            )
        )
        for _ in range(100):
            if control.notified:
                break
            await asyncio.sleep(0.01)
        await asyncio.wait_for(runtime.wait_idle(), timeout=2)
        user_messages = [
            message["content"]
            for message in llm.messages  # type: ignore[union-attr]
            if isinstance(message, dict) and message.get("role") == "user"
        ]
        assert "任务完成：登录修好了" in user_messages
        assert control.notified == ["在。"]
        assert control.delivered == []
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_start_primes_desktop_presence_and_world(tmp_path: Path) -> None:
    control = StatefulControl()
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
        assert "平板：桌面上有两张卡" in system
        assert "在电脑前" in system
        assert "夜里很安静" in system
    finally:
        await runtime.stop()


def test_event_caches_feed_task_and_desktop_context(tmp_path: Path) -> None:
    runtime = AliyaRuntime(
        control=FakeControl(),  # type: ignore[arg-type]
        llm=FakeLLM(),  # type: ignore[arg-type]
        config=make_config(tmp_path),
    )
    now = datetime.now(UTC)
    runtime._cache_event(
        CosmosEvent(
            event_id="e1",
            topic="agent.state.changed",
            sequence=1,
            at=now,
            payload=AgentRuntimeState(
                agent_id="echo",
                status="running",
                running=1,
            ).model_dump(mode="json"),
        )
    )
    runtime._cache_event(
        CosmosEvent(
            event_id="e2",
            topic="agent.progress",
            sequence=2,
            at=now,
            payload=AgentProgress(
                task_id="t1",
                agent_id="echo",
                label="整理照片",
                progress=0.4,
                step="正在筛选",
                at=now,
            ).model_dump(mode="json"),
        )
    )
    runtime._cache_event(
        CosmosEvent(
            event_id="e3",
            topic="desktop.layout.changed",
            sequence=3,
            at=now,
            payload=DesktopSnapshot(
                generated_at=now,
                summary="桌面很干净",
            ).model_dump(mode="json"),
        )
    )
    tasks = runtime._tasks_text()
    assert tasks is not None
    assert "正在运行 1 个" in tasks
    assert "整理照片（40%）：正在筛选" in tasks
    assert runtime._desktop_text() == "平板：桌面很干净"


@pytest.mark.asyncio
async def test_dispatch_hint_includes_tool_preferences(tmp_path: Path) -> None:
    control = FakeControl()
    runtime = AliyaRuntime(
        control=control,  # type: ignore[arg-type]
        llm=FakeLLM(),  # type: ignore[arg-type]
        config=AgentAliyaSettings(
            memory=AgentAliyaMemorySettings(dir=str(tmp_path)),
            tools=AgentAliyaToolsSettings(prefer_agents=["echo"], max_parallel_dispatch=1),
        ),
    )
    await runtime.dispatch("看看登录")
    intent = control.intents[0]
    assert intent.hint == "优先使用：echo；最多并行 1 个"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_outbound_check_and_limit_follow_config(tmp_path: Path) -> None:
    config = AgentAliyaSettings(
        memory=AgentAliyaMemorySettings(dir=str(tmp_path)),
        reply=AgentAliyaReplySettings(
            max_chars_per_message=5,
            post_checks=["narration"],
        ),
    )
    llm = FakeLLM(chat_reply="这是一句很长的改写结果")
    runtime = AliyaRuntime(
        control=FakeControl(),  # type: ignore[arg-type]
        llm=llm,  # type: ignore[arg-type]
        config=config,
    )
    prepared = await runtime._prepare_outbound("（笑）123456")
    assert len(prepared) == 5
    assert prepared.endswith("…")
    assert llm.chat_calls


@pytest.mark.asyncio
async def test_outbound_check_rewrites_before_sending(tmp_path: Path) -> None:
    llm = FakeLLM(chat_reply="好的")
    runtime = AliyaRuntime(
        control=FakeControl(),  # type: ignore[arg-type]
        llm=llm,  # type: ignore[arg-type]
        config=make_config(tmp_path),
    )
    prepared = await runtime._prepare_outbound("（笑）好啊")
    assert prepared == "好的"


@pytest.mark.asyncio
async def test_crisis_phase_is_reported(tmp_path: Path) -> None:
    control = FakeControl()
    runtime = AliyaRuntime(
        control=control,  # type: ignore[arg-type]
        llm=FakeLLM(),  # type: ignore[arg-type]
        config=make_config(tmp_path),
    )
    await runtime.start()
    try:
        control.inbound_queue.put_nowait(make_envelope("m1", "我不想活了"))
        await asyncio.wait_for(runtime.wait_idle(), timeout=2)
        assert control.contexts[-1].phase == "crisis"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_default_anchors_are_seeded(tmp_path: Path) -> None:
    runtime = AliyaRuntime(
        control=FakeControl(),  # type: ignore[arg-type]
        llm=FakeLLM(),  # type: ignore[arg-type]
        config=AgentAliyaSettings(
            memory=AgentAliyaMemorySettings(
                dir=str(tmp_path),
                anchors=["风信子", "拉姆石"],
            ),
        ),
    )
    await runtime.start()
    try:
        assert MemoryStore(tmp_path).load_anchors() == ["风信子", "拉姆石"]
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_ticker_requires_semantic_preference(tmp_path: Path) -> None:
    control = FakeControl()
    llm = FakeLLM()
    MemoryStore(tmp_path).save_open_topics(["上次没说完的排障"])
    runtime = AliyaRuntime(
        control=control,  # type: ignore[arg-type]
        llm=llm,  # type: ignore[arg-type]
        config=AgentAliyaSettings(
            memory=AgentAliyaMemorySettings(dir=str(tmp_path)),
            initiative=AgentAliyaInitiativeSettings(
                tick_seconds=0.01,
                prefer_when=["有未收尾的话题且间隔超过 6 小时"],
            ),
        ),
    )
    await runtime.start()
    try:
        for _ in range(100):
            if control.proactive:
                break
            await asyncio.sleep(0.01)
        assert control.proactive == ["在。"]
        messages = llm.messages
        assert isinstance(messages, list)
        assert any(
            "现在是" in str(message.get("content"))
            and "距上次对话" in str(message.get("content"))
            for message in messages
        )
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_ticker_skips_when_preference_not_matched(tmp_path: Path) -> None:
    control = FakeControl()
    runtime = AliyaRuntime(
        control=control,  # type: ignore[arg-type]
        llm=FakeLLM(),  # type: ignore[arg-type]
        config=AgentAliyaSettings(
            memory=AgentAliyaMemorySettings(dir=str(tmp_path)),
            initiative=AgentAliyaInitiativeSettings(
                tick_seconds=0.01,
                prefer_when=["有未收尾的话题且间隔超过 6 小时"],
            ),
        ),
    )
    await runtime.start()
    try:
        await asyncio.sleep(0.1)
        assert control.proactive == []
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_proactive_reply_is_persisted(tmp_path: Path) -> None:
    control = FakeControl()
    runtime = AliyaRuntime(
        control=control,  # type: ignore[arg-type]
        llm=FakeLLM(content="嗯。"),  # type: ignore[arg-type]
        config=make_config(tmp_path),
    )
    await runtime.start()
    try:
        turn = TurnContext(
            user_text="（现在没有人说话。）",
            session_id="s1",
            proactive=True,
            origin="proactive",
        )
        turn.sender = lambda text, turn=turn: runtime._send_for_turn(turn, text)
        runtime._queue.put_nowait(turn)
        await asyncio.wait_for(runtime.wait_idle(), timeout=2)
        assert control.proactive == ["嗯。"]
        turns = MemoryStore(tmp_path).recent_turns()
        assert any(
            item.role == "aliya"
            and item.text == "嗯。"
            and item.message_id.startswith("proactive:")
            for item in turns
        )
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_memory_compresses_when_turns_exceed_limit(tmp_path: Path) -> None:
    control = FakeControl()
    llm = FakeLLM(chat_reply="滚动摘要")
    runtime = AliyaRuntime(
        control=control,  # type: ignore[arg-type]
        llm=llm,  # type: ignore[arg-type]
        config=AgentAliyaSettings(
            memory=AgentAliyaMemorySettings(
                dir=str(tmp_path),
                max_turns=2,
                summary_chars=100,
            ),
        ),
    )
    await runtime.start()
    try:
        control.inbound_queue.put_nowait(make_envelope("m1"))
        await asyncio.wait_for(runtime.wait_idle(), timeout=2)
        control.inbound_queue.put_nowait(make_envelope("m2"))
        await asyncio.wait_for(runtime.wait_idle(), timeout=2)
        store = MemoryStore(tmp_path)
        assert store.load_summary() == "滚动摘要"
        assert len(store.recent_turns()) == 1
        assert llm.chat_calls
    finally:
        await runtime.stop()


def test_world_inference_drops_older_revision(tmp_path: Path) -> None:
    runtime = AliyaRuntime(
        control=FakeControl(),  # type: ignore[arg-type]
        llm=FakeLLM(),  # type: ignore[arg-type]
        config=make_config(tmp_path),
    )
    now = datetime.now(UTC)

    def event(revision: int, snapshot: str) -> CosmosEvent:
        view = WorldInference(
            inference_id=f"i{revision}",
            generated_at=now,
            revision=revision,
            snapshot=snapshot,
            hypotheses=[
                {"statement": "她可能失眠", "confidence": 0.8, "basis": "时间"}
            ],
        )
        return CosmosEvent(
            event_id=f"e{revision}",
            topic="world.inference",
            sequence=revision,
            at=now,
            payload={"inference": view.model_dump(mode="json")},
        )

    runtime._cache_event(event(2, "新结论"))
    runtime._cache_event(event(1, "旧结论"))
    assert runtime._world is not None
    assert runtime._world.snapshot == "新结论"


def test_user_active_cancels_pending_proactive(tmp_path: Path) -> None:
    runtime = AliyaRuntime(
        control=FakeControl(),  # type: ignore[arg-type]
        llm=FakeLLM(),  # type: ignore[arg-type]
        config=make_config(tmp_path),
    )
    turn = TurnContext(user_text="（现在没有人说话。）", proactive=True, origin="proactive")
    runtime._pending_proactive = [turn]
    runtime._cache_event(
        CosmosEvent(
            event_id="e1",
            topic="user.active",
            sequence=1,
            at=datetime.now(UTC),
            payload={},
        )
    )
    assert turn.canceled is True
    assert runtime._pending_proactive == []


@pytest.mark.asyncio
async def test_canceled_proactive_turn_is_skipped(tmp_path: Path) -> None:
    control = FakeControl()
    runtime = AliyaRuntime(
        control=control,  # type: ignore[arg-type]
        llm=FakeLLM(),  # type: ignore[arg-type]
        config=make_config(tmp_path),
    )
    await runtime.start()
    try:
        turn = TurnContext(
            user_text="（现在没有人说话。）",
            proactive=True,
            origin="proactive",
            canceled=True,
        )
        runtime._queue.put_nowait(turn)
        await asyncio.wait_for(runtime.wait_idle(), timeout=2)
        assert control.proactive == []
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_event_turn_is_dropped_when_queue_full(tmp_path: Path) -> None:
    runtime = AliyaRuntime(
        control=FakeControl(),  # type: ignore[arg-type]
        llm=FakeLLM(),  # type: ignore[arg-type]
        config=make_config(tmp_path),
    )
    for _ in range(32):
        runtime._queue.put_nowait(TurnContext(origin="event", droppable=True))
    assert await runtime._enqueue(TurnContext(origin="event", droppable=True)) is False
    assert runtime._queue.qsize() == 32


@pytest.mark.asyncio
async def test_wake_event_waits_for_queue_space(tmp_path: Path) -> None:
    runtime = AliyaRuntime(
        control=FakeControl(),  # type: ignore[arg-type]
        llm=FakeLLM(),  # type: ignore[arg-type]
        config=make_config(tmp_path),
    )
    for _ in range(32):
        runtime._queue.put_nowait(TurnContext(origin="event", droppable=True))
    pending = asyncio.create_task(runtime._enqueue(TurnContext(origin="event")))
    await asyncio.sleep(0)
    assert pending.done() is False
    runtime._queue.get_nowait()
    await asyncio.wait_for(pending, timeout=1)
    assert runtime._queue.qsize() == 32


def test_progress_forwarding_follows_config(tmp_path: Path) -> None:
    event = CosmosEvent(
        event_id="e1",
        topic="agent.progress",
        sequence=1,
        at=datetime.now(UTC),
        payload={},
    )
    never = AliyaRuntime(
        control=FakeControl(),  # type: ignore[arg-type]
        llm=FakeLLM(),  # type: ignore[arg-type]
        config=AgentAliyaSettings(
            memory=AgentAliyaMemorySettings(dir=str(tmp_path)),
            reply=AgentAliyaReplySettings(progress_forwarding="never"),
        ),
    )
    always = AliyaRuntime(
        control=FakeControl(),  # type: ignore[arg-type]
        llm=FakeLLM(),  # type: ignore[arg-type]
        config=AgentAliyaSettings(
            memory=AgentAliyaMemorySettings(dir=str(tmp_path)),
            reply=AgentAliyaReplySettings(progress_forwarding="always"),
        ),
    )
    assert never._should_report(event) is False
    assert always._should_report(event) is True

    on_wait = AliyaRuntime(
        control=FakeControl(),  # type: ignore[arg-type]
        llm=FakeLLM(),  # type: ignore[arg-type]
        config=AgentAliyaSettings(
            memory=AgentAliyaMemorySettings(dir=str(tmp_path)),
            reply=AgentAliyaReplySettings(progress_forwarding="on_wait"),
        ),
    )
    assert on_wait._should_report(event) is False
    on_wait._current_turn = TurnContext(user_text="在吗")
    assert on_wait._should_report(event) is True


@pytest.mark.asyncio
async def test_persona_includes_are_loaded(tmp_path: Path) -> None:
    base = tmp_path / "persona.md"
    base.write_text("基础人格", encoding="utf-8")
    extra = tmp_path / "extra.md"
    extra.write_text("附加人格", encoding="utf-8")
    control = FakeControl()
    llm = FakeLLM()
    runtime = AliyaRuntime(
        control=control,  # type: ignore[arg-type]
        llm=llm,  # type: ignore[arg-type]
        config=AgentAliyaSettings(
            persona=AgentAliyaPersonaSettings(
                source=str(base),
                includes=[str(extra)],
            ),
            memory=AgentAliyaMemorySettings(dir=str(tmp_path)),
        ),
    )
    await runtime.start()
    try:
        control.inbound_queue.put_nowait(make_envelope("m1"))
        await asyncio.wait_for(runtime.wait_idle(), timeout=2)
        system = llm.messages[0]["content"]  # type: ignore[index]
        assert "基础人格" in system
        assert "附加人格" in system
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_memory_disabled_skips_long_term_storage(tmp_path: Path) -> None:
    control = FakeControl()
    runtime = AliyaRuntime(
        control=control,  # type: ignore[arg-type]
        llm=FakeLLM(),  # type: ignore[arg-type]
        config=AgentAliyaSettings(
            memory=AgentAliyaMemorySettings(dir=str(tmp_path), enabled=False),
        ),
    )
    await runtime.start()
    try:
        assert await runtime.remember("她喜欢风信子", "anchor") == "这件事先不记"
        control.inbound_queue.put_nowait(make_envelope("m1"))
        await asyncio.wait_for(runtime.wait_idle(), timeout=2)
        assert not (tmp_path / "turns.jsonl").exists()
        assert runtime._compose_memory("摘要") == "摘要"
    finally:
        await runtime.stop()
