"""Aliya 运行时：串行回合、事件缓存与自主 tick。"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from uuid import uuid4

from openai.types.chat import ChatCompletionMessageParam

from agent.aliya.initiative import Initiative, PreferenceGate, TimeSignals
from agent.aliya.memory import MemoryKind, MemoryScope, MemoryStore
from agent.aliya.ports import ControlPort
from agent.aliya.prompt.builder import PromptBuilder
from agent.aliya.prompt.loader import load_charter, load_persona
from agent.aliya.turn.context import TurnContext
from agent.aliya.turn.postcheck import check
from agent.aliya.turn.runner import TurnRunner
from agent.aliya.turn.tools import build_tools
from agent.cosmos.contracts import (
    AgentProgress,
    AgentRuntimeState,
    AliyaContext,
    CosmosEvent,
    DesktopSnapshot,
    DispatchIntent,
    LinkState,
    PresenceSnapshot,
    ReadablePresence,
    WorldInference,
)
from core.agent import MCPManager, SkillLibrary, Tool, ToolDefinitionError, ToolRegistry
from core.config.settings import AgentAliyaSettings
from core.llm import LLMService
from core.logger import log

type Phase = Literal["daily", "maintenance", "crisis", "rest"]

_REPORT_TOPICS = {"agent.result", "dispatch.finished", "agent.abnormal"}
#: task_status 的查询窗口；指定 id 查不到时据此提示「可能不在最近 N 条」。
_TASK_QUERY_LIMIT = 50
#: 跟进意图最长可预约 7 天；越界输入在工具边界收敛。
_MAX_FOLLOWUP_MINUTES = 7 * 24 * 60
#: 主动开口最长可延后 24 小时。
_MAX_DEFER_MINUTES = 24 * 60
#: 到期跟进被取消或失败后，延后多久再试。
_INTENT_RETRY_MINUTES = 30
#: 注入上下文的最近事实条数；事实只写不读，等于没记。
_FACTS_IN_CONTEXT = 8
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


_STATUS_LABELS = {
    "ok": "完成",
    "partial": "部分完成",
    "failed": "失败",
    "cancelled": "已取消",
}
_ABNORMAL_LABELS = {
    "timeout": "超时",
    "stalled": "卡住",
    "error": "出错",
    "internal": "内部错误",
}
#: 只做保守的显式表达匹配，避免把普通抱怨误判成危机。
_CRISIS_WORDS = ("不想活", "活不下去", "自杀", "结束生命", "伤害自己")


def _is_crisis(text: str) -> bool:
    return any(word in text for word in _CRISIS_WORDS)


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _gap_text(now: datetime, last_user_at: datetime | None) -> str:
    if last_user_at is None:
        return "很久"
    seconds = max(0.0, (now - last_user_at).total_seconds())
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))} 分钟"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} 小时".replace(".0", "")
    return f"{seconds / 86400:.1f} 天".replace(".0", "")


def _mapping(value: object) -> Mapping[str, object]:
    return cast("Mapping[str, object]", value) if isinstance(value, Mapping) else {}


def remember_result(payload: Mapping[str, object], counts: dict[str, int]) -> None:
    """记一次单条结果汇报；只统计带 `dispatch_id` 的批次任务。"""
    dispatch_id = _text(payload.get("dispatch_id"))
    if dispatch_id:
        counts[dispatch_id] = counts.get(dispatch_id, 0) + 1


def batch_needs_report(outcome: Mapping[str, object], reported: int) -> bool:
    """整批汇总是否还要单独汇报。

    批次成功、且每个结果都已通过 `agent.result` 汇报过时返回 False，
    避免单任务场景下同一件事被汇报两遍；部分失败或取消仍要汇总。
    """
    status = _text(outcome.get("status"))
    results = outcome.get("results")
    total = len(cast("list[object]", results)) if isinstance(results, list) else 0
    return not (status == "ok" and total > 0 and reported >= total)


def event_brief(event: CosmosEvent) -> str:
    """把真实事件载荷转成模型可读的一段话。

    cosmos 的 `agent.result` / `dispatch.finished` 都把摘要放在嵌套字段里，
    不能只读顶层 `summary`；否则汇报回合永远只能看到占位文本。
    """
    payload = event.payload
    if event.topic == "agent.result":
        result = _mapping(payload.get("result"))
        summary = _text(result.get("summary")) or "任务有结果了"
        status = _STATUS_LABELS.get(_text(result.get("status")), "有结果")
        lines = [f"任务{status}：{summary}"]
        error = _mapping(result.get("error"))
        message = _text(error.get("message"))
        if message:
            lines.append(f"原因：{message}")
        hint = _text(error.get("user_hint"))
        if hint:
            lines.append(f"提示：{hint}")
        # 摘要只说「成没成」，正文才是她要转述给用户的东西；不给她只能让用户自己贴
        content = _text(result.get("content"))
        if content:
            lines.append("内容：\n" + content)
        if result.get("truncated") is True:
            lines.append("（结果已截断）")
        return "\n".join(lines)
    if event.topic == "dispatch.finished":
        outcome = _mapping(payload.get("outcome"))
        summary = _text(outcome.get("summary")) or "这一批做完了"
        status = _STATUS_LABELS.get(_text(outcome.get("status")), "有结果")
        lines = [f"这一批{status}：{summary}"]
        results = outcome.get("results")
        if isinstance(results, list):
            for item in cast("list[object]", results)[:3]:
                data = _mapping(item)
                item_summary = _text(data.get("summary"))
                if item_summary:
                    lines.append(f"- {item_summary}")
                content = _text(data.get("content"))
                if content:
                    lines.append("  " + content)
        return "\n".join(lines)
    if event.topic == "agent.progress":
        label = _text(payload.get("label")) or "任务"
        step = _text(payload.get("step")) or _text(payload.get("message"))
        progress = payload.get("progress")
        percent = ""
        if isinstance(progress, int | float):
            percent = f"（{int(progress * 100)}%）"
        return f"{label}{percent}：{step}" if step else f"{label}{percent}有进展"
    if event.topic == "agent.abnormal":
        reason = _ABNORMAL_LABELS.get(_text(payload.get("reason")), "出了点问题")
        error = _mapping(payload.get("error"))
        message = _text(error.get("message"))
        lines = [f"任务{reason}" + (f"：{message}" if message else "")]
        hint = _text(error.get("user_hint"))
        if hint:
            lines.append(f"提示：{hint}")
        return "\n".join(lines)
    return "（收到一条进度）"


class AliyaRuntime:
    def __init__(
        self,
        *,
        control: ControlPort,
        llm: LLMService,
        config: AgentAliyaSettings,
        memory: MemoryStore | None = None,
    ) -> None:
        self._control: ControlPort = control
        self._llm: LLMService = llm
        self._config: AgentAliyaSettings = config
        self._memory: MemoryStore = memory or MemoryStore(config.memory.dir)
        self._queue: asyncio.Queue[TurnContext] = asyncio.Queue(maxsize=32)
        self._worker: asyncio.Task[None] | None = None
        self._ticker: asyncio.Task[None] | None = None
        self._consumers: list[asyncio.Task[None]] = []
        self._seen: set[str] = set()
        self._current_turn: TurnContext | None = None
        self._presence: ReadablePresence | None = None
        self._desktop: DesktopSnapshot | None = None
        self._world: WorldInference | None = None
        self._agent_states: dict[str, AgentRuntimeState] = {}
        self._progress: dict[str, AgentProgress] = {}
        self._pending_proactive: list[TurnContext] = []
        #: 每个 dispatch 已通过 agent.result 汇报过的任务数，用于整批去重
        self._reported_by_dispatch: dict[str, int] = {}
        #: 主动开口的延后窗口；到点后清空并恢复常规判断。
        self._defer_until: datetime | None = None
        self._link: LinkState = "up"
        self._last_user_at: datetime | None = None
        self._last_session_id: str = ""
        self._task_count: int = 0
        self._crisis: bool = False
        self._initiative: Initiative = Initiative(config.initiative)
        self._preference_gate: PreferenceGate = PreferenceGate(config.initiative.prefer_when)
        self._runner: TurnRunner | None = None
        self._proactive_runner: TurnRunner | None = None
        self._mcp: MCPManager | None = None
        self._skills: SkillLibrary = SkillLibrary()
        self._started: bool = False
        self._pending_inbound: int = 0
        self._closed: asyncio.Event = asyncio.Event()

    async def start(self) -> None:
        if self._started:
            return
        _ = await self._control.handshake()
        await self._prime_state()
        if self._config.memory.enabled:
            _ = self._memory.prune_turns(self._config.memory.max_turns)
            if self._config.memory.anchors and not self._memory.load_anchors():
                self._memory.save_anchors(self._config.memory.anchors)
        persona = load_persona(self._config.persona.source)
        for include in self._config.persona.includes:
            persona += "\n\n" + load_persona(include)
        charter = load_charter(self._config.persona.charter)
        tools = build_tools(self)
        proactive_tools = build_tools(self, proactive=True)
        try:
            extra_index = await self._attach_external_tools(tools, proactive_tools)
        except BaseException:
            # MCP 已经连上但后续装配失败或被取消时，不能把连接留在后台
            await self._stop_mcp()
            raise
        self._runner = TurnRunner(
            llm=self._llm,
            tools=tools,
            builder=PromptBuilder(
                persona=persona,
                charter=charter,
                tools=tools.tools,
                extra_index=extra_index,
            ),
            config=self._config,
        )
        self._proactive_runner = TurnRunner(
            llm=self._llm,
            tools=proactive_tools,
            builder=PromptBuilder(
                persona=persona,
                charter=charter,
                tools=proactive_tools.tools,
                extra_index=extra_index,
            ),
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
        log.info(
            "Aliya 运行时已启动",
            人格字数=len(persona),
            准则字数=len(charter),
            普通工具数=len(tools.tools),
            主动工具数=len(proactive_tools.tools),
            MCP工具数=len(self._mcp.tools) if self._mcp is not None else 0,
            技能数=len(self._skills),
            订阅主题数=len(_EVENT_TOPICS),
            记忆启用=self._config.memory.enabled,
        )

    async def _attach_external_tools(
        self, tools: ToolRegistry, proactive_tools: ToolRegistry
    ) -> str:
        """把 MCP 工具与技能库接到回合工具面上。

        返回技能索引文本，由 PromptBuilder 拼进 system；空库返回空串。
        MCP 只进普通回合：主动回合是无人值守的，不应自行调用任意外部工具。
        MCP 建连失败直接抛出（工具不能无声消失）；单个工具重名只跳过并告警。
        """
        if self._config.tools.mcp_enabled:
            manager = MCPManager.from_file()
            await manager.start()
            self._mcp = manager
            self._register_quietly(tools, manager.tools)
        if not self._config.tools.skills_enabled:
            return ""
        library = SkillLibrary.load()
        skill_tools = library.tools()
        self._register_quietly(tools, skill_tools)
        self._register_quietly(proactive_tools, skill_tools)
        self._skills = library
        return library.index()

    @staticmethod
    def _register_quietly(registry: ToolRegistry, items: Sequence[Tool]) -> None:
        """逐个注册；重名工具跳过，避免一个外部工具把整个进程拖垮。"""
        for item in items:
            try:
                registry.register(item)
            except ToolDefinitionError:
                log.warning("工具重名，已跳过", 工具=item.name)

    async def _stop_mcp(self) -> None:
        """断开 MCP；失败只记日志，不阻断停止流程。"""
        manager, self._mcp = self._mcp, None
        if manager is None:
            return
        try:
            await manager.stop()
        except Exception as exc:
            log.warning(
                "断开 MCP 失败",
                错误类型=type(exc).__name__,
                错误消息=str(exc),
            )

    async def _prime_state(self) -> None:
        """启动时拉一次状态，避免主动回合在首个事件到达前没有任何上下文。"""
        try:
            self._desktop = await self._control.describe_desktop()
            self._task_count = sum(
                1 for entry in self._desktop.entries if entry.status in {"running", "queued"}
            )
        except Exception as exc:
            log.debug(
                "启动拉取桌面失败，按空快照继续",
                错误类型=type(exc).__name__,
                错误消息=str(exc),
            )
        try:
            view = await self._control.get_world_view()
        except Exception as exc:
            log.debug(
                "启动拉取世界推理失败，按无缓存继续",
                错误类型=type(exc).__name__,
                错误消息=str(exc),
            )
        else:
            if view is not None:
                _ = self._accept_world(view)
        try:
            self._link = await self._control.get_link_state()
        except Exception as exc:
            log.debug(
                "启动拉取链路状态失败，按 up 继续",
                错误类型=type(exc).__name__,
                错误消息=str(exc),
            )
        try:
            fetch = getattr(self._control, "fetch_presence", None)
            if fetch is None:
                snapshot = self._control.get_presence()
            else:
                snapshot = await cast(Callable[[], Awaitable[PresenceSnapshot]], fetch)()
            self._presence = snapshot.readable
        except Exception as exc:
            log.debug(
                "启动拉取状态失败，按无状态继续",
                错误类型=type(exc).__name__,
                错误消息=str(exc),
            )

    async def _recover_unanswered(self) -> None:
        """启动时补跑已落盘但未回复的回合；幂等键为 message_id。"""
        if not self._config.memory.enabled:
            return
        for stored in self._memory.unanswered_turns():
            if not stored.message_id or stored.message_id in self._seen:
                continue
            turn = TurnContext(
                user_text=stored.text,
                session_id=stored.session_id,
                message_id=stored.message_id,
                presence=self._presence.summary if self._presence else None,
                tasks=self._tasks_text(),
                desktop=self._desktop_text(),
                world=self._world_text(),
            )
            self._bind_turn(turn)
            self._seen.add(stored.message_id)
            _ = await self._enqueue(turn)

    async def stop(self) -> None:
        tasks = [self._worker, self._ticker, *self._consumers]
        for task in tasks:
            if task is not None:
                _ = task.cancel()
        _ = await asyncio.gather(
            *(task for task in tasks if task is not None),
            return_exceptions=True,
        )
        await self._stop_mcp()
        self._worker = None
        self._ticker = None
        self._consumers = []
        self._started = False
        self._closed.set()
        log.info("Aliya 运行时已停止")

    async def wait_closed(self) -> None:
        _ = await self._closed.wait()

    async def wait_idle(self) -> None:
        """等待当前入站与队列中的回合处理完。"""
        await asyncio.sleep(0.02)
        while (
            self._pending_inbound > 0 or not self._queue.empty() or self._current_turn is not None
        ):
            await asyncio.sleep(0.01)
        await self._queue.join()

    # ---------- 工具面调用的运行时能力 ----------

    def now(self) -> datetime:
        """工具面读取的时间；与主动判断共用控制面校准过的来源。"""
        return self._now()

    async def send(self, text: str) -> None:
        turn = self._current_turn
        if turn is None:
            await self._deliver(text, self._last_session_id)
            return
        was_sent = turn.sent
        accepted = await turn.send(text)
        if not accepted:
            raise RuntimeError("现在发不出去")
        if turn.proactive and not was_sent:
            self._initiative.record_proactive(self._now())

    async def describe_desktop(self) -> str:
        snapshot = await self._control.describe_desktop()
        lines = [snapshot.summary]
        # 带上 id：dispatch_task 的 agent_id 只能从这里获得，缺了模型就没法精确指定工具。
        for entry in snapshot.entries:
            capabilities = "、".join(entry.card.capabilities[:3])
            line = f"{entry.card.id}｜{entry.card.name}（{entry.status}）：{entry.readable.summary}"
            if capabilities:
                line += f"；能做：{capabilities}"
            lines.append(line)
        return "\n".join(lines)

    async def dispatch(
        self,
        task_text: str,
        *,
        agent_id: str | None = None,
        params: dict[str, object] | None = None,
        hint: str | None = None,
    ) -> str:
        hint_parts = [hint] if hint else []
        if agent_id is None and self._config.tools.prefer_agents:
            hint_parts.append("优先使用：" + "、".join(self._config.tools.prefer_agents))
        if self._config.tools.max_parallel_dispatch:
            hint_parts.append(f"最多并行 {self._config.tools.max_parallel_dispatch} 个")
        plan = await self._control.plan(
            DispatchIntent(
                source="natural_language",
                session_id=self._last_session_id,
                user_text=task_text,
                agent_id=agent_id,
                params=params,
                hint="；".join(hint_parts) or None,
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
        # 任务 id 必须回给模型，否则后续 task_status / cancel_task / retry_task 无从指定。
        done = "、".join(
            f"{task.label}（{task_id}）"
            for task, task_id in zip(plan.tasks, handle.task_ids, strict=False)
        ) or "、".join(handle.task_ids)
        if handle.status == "partial" and handle.rejected:
            reasons = "；".join(item.reason for item in handle.rejected[:3])
            return f"已经派人去做了：{done}（有些没派成：{reasons}）"
        return f"已经派人去做了：{done}"

    async def task_status(self, task_id: str | None = None) -> str:
        tasks = await self._control.list_tasks(None, _TASK_QUERY_LIMIT)
        if task_id is not None:
            matched = [task for task in tasks if task.task_id == task_id]
            if not matched:
                return f"没找到任务 {task_id}，可能已经结束，或者不在最近 {_TASK_QUERY_LIMIT} 条里"
            tasks = matched
        if not tasks:
            return "现在没有在做的任务"
        lines: list[str] = []
        for task in tasks[:5]:
            progress = f"（{int(task.progress * 100)}%）" if task.progress is not None else ""
            lines.append(f"{task.task_id}｜{task.label}：{task.status}{progress}")
        return "\n".join(lines)

    async def cancel(self, task_id: str) -> str:
        await self._control.cancel_task(task_id)
        return f"已取消任务 {task_id}"

    async def retry(self, task_id: str) -> str:
        handle = await self._control.retry_task(task_id)
        return f"已重试，新任务 {handle.task_id}：{handle.label}"

    async def refresh_world(self) -> str:
        view = await self._control.refresh_world()
        if view is None:
            return "现在看不出什么"
        _ = self._accept_world(view)
        return self._world_text() or view.snapshot

    async def remember(self, note: str, kind: MemoryKind) -> str:
        if not self._config.memory.enabled:
            return "这件事先不记"
        return "记住了" if self._memory.remember(note, kind) else "这件事先不记"

    async def check_memory(self, scope: MemoryScope | None = None) -> str:
        """查看长期记忆；不传范围时返回所有非空分类。"""
        if not self._config.memory.enabled:
            return "记忆没有开"
        lines: list[str] = []
        if scope in (None, "summary"):
            summary = self._memory.load_summary().strip()
            if summary:
                lines.append("摘要：" + summary)
        if scope in (None, "anchor"):
            anchors = self._memory.load_anchors()
            if anchors:
                lines.append("锚点：" + "、".join(anchors))
        if scope in (None, "open_topic"):
            topics = self._memory.load_open_topics()
            if topics:
                lines.append("未收尾：" + "、".join(topics))
        if scope in (None, "unspoken"):
            unspoken = self._memory.load_unspoken()
            if unspoken:
                lines.append("未说出口：" + "；".join(unspoken))
        if scope in (None, "fact"):
            facts = self._memory.load_facts()
            if facts:
                lines.append("事实：" + "；".join(facts[-10:]))
        if scope in (None, "intent"):
            intents = self._memory.load_intents()
            if intents:
                lines.append(
                    "待跟进："
                    + "；".join(
                        f"{item.due_at:%m-%d %H:%M} {item.text}" for item in intents[:10]
                    )
                )
        return "\n".join(lines) or "记忆里还是空的"

    async def resolve_topic(self, topic: str) -> str:
        """收尾一条未收尾话题，避免主动判断一直把它当成未完成。"""
        if not self._config.memory.enabled:
            return "记忆没有开"
        if self._memory.resolve_open_topic(topic):
            return "已经收尾了"
        return f"没找到未收尾的话题：{topic}"

    async def plan_followup(self, text: str, after_minutes: int) -> str:
        """把一件事排进未来，到期后由 ticker 主动唤醒。"""
        if not self._config.memory.enabled:
            return "记忆没有开，安排不了"
        minutes = max(1, min(after_minutes, _MAX_FOLLOWUP_MINUTES))
        due = self._now() + timedelta(minutes=minutes)
        if not self._memory.add_intent(text, due_at=due):
            return "这件事先不记"
        return f"记下了，{due:%m-%d %H:%M} 左右提醒你"

    async def cancel_followup(self, text: str) -> str:
        """取消一条未来意图；用户说「不用提醒了」时调用。"""
        if not self._config.memory.enabled:
            return "记忆没有开"
        if self._memory.cancel_intent(text):
            return "已经取消了"
        return f"没找到这条跟进：{text}"

    async def defer(self, minutes: int, reason: str = "") -> str:
        """把下一次主动开口往后推；主动回合里同时让本回合静默。"""
        minutes = max(1, min(minutes, _MAX_DEFER_MINUTES))
        self._defer_until = self._now() + timedelta(minutes=minutes)
        turn = self._current_turn
        if turn is not None and turn.proactive:
            turn.silent = True
        log.info("主动开口已延后", 分钟=minutes, 原因=reason or "-")
        return f"好，{minutes} 分钟后再考虑"

    # ---------- 消费循环 ----------

    async def _consume_inbound(self) -> None:
        async for envelope in self._control.inbound():
            message_id = envelope.message.message_id
            try:
                if message_id in self._seen:
                    # 已落盘但上次确认失败：重放时补确认，不重复入队。
                    await self._ack(message_id)
                    continue
                at = envelope.message.at.isoformat() if envelope.message.at else ""
                if self._config.memory.enabled:
                    try:
                        _ = self._memory.record_inbound(
                            message_id,
                            envelope.message.text,
                            session_id=envelope.message.session_id,
                            at=at,
                        )
                    except OSError:
                        log.exception("入站消息落盘失败，暂不回执", 消息=message_id)
                        continue
                self._seen.add(message_id)
                await self._ack(message_id)
                log.debug(
                    "收到用户消息",
                    消息=message_id,
                    会话=envelope.message.session_id,
                    字数=len(envelope.message.text),
                    历史条数=len(envelope.conversation.turns),
                )
                self._last_session_id = envelope.message.session_id
                self._last_user_at = envelope.message.at or self._now()
                self._initiative.record_user_reply()
                self._cancel_pending_proactive()
                self._task_count = len(envelope.tasks)
                self._crisis = _is_crisis(envelope.message.text)
                if envelope.presence is not None:
                    self._presence = envelope.presence
                turn = TurnContext.from_envelope(envelope)
                turn.presence = turn.presence or (
                    self._presence.summary if self._presence else None
                )
                turn.tasks = turn.tasks or self._tasks_text()
                turn.desktop = self._desktop_text()
                turn.world = self._world_text()
                self._bind_turn(turn)
                self._pending_inbound += 1
                try:
                    _ = await self._enqueue(turn)
                finally:
                    self._pending_inbound -= 1
            except Exception:
                # 单条消息处理失败不能让入站消费永久停摆。
                log.exception("处理入站消息失败，跳过该条", 消息=message_id)

    async def _consume_events(self) -> None:
        try:
            cursors = self._memory.load_cursors()
        except OSError:
            log.exception("读取事件游标失败，本次从头订阅")
            cursors = {}
        since = min(cursors.values()) if cursors else None
        async for event in self._control.subscribe(
            _EVENT_TOPICS, since_sequence=since, cursors=cursors
        ):
            try:
                self._cache_event(event)
                report = self._should_report(event)
                log.debug(
                    "收到控制面事件",
                    主题=event.topic,
                    序号=event.sequence,
                    触发回合=report,
                )
                if report:
                    turn = TurnContext(
                        user_text=event_brief(event),
                        session_id=self._last_session_id,
                        presence=self._presence.summary if self._presence else None,
                        tasks=self._tasks_text(),
                        desktop=self._desktop_text(),
                        world=self._world_text(),
                        origin="event",
                        droppable=event.topic == "agent.progress",
                    )
                    self._bind_turn(turn)
                    _ = await self._enqueue(turn)
                try:
                    self._memory.save_cursor(event.sequence, topic=event.topic)
                except OSError as exc:
                    log.warning(
                        "事件游标落盘失败，重启后可能重复消费",
                        主题=event.topic,
                        序号=event.sequence,
                        错误类型=type(exc).__name__,
                        错误消息=str(exc),
                    )
            except Exception:
                # 单条事件处理失败不能让事件消费永久停摆。
                log.exception(
                    "处理控制面事件失败，跳过该条",
                    主题=event.topic,
                    序号=event.sequence,
                )

    def _should_report(self, event: CosmosEvent) -> bool:
        if event.topic == "dispatch.finished":
            outcome = _mapping(event.payload.get("outcome"))
            dispatch_id = _text(outcome.get("dispatch_id"))
            reported = self._reported_by_dispatch.pop(dispatch_id, 0) if dispatch_id else 0
            if len(self._reported_by_dispatch) > 200:
                self._reported_by_dispatch.clear()
            return batch_needs_report(outcome, reported)
        if event.topic in _REPORT_TOPICS:
            return True
        if event.topic != "agent.progress":
            return False
        mode = self._config.reply.progress_forwarding
        if mode == "always":
            return True
        if mode == "never":
            return False
        turn = self._current_turn
        return turn is not None and turn.origin == "user"

    def _cache_event(self, event: CosmosEvent) -> None:
        if event.topic == "agent.result":
            remember_result(event.payload, self._reported_by_dispatch)
        elif event.topic == "presence.changed":
            try:
                self._presence = ReadablePresence.model_validate(event.payload["readable"])
            except (KeyError, ValueError):
                return
        elif event.topic == "link.state.changed":
            state = event.payload.get("state")
            if state in {"up", "unstable", "down"}:
                self._link = cast("LinkState", state)
        elif event.topic == "world.inference":
            if not self._config.world.consume:
                return
            try:
                view = WorldInference.model_validate(event.payload.get("inference"))
            except (TypeError, ValueError):
                return
            _ = self._accept_world(view)
        elif event.topic == "desktop.layout.changed":
            try:
                self._desktop = DesktopSnapshot.model_validate(event.payload)
            except (TypeError, ValueError):
                return
        elif event.topic == "agent.state.changed":
            try:
                state = AgentRuntimeState.model_validate(event.payload)
            except (TypeError, ValueError):
                return
            self._agent_states[state.agent_id] = state
        elif event.topic == "agent.progress":
            try:
                progress = AgentProgress.model_validate(event.payload)
            except (TypeError, ValueError):
                return
            self._progress[progress.task_id] = progress
        elif event.topic == "user.active":
            at = event.payload.get("at")
            if isinstance(at, str):
                with contextlib.suppress(ValueError):
                    self._last_user_at = datetime.fromisoformat(at)
            self._initiative.record_user_reply()
            self._cancel_pending_proactive()

    async def _enqueue(self, turn: TurnContext) -> bool:
        """只丢可丢弃的进度汇报；用户消息、主动回合与唤醒事件继续排队。"""
        if turn.droppable:
            try:
                self._queue.put_nowait(turn)
            except asyncio.QueueFull:
                log.warning("回合队列已满，丢弃进度汇报", 来源=turn.origin)
                return False
            return True
        await self._queue.put(turn)
        return True

    def _cancel_pending_proactive(self) -> None:
        """用户刚有动作：取消尚未开始的主动回合，不打断进行中的回合。"""
        if not self._pending_proactive:
            return
        count = len(self._pending_proactive)
        for pending in self._pending_proactive:
            pending.canceled = True
        self._pending_proactive.clear()
        log.info("用户有动作，已取消待执行的主动回合", 数量=count)

    def _accept_world(self, view: WorldInference) -> bool:
        """世界推理按 revision 单调更新，旧版本直接丢弃。"""
        if self._world is not None and view.revision < self._world.revision:
            log.debug(
                "丢弃旧版本世界推理",
                收到版本=view.revision,
                当前版本=self._world.revision,
            )
            return False
        self._world = view
        log.debug(
            "接受世界推理",
            版本=view.revision,
            置信度=view.confidence,
            假设数=len(view.hypotheses),
            行动窗口数=len(view.action_windows),
        )
        return True

    async def _run_worker(self) -> None:
        while True:
            turn = await self._queue.get()
            if turn.origin == "proactive":
                with contextlib.suppress(ValueError):
                    self._pending_proactive.remove(turn)
                if turn.canceled:
                    # 意图在触发时没有删除；被用户活动取消后推后重试。
                    self._retry_intent(turn)
                    self._queue.task_done()
                    continue
            self._current_turn = turn
            turn.memory = self._compose_memory(turn.memory)
            started = time.monotonic()
            log.debug(
                "回合开始",
                来源=turn.origin,
                主动=turn.proactive,
                消息=turn.message_id or "-",
                输入字数=len(turn.user_text),
            )
            try:
                runner = self._proactive_runner if turn.proactive else self._runner
                if runner is None:
                    raise RuntimeError("回合执行器未初始化")
                await runner.run(turn=turn)
            except TimeoutError:
                log.warning(
                    "回合超时，发送失败提示",
                    来源=turn.origin,
                    超时秒=self._config.reply.turn_timeout_seconds,
                )
                await self._fallback(turn)
                self._retry_intent(turn)
            except Exception:
                log.exception("回合执行失败", 来源=turn.origin, 消息=turn.message_id or "-")
                await self._fallback(turn)
                self._retry_intent(turn)
            else:
                self._settle_intent(turn)
            finally:
                # task_done 必须最先执行：后续清理失败不能把 queue.join() 卡死。
                self._queue.task_done()
                self._current_turn = None
                try:
                    if self._config.memory.enabled and turn.replies:
                        prefix = (
                            f"{turn.message_id}:reply"
                            if turn.message_id
                            else f"{turn.origin}:{uuid4().hex}:reply"
                        )
                        for reply in turn.replies:
                            self._memory.append_turn(
                                prefix,
                                "aliya",
                                reply,
                                session_id=turn.session_id or self._last_session_id,
                            )
                    await self._maybe_compress_memory()
                    fields = {
                        "来源": turn.origin,
                        "耗时毫秒": round((time.monotonic() - started) * 1000),
                        "回复条数": len(turn.replies),
                        "已发送": turn.sent,
                    }
                    if turn.origin == "event":
                        log.debug("回合结束", face=None, **fields)
                    else:
                        log.info("回合结束", face=None, **fields)
                    await self._publish_context()
                except Exception:
                    log.exception(
                        "回合收尾失败",
                        来源=turn.origin,
                        消息=turn.message_id or "-",
                    )

    async def _fallback(self, turn: TurnContext) -> None:
        """失败提示以 system 身份投递：不算 Aliya 的回复，也不写入记忆。"""
        if turn.sent or turn.silent or turn.origin != "user":
            return
        try:
            _ = await turn.send_system(self._config.reply.fallback_text)
        except Exception:
            log.exception("发送失败提示失败", 来源=turn.origin)

    async def _run_ticker(self) -> None:
        while True:
            await asyncio.sleep(self._config.initiative.tick_seconds)
            try:
                await self._tick_once()
            except Exception:
                # 单轮失败不能让主动行为永久停摆。
                log.exception("主动行为检查失败，跳过本轮")

    async def _fire_due_intent(self, now: datetime) -> bool:
        """到期意图优先于常规主动判断；仍受链路、状态与打扰预算约束。"""
        if not self._config.memory.enabled:
            return False
        due = self._memory.due_intents(now, limit=1)
        if not due:
            return False
        allowed = self._initiative.allows(now=now, presence=self._presence, link=self._link)
        if not allowed.should_speak:
            log.debug("到期跟进暂缓", 原因=allowed.reason)
            return False
        intent = due[0]
        turn = TurnContext(
            user_text=(
                f"你之前给自己安排了一件事：{intent.text}"
                f"\n现在是 {now:%H:%M}，到时间了，而且没有人说话。"
                "要不要说、说什么由你决定。"
            ),
            session_id=self._last_session_id,
            presence=self._presence.summary if self._presence else None,
            tasks=self._tasks_text(),
            desktop=self._desktop_text(),
            world=self._world_text(),
            proactive=True,
            origin="proactive",
            intent_text=intent.text,
        )
        self._bind_turn(turn)
        self._pending_proactive.append(turn)
        _ = await self._enqueue(turn)
        log.info("到期跟进已排队", 内容=intent.text[:40], 到期=intent.due_at.isoformat())
        return True

    def _settle_intent(self, turn: TurnContext) -> None:
        """回合正常结束：到期意图视为已处理，从队列里移除。"""
        if not turn.intent_text:
            return
        _ = self._memory.pop_intent(turn.intent_text)
        log.debug("到期跟进已处理", 内容=turn.intent_text[:40])

    def _retry_intent(self, turn: TurnContext) -> None:
        """回合被取消或失败：把意图推后重试，不能静默丢掉。

        条目已被显式取消时不再复活：用户可能恰好在同一时间说了「不用提醒了」。
        """
        if not turn.intent_text:
            return
        if not any(item.text == turn.intent_text for item in self._memory.load_intents()):
            log.debug("到期跟进已被取消，不再重排", 内容=turn.intent_text[:40])
            return
        due = self._now() + timedelta(minutes=_INTENT_RETRY_MINUTES)
        _ = self._memory.add_intent(turn.intent_text, due_at=due)
        log.info("到期跟进稍后重试", 内容=turn.intent_text[:40], 分钟=_INTENT_RETRY_MINUTES)

    async def _tick_once(self) -> None:
        if not self._config.initiative.enabled or self._pending_proactive:
            return
        now = self._now()
        if self._defer_until is not None:
            if now < self._defer_until:
                return
            self._defer_until = None
            log.debug("延后窗口结束，恢复主动判断")
        if await self._fire_due_intent(now):
            return
        decision = self._initiative.should_speak(
            now=now,
            presence=self._presence,
            link=self._link,
            last_user_at=self._last_user_at,
        )
        if not decision.should_speak:
            return
        preference = self._preference_gate.match(self._time_signals(now))
        if not preference.matched:
            log.debug(
                "主动时间门控未命中",
                原因=preference.reason,
                距上次对话秒=(
                    None
                    if self._last_user_at is None
                    else round((now - self._last_user_at).total_seconds())
                ),
            )
            return
        user_text = (
            f"现在是 {now:%H:%M}，距上次对话 {_gap_text(now, self._last_user_at)}。"
            "\n（现在没有人说话。）"
        )
        if preference.condition:
            user_text += f"\n可以考虑：{preference.condition}"
        turn = TurnContext(
            user_text=user_text,
            session_id=self._last_session_id,
            presence=self._presence.summary if self._presence else None,
            tasks=self._tasks_text(),
            desktop=self._desktop_text(),
            world=self._world_text(),
            proactive=True,
            origin="proactive",
        )
        self._bind_turn(turn)
        self._pending_proactive.append(turn)
        _ = await self._enqueue(turn)
        log.info(
            "主动回合已排队",
            触发原因=decision.reason,
            偏好条件=preference.condition or "-",
            时间=f"{now:%H:%M}",
        )

    def _time_signals(self, now: datetime | None = None) -> TimeSignals:
        """收集时间层可用的语义信号；只读缓存，不触发额外拉取。"""
        topics = tuple(self._memory.load_open_topics()) if self._config.memory.enabled else ()
        return TimeSignals(
            now=now or self._now(),
            last_user_at=self._last_user_at,
            open_topics=topics,
            world=self._world_text() or "",
            desktop=self._desktop_text() or "",
            presence=self._presence.summary if self._presence else "",
            world_at=self._world.generated_at if self._world is not None else None,
            desktop_at=self._desktop.generated_at if self._desktop is not None else None,
        )

    async def _maybe_compress_memory(self) -> None:
        """本地轮次超过上限时生成滚动摘要，再裁掉旧轮次。"""
        if not self._config.memory.enabled:
            return
        max_turns = self._config.memory.max_turns
        turns = self._memory.recent_turns(max_turns + 1)
        if len(turns) <= max_turns:
            return
        transcript = "\n".join(
            f"{'她' if turn.role == 'user' else '我'}：{turn.text}"
            for turn in turns
            if turn.text.strip()
        )[-8000:]
        previous = self._memory.load_summary().strip()
        prompt: list[ChatCompletionMessageParam] = [
            {
                "role": "system",
                "content": (
                    f"把对话压成不超过 {self._config.memory.summary_chars} 字的中文摘要。"
                    "只保留稳定事实、关系和未完成的事，不写情绪数值与内部标识。"
                ),
            },
            {
                "role": "user",
                "content": (f"已有摘要：{previous}\n\n" if previous else "") + transcript,
            },
        ]
        try:
            timeout = min(20, self._config.reply.turn_timeout_seconds)
            summary = str(await asyncio.wait_for(self._llm.chat(prompt), timeout=timeout))
        except Exception as exc:
            log.warning(
                "记忆压缩失败，改用截断摘要",
                错误类型=type(exc).__name__,
                错误消息=str(exc),
                轮次数=len(turns),
            )
            summary = transcript
        summary = summary.strip()[: self._config.memory.summary_chars]
        if not summary:
            summary = transcript[: self._config.memory.summary_chars]
        try:
            self._memory.save_summary(summary)
            # 留出一半余量，避免之后每回合都触发压缩。
            _ = self._memory.prune_turns(max(1, max_turns // 2))
        except OSError:
            log.exception("记忆摘要落盘失败")
        else:
            log.info(
                "记忆已压缩",
                保留条数=max(1, max_turns // 2),
                摘要字数=len(summary),
            )

    def _compose_memory(self, base: str | None) -> str | None:
        """cosmos 摘要 + 长期记忆；锚点与私密线不参与摘要。"""
        if not self._config.memory.enabled:
            return base.strip() if base and base.strip() else None
        parts: list[str] = []
        if base and base.strip():
            parts.append(base.strip())
        summary = self._memory.load_summary().strip()
        if summary:
            parts.append("长期摘要：" + summary[: self._config.memory.summary_chars])
        anchors = self._memory.load_anchors()
        if anchors:
            parts.append("锚点：" + "、".join(anchors))
        facts = self._memory.load_facts()[-_FACTS_IN_CONTEXT:]
        if facts:
            parts.append("记得：" + "；".join(facts))
        topics = self._memory.load_open_topics()[: self._config.memory.open_topics_limit]
        if topics:
            parts.append("未收尾：" + "、".join(topics))
        if self._config.memory.private_threads:
            unspoken = self._memory.load_unspoken()[:3]
            if unspoken:
                parts.append("只对你说过：" + "；".join(unspoken))
        return "\n".join(parts) or None

    def _desktop_text(self) -> str | None:
        """只在平板上有事发生时注入；全空闲不写，避免模型每回合复述「都闲着」。

        工具清单本身由提示词里的工具索引提供，她想知道细节随时可以调 see_desktop。
        """
        snapshot = self._desktop
        if snapshot is None or not snapshot.summary.strip():
            return None
        if snapshot.entries and all(entry.status == "idle" for entry in snapshot.entries):
            return None
        return f"平板：{snapshot.summary}"

    def _tasks_text(self) -> str | None:
        """把运行态与最近进度压成一段；只给展示名，不暴露 agent_id。"""
        lines: list[str] = []
        running = sum(1 for state in self._agent_states.values() if state.status == "running")
        queued = sum(1 for state in self._agent_states.values() if state.status == "queued")
        if running or queued:
            lines.append(f"正在运行 {running} 个，排队 {queued} 个")
        for progress in self._progress.values():
            detail = progress.step or progress.message
            if not detail:
                continue
            percent = (
                f"（{int(progress.progress * 100)}%）" if progress.progress is not None else ""
            )
            lines.append(f"{progress.label}{percent}：{detail}")
        return "\n".join(lines) or None

    def _world_text(self) -> str | None:
        """把世界推理压成提示词可用的一段，不暴露内部标识。"""
        if not self._config.world.provide_context:
            return None
        view = self._world
        if view is None:
            return None
        parts = [view.snapshot]
        limit = self._config.world.max_hypotheses_used
        if limit > 0:
            hypotheses = [
                item
                for item in view.hypotheses
                if item.confidence >= self._config.world.trust_threshold
            ][:limit]
            if hypotheses:
                parts.append(
                    "可能："
                    + "；".join(f"{item.statement}（{item.confidence:.0%}）" for item in hypotheses)
                )
        if view.action_windows:
            parts.append(
                "时机："
                + "；".join(f"{item.window} {item.suggestion}" for item in view.action_windows[:2])
            )
        return "\n".join(parts)

    def _now(self) -> datetime:
        """优先用控制面校准过的时间；测试桩没有 `now` 时退回本地时间。"""
        provider = getattr(self._control, "now", None)
        if callable(provider):
            try:
                value = provider()
            except Exception as exc:
                log.debug(
                    "读取服务端时间失败，退回本地时间",
                    错误类型=type(exc).__name__,
                    错误消息=str(exc),
                )
            else:
                if isinstance(value, datetime):
                    return value
        return datetime.now(UTC)

    async def _ack(self, message_id: str) -> None:
        try:
            await self._control.ack_inbound(message_id)
        except Exception as exc:
            log.warning(
                "入站确认失败，等待重连重放",
                消息=message_id,
                错误类型=type(exc).__name__,
                错误消息=str(exc),
            )

    def _bind_turn(self, turn: TurnContext) -> None:
        """绑定出站通道与回合时间：正常回复走 Aliya 身份，失败提示走 system 身份。"""
        turn.sender = lambda text, turn=turn: self._send_for_turn(turn, text)
        turn.system_sender = lambda text, turn=turn: self._send_system(turn, text)
        if turn.now is None:
            turn.now = self._now()

    async def _send_for_turn(self, turn: TurnContext, text: str) -> bool:
        accepted = await self._send(turn, text)
        if accepted and turn.proactive and not turn.sent:
            self._initiative.record_proactive(self._now())
        return accepted

    async def _send(self, turn: TurnContext, text: str) -> bool:
        """按回合来源选择通道：用户回复 / 主动消息 / 事件汇报。"""
        prepared = await self._prepare_outbound(text)
        if not prepared:
            return False
        if turn.origin == "proactive":
            receipt = await self._control.send_proactive(prepared)
        elif turn.origin == "event":
            receipt = await self._control.notify(prepared)
        else:
            await self._control.deliver(prepared, session_id=turn.session_id)
            log.debug(
                "回复已投递",
                会话=turn.session_id,
                字数=len(prepared),
                消息=turn.message_id or "-",
            )
            return True
        if not receipt.accepted and receipt.deliver_after is None:
            log.info("主动消息被拒", 原因=receipt.reason, 来源=turn.origin)
            return False
        if receipt.deliver_after is not None:
            log.debug(
                "主动消息延迟投递",
                来源=turn.origin,
                投递时间=receipt.deliver_after.isoformat(),
            )
        return True

    async def _deliver(self, text: str, session_id: str) -> None:
        prepared = await self._prepare_outbound(text)
        if not prepared:
            return
        await self._control.deliver(prepared, session_id=session_id)

    async def _send_system(self, turn: TurnContext, text: str) -> bool:
        """以 system 消息投递失败提示。

        不走人格出站检查，避免重写时又变成 Aliya 的口吻；终端按 `system`
        处理，不写对话轮次、不记 Aliya 统计，也不进她的记忆。
        """
        session_id = turn.session_id or self._last_session_id
        if not session_id:
            log.warning("没有会话可投递系统提示", 来源=turn.origin)
            return False
        try:
            await self._control.deliver(text, session_id=session_id, kind="system")
        except Exception:
            log.exception("发送系统提示失败", 来源=turn.origin, 会话=session_id)
            return False
        log.info("系统提示已投递", 来源=turn.origin, 会话=session_id, 字数=len(text))
        return True

    async def _prepare_outbound(self, text: str) -> str:
        """按配置跑出站检查；命中先让模型重写一次，仍不通过则降级删除。"""
        checked = check(text, self._config.reply.post_checks)
        if not checked.text:
            log.debug("出站内容被检查清空，放弃发送", 原字数=len(text))
            return ""
        if checked.rules:
            log.warning(
                "出站检查命中",
                规则="、".join(checked.rules),
                原字数=len(text),
            )
            rewritten = await self._rewrite_outbound(text, checked.rules)
            if rewritten:
                checked = check(rewritten, self._config.reply.post_checks)
                if not checked.text:
                    log.debug("重写后仍被检查清空，放弃发送", 规则="、".join(checked.rules))
                    return ""
        limit = self._config.reply.max_chars_per_message
        if limit and len(checked.text) > limit:
            log.debug("出站内容超长，已截断", 原字数=len(checked.text), 上限=limit)
            return checked.text[: max(0, limit - 1)].rstrip() + "…"
        return checked.text

    async def _rewrite_outbound(self, text: str, rules: tuple[str, ...]) -> str | None:
        """模型可用时重写一次；不可用或失败返回 None，走降级删除。"""
        prompt: list[ChatCompletionMessageParam] = [
            {
                "role": "system",
                "content": (
                    "把这句话改写成自然口语，保留原意；不要括号旁白、不要工程词、"
                    "不要内部标识、不要说破状态。只输出改写后的句子。"
                ),
            },
            {"role": "user", "content": f"原句：{text}\n命中：{'、'.join(rules)}"},
        ]
        try:
            rewritten = str(await asyncio.wait_for(self._llm.chat(prompt), timeout=10))
        except Exception as exc:
            log.warning(
                "出站重写失败，走降级删除",
                错误类型=type(exc).__name__,
                错误消息=str(exc),
            )
            return None
        return rewritten.strip() or None

    async def _publish_context(self) -> None:
        enabled = self._config.memory.enabled
        try:
            await self._control.set_aliya_context(
                AliyaContext(
                    open_topics=(
                        self._memory.load_open_topics()[: self._config.memory.open_topics_limit]
                        if enabled
                        else []
                    ),
                    unspoken=(
                        self._memory.load_unspoken()
                        if enabled and self._config.memory.private_threads
                        else []
                    ),
                    phase=phase_for(
                        now=self._now(),
                        tasks=self._task_count,
                        crisis=self._crisis,
                    ),
                )
            )
        except Exception as exc:
            log.debug(
                "回传 AliyaContext 失败",
                错误类型=type(exc).__name__,
                错误消息=str(exc),
            )
