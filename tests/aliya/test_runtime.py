"""Aliya 运行时测试。

测试桩刻意只实现被测路径需要的控制面能力：签名与协议一致，未被调用的方法
保持 NotImplementedError 或空返回。私有成员访问与忽略返回值是测试的既定用法。
"""

# pyright: reportPrivateUsage=false, reportUnusedParameter=false, reportUnusedCallResult=false

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast, override

import pytest
from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolParam

from agent.aliya.memory.store import MemoryStore
from agent.aliya.runtime import (
    AliyaRuntime,
    batch_needs_report,
    event_brief,
    remember_result,
)
from agent.aliya.turn.context import TurnContext
from agent.cosmos.contracts import (
    AgentCard,
    AgentDetail,
    AgentProgress,
    AgentRuntimeState,
    AgentStatus,
    AliyaContext,
    ConversationContext,
    CosmosCapabilities,
    CosmosEvent,
    DesktopEntry,
    DesktopLayoutPatch,
    DesktopSnapshot,
    DispatchHandle,
    DispatchIntent,
    DispatchPlan,
    Hypothesis,
    InboundEnvelope,
    LinkState,
    PresenceField,
    PresenceSnapshot,
    ProactiveReceipt,
    ReadablePresence,
    ReadableState,
    TaskBrief,
    TaskHandle,
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
    LLMSettings,
)
from core.llm import AssistantReply, LLMService


class FakeControl:
    """ControlPort 的测试桩：只实现被测路径，其余方法保持未实现。"""

    def __init__(self) -> None:
        self.inbound_queue: asyncio.Queue[InboundEnvelope] = asyncio.Queue()
        self.event_queue: asyncio.Queue[CosmosEvent] = asyncio.Queue()
        self.delivered: list[str] = []
        self.delivered_kinds: list[str] = []
        self.notified: list[str] = []
        self.proactive: list[str] = []
        self.acked: list[str] = []
        self.contexts: list[AliyaContext] = []
        self.since: int | None = None
        self.cursors: Mapping[str, int] | None = None
        self.intents: list[DispatchIntent] = []

    def now(self) -> datetime:
        return datetime.now(UTC)

    async def handshake(self) -> CosmosCapabilities:
        return CosmosCapabilities(api_version="1.0", contract_version="1.0.0")

    def inbound(self) -> AsyncIterator[InboundEnvelope]:
        return self._inbound()

    async def _inbound(self) -> AsyncIterator[InboundEnvelope]:
        while True:
            yield await self.inbound_queue.get()

    async def ack_inbound(self, message_id: str) -> None:
        self.acked.append(message_id)

    def subscribe(
        self,
        topics: Sequence[str],
        *,
        since_sequence: int | None = None,
        cursors: Mapping[str, int] | None = None,
    ) -> AsyncIterator[CosmosEvent]:
        return self._subscribe(since_sequence=since_sequence, cursors=cursors)

    async def _subscribe(
        self,
        *,
        since_sequence: int | None = None,
        cursors: Mapping[str, int] | None = None,
    ) -> AsyncIterator[CosmosEvent]:
        self.since = since_sequence
        self.cursors = cursors
        while True:
            yield await self.event_queue.get()

    async def deliver(self, text: str, *, session_id: str, kind: str = "chat") -> None:
        self.delivered.append(text)
        self.delivered_kinds.append(kind)

    async def notify(self, text: str, *, dispatch_id: str | None = None) -> ProactiveReceipt:
        self.notified.append(text)
        return ProactiveReceipt(accepted=True, reason="ok")

    async def send_proactive(self, text: str, *, urgency: float = 0.5) -> ProactiveReceipt:
        self.proactive.append(text)
        return ProactiveReceipt(accepted=True, reason="ok")

    async def history(self, session_id: str, limit: int = 20) -> list[TerminalMessage]:
        return []

    async def set_aliya_context(self, ctx: AliyaContext) -> None:
        self.contexts.append(ctx)

    async def describe_desktop(self) -> DesktopSnapshot:
        return DesktopSnapshot(generated_at=datetime.now(UTC), summary="桌面")

    async def get_agent(self, agent_id: str) -> AgentDetail:
        raise NotImplementedError

    async def open_agent(self, agent_id: str) -> AgentDetail:
        raise NotImplementedError

    async def patch_layout(self, patch: DesktopLayoutPatch) -> DesktopSnapshot:
        raise NotImplementedError

    async def plan(self, intent: DispatchIntent) -> DispatchPlan:
        self.intents.append(intent)
        return DispatchPlan(decision="reply_only", rationale="ok")

    async def invoke_agent(
        self, agent_id: str, params: dict[str, object], *, session_id: str
    ) -> TaskHandle:
        raise NotImplementedError

    async def execute_plan(self, plan: DispatchPlan) -> DispatchHandle:
        raise NotImplementedError

    async def cancel_task(self, task_id: str) -> None:
        return None

    async def cancel_dispatch(self, dispatch_id: str) -> None:
        return None

    async def retry_task(self, task_id: str) -> TaskHandle:
        raise NotImplementedError

    async def list_tasks(self, agent_id: str | None = None, limit: int = 20) -> list[TaskBrief]:
        return []

    def get_presence(self) -> PresenceSnapshot:
        return PresenceSnapshot(
            captured_at=datetime.now(UTC),
            readable=ReadablePresence(summary="在电脑前"),
        )

    async def fetch_presence(self) -> PresenceSnapshot:
        return self.get_presence()

    async def set_presence_visibility(self, fields: Sequence[PresenceField]) -> None:
        return None

    async def get_link_state(self) -> LinkState:
        return "up"

    async def get_world_view(self, force: bool = False) -> WorldInference | None:
        return None

    async def refresh_world(self) -> WorldInference | None:
        return None


