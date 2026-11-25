"""Aliya 运行时：串行回合、事件缓存与自主 tick。"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Literal

from agent.aliya.initiative import Initiative
from agent.aliya.memory.store import MemoryStore
from agent.aliya.ports import ControlPort
from agent.aliya.prompt.builder import PromptBuilder
from agent.aliya.prompt.loader import load_charter, load_persona
from agent.aliya.turn.context import TurnContext
from agent.aliya.turn.postcheck import check
from agent.aliya.turn.runner import TurnRunner
from agent.aliya.turn.tools import build_tools
from agent.cosmos.contracts import (
    AliyaContext,
    CosmosEvent,
    DispatchIntent,
    ReadablePresence,
)
from core.config.settings import AgentAliyaSettings
from core.llm import LLMService

_LOG = logging.getLogger(__name__)

type Phase = Literal["daily", "maintenance", "crisis", "rest"]

_REPORT_TOPICS = {"agent.result", "dispatch.finished", "agent.abnormal"}
_EVENT_TOPICS = [
    "agent.progress",
    "agent.result",
    "agent.abnormal",
    "dispatch.finished",
    "agent.state.changed",
    "desktop.layout.changed",
    "presence.changed",
    "world.inference",
    "user.active",
    "link.state.changed",
]


def phase_for(*, now: datetime, tasks: int, crisis: bool) -> Phase:
    """回合结束后回传的处境：有任务优先，其次危机，再按时段。"""
    if tasks > 0:
        return "maintenance"
    if crisis:
        return "crisis"
    if now.hour >= 23 or now.hour < 5:
        return "rest"
    return "daily"


class AliyaRuntime:
    def __init__(
        self,
        *,
        control: ControlPort,
        llm: LLMService,
        config: AgentAliyaSettings,
        memory: MemoryStore | None = None,
    ) -> None:
        self._control = control
        self._llm = llm
        self._config = config
        self._memory = memory or MemoryStore(config.memory.dir)
        self._queue: asyncio.Queue[TurnContext] = asyncio.Queue(maxsize=32)
        self._worker: asyncio.Task[None] | None = None
        self._ticker: asyncio.Task[None] | None = None
        self._consumers: list[asyncio.Task[None]] = []
        self._seen: set[str] = set()
        self._current_turn: TurnContext | None = None
        self._presence: ReadablePresence | None = None
        self._link = "up"
        self._last_user_at: datetime | None = None
        self._last_session_id = ""
        self._task_count = 0
        self._initiative = Initiative(config.initiative)
        self._runner: TurnRunner | None = None
        self._proactive_runner: TurnRunner | None = None
        self._started = False
        self._pending_inbound = 0
        self._closed = asyncio.Event()

    async def start(self) -> None:
        if self._started:
            return
        await self._control.handshake()
        builder = PromptBuilder(
            persona=load_persona(self._config.persona.source),
            charter=load_charter(self._config.persona.charter),
            tools=[],
        )
        self._runner = TurnRunner(
            llm=self._llm,
            tools=build_tools(self),
            builder=builder,
            config=self._config,
        )
        self._proactive_runner = TurnRunner(
            llm=self._llm,
            tools=build_tools(self, proactive=True),
            builder=builder,
            config=self._config,
        )
        self._worker = asyncio.create_task(self._run_worker())
        self._ticker = asyncio.create_task(self._run_ticker())
        await self._recover_unanswered()
        self._consumers = [
            asyncio.create_task(self._consume_inbound()),
            asyncio.create_task(self._consume_events()),
        ]
        self._started = True

    async def _recover_unanswered(self) -> None:
        """启动时补跑已落盘但未回复的回合；幂等键为 message_id。"""
        for stored in self._memory.unanswered_turns():
            if not stored.message_id or stored.message_id in self._seen:
                continue
            turn = TurnContext(
                user_text=stored.text,
                session_id=stored.session_id,
                message_id=stored.message_id,
            )
            turn.sender = lambda text, turn=turn: self._send_for_turn(turn, text)
            self._seen.add(stored.message_id)
            await self._queue.put(turn)

    async def stop(self) -> None:
        tasks = [self._worker, self._ticker, *self._consumers]
        for task in tasks:
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *(task for task in tasks if task is not None),
            return_exceptions=True,
        )
        self._worker = None
        self._ticker = None
        self._consumers = []
        self._started = False
        self._closed.set()

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def wait_idle(self) -> None:
        """等待当前入站与队列中的回合处理完。"""
        await asyncio.sleep(0.02)
        while (
            self._pending_inbound > 0 or not self._queue.empty() or self._current_turn is not None
        ):
            await asyncio.sleep(0.01)
        await self._queue.join()

    # ---------- 工具面调用的运行时能力 ----------

    async def send(self, text: str) -> None:
        turn = self._current_turn
        session_id = turn.session_id if turn is not None else self._last_session_id
        await self._deliver(text, session_id)
        if turn is not None:
            if turn.proactive and not turn.sent:
                self._initiative.record_proactive(datetime.now(UTC))
            turn.sent = True
            turn.replies.append(text)

    async def describe_desktop(self) -> str:
        snapshot = await self._control.describe_desktop()
        return snapshot.summary

    async def dispatch(
        self,
        task_text: str,
        *,
        agent_id: str | None = None,
        params: dict[str, object] | None = None,
        hint: str | None = None,
    ) -> str:
        plan = await self._control.plan(
            DispatchIntent(
                source="natural_language",
                session_id=self._last_session_id,
                user_text=task_text,
                agent_id=agent_id,
                params=params,
                hint=hint,
            )
        )
        if plan.decision == "clarify":
            missing = "、".join(plan.missing) if plan.missing else "参数"
            return f"还需要：{missing}"
        if plan.decision in {"rejected", "reply_only"} or not plan.tasks:
            return plan.rationale
        handle = await self._control.execute_plan(plan)
        if not handle.task_ids:
            return "没派成"
        return f"已经派人去做了：{plan.tasks[0].label}"

    async def task_status(self, task_id: str | None = None) -> str:
        tasks = await self._control.list_tasks(None, 20)
        if task_id is not None:
            tasks = [task for task in tasks if task.task_id == task_id]
        if not tasks:
            return "现在没有在做的任务"
        lines = []
        for task in tasks[:5]:
            progress = f"（{int(task.progress * 100)}%）" if task.progress is not None else ""
            lines.append(f"{task.label}：{task.status}{progress}")
        return "\n".join(lines)

    async def cancel(self, task_id: str) -> str:
        await self._control.cancel_task(task_id)
        return "已取消"

    async def retry(self, task_id: str) -> str:
        await self._control.retry_task(task_id)
        return "已重试"

    async def refresh_world(self) -> str:
        view = await self._control.refresh_world()
        return view.snapshot if view is not None else "现在看不出什么"

    async def remember(self, note: str, kind: str) -> str:
        return "记住了" if self._memory.remember(note, kind) else "这件事先不记"

    # ---------- 消费循环 ----------

    async def _consume_inbound(self) -> None:
        async for envelope in self._control.inbound():
            message_id = envelope.message.message_id
            if message_id in self._seen:
                # 已落盘但上次确认失败：重放时补确认，不重复入队。
                await self._ack(message_id)
                continue
            at = envelope.message.at.isoformat() if envelope.message.at else ""
            try:
                self._memory.record_inbound(
                    message_id,
                    envelope.message.text,
                    session_id=envelope.message.session_id,
                    at=at,
                )
            except OSError:
                _LOG.exception("入站消息落盘失败，暂不回执")
                continue
            self._seen.add(message_id)
            await self._ack(message_id)
            self._last_session_id = envelope.message.session_id
            self._last_user_at = envelope.message.at or datetime.now(UTC)
            self._initiative.record_user_reply()
            self._task_count = len(envelope.tasks)
            self._presence = envelope.presence
            turn = TurnContext.from_envelope(envelope)
            turn.sender = lambda text, turn=turn: self._send_for_turn(turn, text)
            self._pending_inbound += 1
            try:
                await self._queue.put(turn)
            finally:
                self._pending_inbound -= 1

    async def _consume_events(self) -> None:
        cursors = self._memory.load_cursors()
        since = min(cursors.values()) if cursors else None
        async for event in self._control.subscribe(
            _EVENT_TOPICS, since_sequence=since, cursors=cursors
        ):
            self._cache_event(event)
            if event.topic in _REPORT_TOPICS:
                turn = TurnContext(
                    user_text=str(event.payload.get("summary", "")) or "（收到一条进度）",
                    session_id=self._last_session_id,
                )
                turn.sender = lambda text, turn=turn: self._send_for_turn(turn, text)
                await self._queue.put(turn)
            try:
                self._memory.save_cursor(event.sequence, topic=event.topic)
            except OSError:
                _LOG.warning("事件游标落盘失败", exc_info=True)

    def _cache_event(self, event: CosmosEvent) -> None:
        if event.topic == "presence.changed":
            try:
                self._presence = ReadablePresence.model_validate(event.payload["readable"])
            except (KeyError, ValueError):
                return
        elif event.topic == "link.state.changed":
            state = event.payload.get("state")
            if state in {"up", "unstable", "down"}:
                self._link = state

    async def _run_worker(self) -> None:
        while True:
            turn = await self._queue.get()
            self._current_turn = turn
            turn.memory = self._compose_memory(turn.memory)
            try:
                runner = self._proactive_runner if turn.proactive else self._runner
                assert runner is not None
                await runner.run(turn=turn)
            except TimeoutError:
                await self._fallback(turn)
            except Exception:
                _LOG.exception("回合执行失败")
                await self._fallback(turn)
            finally:
                if turn.message_id:
                    for reply in turn.replies:
                        self._memory.append_turn(f"{turn.message_id}:reply", "aliya", reply)
                self._current_turn = None
                self._queue.task_done()
                await self._publish_context()

    async def _fallback(self, turn: TurnContext) -> None:
        if turn.sent:
            return
        try:
            await turn.send(self._config.reply.fallback_text)
        except Exception:
            _LOG.exception("发送兜底回复失败")

    async def _run_ticker(self) -> None:
        while True:
            await asyncio.sleep(self._config.initiative.tick_seconds)
            if not self._config.initiative.enabled:
                continue
            decision = self._initiative.should_speak(
                now=datetime.now(UTC),
                presence=self._presence,
                link=self._link,  # type: ignore[arg-type]
                last_user_at=self._last_user_at,
            )
            if not decision.should_speak:
                continue
            turn = TurnContext(
                user_text="（现在没有人说话。）",
                session_id=self._last_session_id,
                proactive=True,
            )
            turn.sender = lambda text, turn=turn: self._send_for_turn(turn, text)
            await self._queue.put(turn)

    def _compose_memory(self, base: str | None) -> str | None:
        """cosmos 摘要 + 长期记忆；锚点与私密线不参与摘要。"""
        parts: list[str] = []
        if base and base.strip():
            parts.append(base.strip())
        summary = self._memory.load_summary().strip()
        if summary:
            parts.append("长期摘要：" + summary[: self._config.memory.summary_chars])
        anchors = self._memory.load_anchors()
        if anchors:
            parts.append("锚点：" + "、".join(anchors))
        topics = self._memory.load_open_topics()[: self._config.memory.open_topics_limit]
        if topics:
            parts.append("未收尾：" + "、".join(topics))
        if self._config.memory.private_threads:
            unspoken = self._memory.load_unspoken()[:3]
            if unspoken:
                parts.append("只对你说过：" + "；".join(unspoken))
        return "\n".join(parts) or None

    async def _ack(self, message_id: str) -> None:
        try:
            await self._control.ack_inbound(message_id)
        except Exception:
            _LOG.warning("入站确认失败，等待重连重放", exc_info=True)

    async def _send_for_turn(self, turn: TurnContext, text: str) -> None:
        await self._deliver(text, turn.session_id)
        if turn.proactive and not turn.sent:
            self._initiative.record_proactive(datetime.now(UTC))
        turn.sent = True

    async def _deliver(self, text: str, session_id: str) -> None:
        checked = check(text)
        if not checked.text:
            return
        if checked.rules:
            _LOG.warning("出站检查命中：%s", checked.rules)
        await self._control.deliver(checked.text, session_id=session_id)

    async def _publish_context(self) -> None:
        try:
            await self._control.set_aliya_context(
                AliyaContext(
                    open_topics=self._memory.load_open_topics()[
                        : self._config.memory.open_topics_limit
                    ],
                    unspoken=self._memory.load_unspoken(),
                    phase=phase_for(
                        now=datetime.now(UTC),
                        tasks=self._task_count,
                        crisis=False,
                    ),
                )
            )
        except Exception:
            _LOG.debug("回传 AliyaContext 失败", exc_info=True)