class StatefulControl(FakeControl):
    """启动时能返回桌面、世界与状态的控制面桩。"""

    def __init__(self) -> None:
        super().__init__()
        self.desktop: DesktopSnapshot = DesktopSnapshot(
            generated_at=datetime.now(UTC),
            summary="桌面上有两张卡",
        )
        self.world: WorldInference = WorldInference(
            inference_id="i1",
            generated_at=datetime.now(UTC),
            snapshot="夜里很安静",
            hypotheses=[Hypothesis(statement="她可能失眠", confidence=0.8, basis="时间")],
        )

    @override
    async def describe_desktop(self) -> DesktopSnapshot:
        return self.desktop

    @override
    async def get_world_view(self, force: bool = False) -> WorldInference | None:
        return self.world

    @override
    async def get_link_state(self) -> LinkState:
        return "up"


class FakeLLM(LLMService):
    """LLMService 的测试桩：只覆写回合用到的两个入口，不建客户端。"""

    def __init__(
        self,
        content: str = "在。",
        delay: float = 0.0,
        chat_reply: str = "滚动摘要",
    ) -> None:
        super().__init__(LLMSettings())
        self.content: str = content
        self.delay: float = delay
        self.chat_reply: str = chat_reply
        self.messages: list[ChatCompletionMessageParam] = []
        self.chat_calls: list[Sequence[ChatCompletionMessageParam]] = []

    @property
    def system_text(self) -> str:
        """首条消息的正文，供断言系统提示用。"""
        if not self.messages:
            return ""
        content = cast("dict[str, object]", cast("object", self.messages[0])).get("content")
        return content if isinstance(content, str) else ""

    @property
    def prompt_text(self) -> str:
        """全部消息正文：上下文与工具索引走消息，不再进系统提示词。"""
        parts: list[str] = []
        for message in self.messages:
            content = cast("dict[str, object]", cast("object", message)).get("content")
            if isinstance(content, str):
                parts.append(content)
        return "\n".join(parts)

    def text_of(self, role: str) -> list[str]:
        """按角色取出消息正文，供断言上下文注入用。"""
        texts: list[str] = []
        for message in self.messages:
            data = cast("dict[str, object]", cast("object", message))
            content = data.get("content")
            if data.get("role") == role and isinstance(content, str):
                texts.append(content)
        return texts

    @override
    async def chat_tools(
        self,
        messages: Sequence[ChatCompletionMessageParam],
        tools: Sequence[ChatCompletionToolParam],
        **_kwargs: object,
    ) -> AssistantReply:
        self.messages = list(messages)
        if self.delay:
            await asyncio.sleep(self.delay)
        return AssistantReply(content=self.content, tool_calls=())

    @override
    async def chat(
        self,
        messages: Sequence[ChatCompletionMessageParam],
        **_kwargs: object,
    ) -> str:
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


def test_remember_result_counts_per_dispatch() -> None:
    counts: dict[str, int] = {}
    remember_result({"dispatch_id": "d1"}, counts)
    remember_result({"dispatch_id": "d1"}, counts)
    remember_result({"dispatch_id": None}, counts)
    assert counts == {"d1": 2}


def test_batch_skipped_when_all_results_reported() -> None:
    outcome: dict[str, object] = {"status": "ok", "results": [{}, {}]}
    assert batch_needs_report(outcome, 2) is False
    assert batch_needs_report(outcome, 1) is True


def test_batch_with_partial_status_still_reports() -> None:
    outcome: dict[str, object] = {"status": "partial", "results": [{}]}
    assert batch_needs_report(outcome, 1) is True


@pytest.mark.asyncio
async def test_inbound_turn_is_serial(tmp_path: Path) -> None:
    control = FakeControl()
    llm = FakeLLM()
    runtime = AliyaRuntime(
        control=control,
        llm=llm,
        config=make_config(tmp_path),
    )
    await runtime.start()
    try:
        control.inbound_queue.put_nowait(make_envelope("m1"))
        await asyncio.wait_for(runtime.wait_idle(), timeout=2)
        assert control.delivered == ["在。"]
        assert control.acked == ["m1"]
        prompt = llm.prompt_text
        assert "send_message" in prompt
        assert "dispatch_task" in prompt
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
        control=control,
        llm=FakeLLM(delay=1.0),
        config=make_config(tmp_path, timeout=0.05),
    )
    await runtime.start()
    try:
        control.inbound_queue.put_nowait(make_envelope("m1"))
        await asyncio.wait_for(runtime.wait_idle(), timeout=3)
        assert control.delivered == [AgentAliyaSettings().reply.fallback_text]
        # 失败提示以 system 身份发出，不是 Aliya 说的话。
        assert control.delivered_kinds == ["system"]
        assert [turn.role for turn in MemoryStore(tmp_path).recent_turns()] == ["user"]
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_event_subscription_resumes_from_min_cursor(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    store.save_cursor(3, topic="agent.result")
    store.save_cursor(1, topic="presence.changed")

    control = FakeControl()
    runtime = AliyaRuntime(
        control=control,
        llm=FakeLLM(),
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
        control=control,
        llm=llm,
        config=make_config(tmp_path),
    )
    await runtime.start()
    try:
        control.inbound_queue.put_nowait(make_envelope("m1"))
        await asyncio.wait_for(runtime.wait_idle(), timeout=2)
        prompt = llm.prompt_text
        assert "她最近在整理旧照片" in prompt
        assert "风信子" in prompt
        assert "海边的约定" in prompt
        assert "只对你说的话" in prompt
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_startup_recovers_unanswered_turn(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    store.record_inbound("m0", "在吗", session_id="s1")

    control = FakeControl()
    runtime = AliyaRuntime(
        control=control,
        llm=FakeLLM(),
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
        control=control,
        llm=FakeLLM(),
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

    with_content = CosmosEvent(
        event_id="e1b",
        topic="agent.result",
        sequence=2,
        at=datetime.now(UTC),
        payload={
            "result": {
                "status": "ok",
                "summary": "搜到结果",
                "content": "今天有两条消息。\n参考：https://example.com",
            }
        },
    )
    brief = event_brief(with_content)
    assert "内容：" in brief
    assert "今天有两条消息" in brief

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
        control=control,
        llm=llm,
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
        prompt = llm.prompt_text
        assert "夜里很安静" in prompt
        assert "她可能失眠" in prompt
        assert "轻声问候" in prompt
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_event_report_uses_notify_with_real_payload(tmp_path: Path) -> None:
    control = FakeControl()
    llm = FakeLLM()
    runtime = AliyaRuntime(
        control=control,
        llm=llm,
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
        user_messages = llm.text_of("user")
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
        control=control,
        llm=llm,
        config=make_config(tmp_path),
    )
    await runtime.start()
    try:
        control.inbound_queue.put_nowait(make_envelope("m1"))
        await asyncio.wait_for(runtime.wait_idle(), timeout=2)
        prompt = llm.prompt_text
        assert "平板：桌面上有两张卡" in prompt
        assert "在电脑前" in prompt
        assert "夜里很安静" in prompt
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_describe_desktop_exposes_agent_id(tmp_path: Path) -> None:
    """dispatch_task 的 agent_id 只能从 see_desktop 获得，输出必须带 id。"""
    control = StatefulControl()
    control.desktop = DesktopSnapshot(
        generated_at=datetime.now(UTC),
        summary="桌上有一张卡",
        entries=[
            DesktopEntry(
                card=AgentCard(
                    id="echo",
                    name="回声",
                    summary="复述内容",
                    description="把给定内容复述一遍",
                    capabilities=["repeat"],
                ),
                status="idle",
                readable=ReadableState(summary="闲着"),
            )
        ],
    )
    runtime = AliyaRuntime(
        control=control,
        llm=FakeLLM(),
        config=make_config(tmp_path),
    )
    text = await runtime.describe_desktop()
    assert "echo｜回声" in text
    assert "能做：repeat" in text


def test_event_caches_feed_task_and_desktop_context(tmp_path: Path) -> None:
    runtime = AliyaRuntime(
        control=FakeControl(),
        llm=FakeLLM(),
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


def _desktop_entry(status: AgentStatus) -> DesktopEntry:
    return DesktopEntry(
        card=AgentCard(
            id="echo",
            name="回声",
            summary="复述内容",
            description="把给定内容复述一遍",
            capabilities=["repeat"],
        ),
        status=status,
        readable=ReadableState(summary="闲着" if status == "idle" else "在忙"),
    )


def test_全部空闲的平板不进上下文(tmp_path: Path) -> None:
    """工具全闲着时不该每回合注入「5 个空闲」，否则模型会反复播报。"""
    runtime = AliyaRuntime(
        control=FakeControl(),
        llm=FakeLLM(),
        config=make_config(tmp_path),
    )
    runtime._desktop = DesktopSnapshot(
        generated_at=datetime.now(UTC),
        summary="5 个工具，5 个空闲",
        entries=[_desktop_entry("idle") for _ in range(5)],
    )
    assert runtime._desktop_text() is None


def test_有任务在跑时平板仍进上下文(tmp_path: Path) -> None:
    runtime = AliyaRuntime(
        control=FakeControl(),
        llm=FakeLLM(),
        config=make_config(tmp_path),
    )
    runtime._desktop = DesktopSnapshot(
        generated_at=datetime.now(UTC),
        summary="2 个工具，1 个空闲，1 个正在忙",
        entries=[_desktop_entry("idle"), _desktop_entry("running")],
    )
    assert runtime._desktop_text() == "平板：2 个工具，1 个空闲，1 个正在忙"


def test_长期记忆里的事实会进上下文(tmp_path: Path) -> None:
    """remember(kind=fact) 写进去的事实必须能被后续回合读到，否则「记住」是空话。"""
    store = MemoryStore(str(tmp_path))
    assert store.remember("我的中文名字是梦", "fact") is True
    runtime = AliyaRuntime(
        control=FakeControl(),
        llm=FakeLLM(),
        config=make_config(tmp_path),
    )
    memory = runtime._compose_memory(None)
    assert memory is not None
    assert "记得：我的中文名字是梦" in memory


@pytest.mark.asyncio
async def test_dispatch_hint_includes_tool_preferences(tmp_path: Path) -> None:
    control = FakeControl()
    runtime = AliyaRuntime(
        control=control,
        llm=FakeLLM(),
        config=AgentAliyaSettings(
            memory=AgentAliyaMemorySettings(dir=str(tmp_path)),
            tools=AgentAliyaToolsSettings(prefer_agents=["echo"], max_parallel_dispatch=1),
        ),
    )
    await runtime.dispatch("看看登录")
    intent = control.intents[0]
    assert intent.hint == "优先使用：echo；最多并行 1 个"


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
        control=FakeControl(),
        llm=llm,
        config=config,
    )
    prepared = await runtime._prepare_outbound("（笑）123456")
    assert len(prepared) == 5
    assert prepared.endswith("…")
    assert llm.chat_calls


@pytest.mark.asyncio
async def test_zero_limit_keeps_message_intact(tmp_path: Path) -> None:
    config = AgentAliyaSettings(
        memory=AgentAliyaMemorySettings(dir=str(tmp_path)),
        reply=AgentAliyaReplySettings(max_chars_per_message=0, post_checks=[]),
    )
    runtime = AliyaRuntime(
        control=FakeControl(),
        llm=FakeLLM(),
        config=config,
    )
    text = "很长的回复" * 1_000
    assert await runtime._prepare_outbound(text) == text


@pytest.mark.asyncio
async def test_outbound_check_rewrites_before_sending(tmp_path: Path) -> None:
    llm = FakeLLM(chat_reply="好的")
    runtime = AliyaRuntime(
        control=FakeControl(),
        llm=llm,
        config=make_config(tmp_path),
    )
    prepared = await runtime._prepare_outbound("（笑）好啊")
    assert prepared == "好的"


@pytest.mark.asyncio
async def test_crisis_phase_is_reported(tmp_path: Path) -> None:
    control = FakeControl()
    runtime = AliyaRuntime(
        control=control,
        llm=FakeLLM(),
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
        control=FakeControl(),
        llm=FakeLLM(),
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
        control=control,
        llm=llm,
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
        control=control,
        llm=FakeLLM(),
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
        control=control,
        llm=FakeLLM(content="嗯。"),
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
        control=control,
        llm=llm,
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
        control=FakeControl(),
        llm=FakeLLM(),
        config=make_config(tmp_path),
    )
    now = datetime.now(UTC)

    def event(revision: int, snapshot: str) -> CosmosEvent:
        view = WorldInference(
            inference_id=f"i{revision}",
            generated_at=now,
            revision=revision,
            snapshot=snapshot,
            hypotheses=[Hypothesis(statement="她可能失眠", confidence=0.8, basis="时间")],
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
        control=FakeControl(),
        llm=FakeLLM(),
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
        control=control,
        llm=FakeLLM(),
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
        control=FakeControl(),
        llm=FakeLLM(),
        config=make_config(tmp_path),
    )
    for _ in range(32):
        runtime._queue.put_nowait(TurnContext(origin="event", droppable=True))
    assert await runtime._enqueue(TurnContext(origin="event", droppable=True)) is False
    assert runtime._queue.qsize() == 32


@pytest.mark.asyncio
async def test_wake_event_waits_for_queue_space(tmp_path: Path) -> None:
    runtime = AliyaRuntime(
        control=FakeControl(),
        llm=FakeLLM(),
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
        control=FakeControl(),
        llm=FakeLLM(),
        config=AgentAliyaSettings(
            memory=AgentAliyaMemorySettings(dir=str(tmp_path)),
            reply=AgentAliyaReplySettings(progress_forwarding="never"),
        ),
    )
    always = AliyaRuntime(
        control=FakeControl(),
        llm=FakeLLM(),
        config=AgentAliyaSettings(
            memory=AgentAliyaMemorySettings(dir=str(tmp_path)),
            reply=AgentAliyaReplySettings(progress_forwarding="always"),
        ),
    )
    assert never._should_report(event) is False
    assert always._should_report(event) is True

    on_wait = AliyaRuntime(
        control=FakeControl(),
        llm=FakeLLM(),
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
        control=control,
        llm=llm,
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
        system = llm.system_text
        assert "基础人格" in system
        assert "附加人格" in system
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_due_intent_wakes_proactive_turn(tmp_path: Path) -> None:
    """到期的跟进意图要绕过常规时间门控，主动排一个回合。"""
    store = MemoryStore(tmp_path)
    assert store.add_intent("问她面试怎么样", due_at=datetime.now(UTC) - timedelta(minutes=1))
    runtime = AliyaRuntime(
        control=FakeControl(),
        llm=FakeLLM(),
        config=make_config(tmp_path),
    )
    await runtime._tick_once()
    assert len(runtime._pending_proactive) == 1
    turn = runtime._pending_proactive[0]
    assert "问她面试怎么样" in turn.user_text
    # 触发时不删除，等回合正常结束后再结算；否则取消或失败会丢掉跟进。
    assert [item.text for item in store.load_intents()] == ["问她面试怎么样"]
    runtime._settle_intent(turn)
    assert store.load_intents() == []


@pytest.mark.asyncio
async def test_canceled_intent_turn_is_rescheduled(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    assert store.add_intent("问她面试", due_at=datetime.now(UTC) - timedelta(minutes=1))
    runtime = AliyaRuntime(
        control=FakeControl(),
        llm=FakeLLM(),
        config=make_config(tmp_path),
    )
    await runtime._tick_once()
    turn = runtime._pending_proactive[0]
    runtime._cancel_pending_proactive()
    assert turn.canceled is True
    runtime._retry_intent(turn)
    intents = store.load_intents()
    assert [item.text for item in intents] == ["问她面试"]
    assert intents[0].due_at > datetime.now(UTC)


@pytest.mark.asyncio
async def test_cancel_followup_removes_intent(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    assert store.add_intent("问她面试", due_at=datetime.now(UTC) + timedelta(hours=1))
    runtime = AliyaRuntime(
        control=FakeControl(),
        llm=FakeLLM(),
        config=make_config(tmp_path),
    )
    assert await runtime.cancel_followup("面试") == "已经取消了"
    assert store.load_intents() == []


@pytest.mark.asyncio
async def test_canceled_intent_does_not_revive_after_explicit_cancel(tmp_path: Path) -> None:
    """用户已取消的跟进，不能因为回合被取消又被重排回来。"""
    store = MemoryStore(tmp_path)
    assert store.add_intent("问她面试", due_at=datetime.now(UTC) - timedelta(minutes=1))
    runtime = AliyaRuntime(
        control=FakeControl(),
        llm=FakeLLM(),
        config=make_config(tmp_path),
    )
    await runtime._tick_once()
    turn = runtime._pending_proactive[0]
    assert store.cancel_intent("面试") is True
    runtime._retry_intent(turn)
    assert store.load_intents() == []


@pytest.mark.asyncio
async def test_defer_silences_current_proactive_turn(tmp_path: Path) -> None:
    runtime = AliyaRuntime(
        control=FakeControl(),
        llm=FakeLLM(),
        config=make_config(tmp_path),
    )
    turn = TurnContext(user_text="", proactive=True, origin="proactive")
    runtime._current_turn = turn
    result = await runtime.defer(30, "她在忙")
    assert turn.silent is True
    assert "30 分钟" in result
    assert runtime._defer_until is not None


@pytest.mark.asyncio
async def test_check_memory_lists_sections(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    store.save_anchors(["风信子"])
    store.save_open_topics(["海边的约定"])
    assert store.add_intent("问她面试", due_at=datetime.now(UTC) + timedelta(hours=1))
    runtime = AliyaRuntime(
        control=FakeControl(),
        llm=FakeLLM(),
        config=make_config(tmp_path),
    )
    text = await runtime.check_memory()
    assert "锚点：风信子" in text
    assert "未收尾：海边的约定" in text
    assert "待跟进：" in text


@pytest.mark.asyncio
async def test_plan_followup_persists_intent(tmp_path: Path) -> None:
    runtime = AliyaRuntime(
        control=FakeControl(),
        llm=FakeLLM(),
        config=make_config(tmp_path),
    )
    result = await runtime.plan_followup("问她面试", 30)
    assert "记下了" in result
    intents = MemoryStore(tmp_path).load_intents()
    assert [item.text for item in intents] == ["问她面试"]


@pytest.mark.asyncio
async def test_resolve_topic_removes_open_topic(tmp_path: Path) -> None:
    MemoryStore(tmp_path).save_open_topics(["海边的约定"])
    runtime = AliyaRuntime(
        control=FakeControl(),
        llm=FakeLLM(),
        config=make_config(tmp_path),
    )
    assert await runtime.resolve_topic("海边") == "已经收尾了"
    assert MemoryStore(tmp_path).load_open_topics() == []


@pytest.mark.asyncio
async def test_memory_disabled_skips_long_term_storage(tmp_path: Path) -> None:
    control = FakeControl()
    runtime = AliyaRuntime(
        control=control,
        llm=FakeLLM(),
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
