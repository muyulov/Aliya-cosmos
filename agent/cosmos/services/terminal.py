"""通信终端：用户与 Aliya 之间的唯一出入口。

三条不变量（设计 06 章 1 节）：单一入口、单一出口、内容不改写。

- `ingest()` 是唯一入口：生成标识、刷新链路与状态、组装信封。
- `deliver()` 是唯一出口：只做分段、截断、脱敏扫描，不改措辞与标点。
- 意图分类已移除：所有用户消息统一交给 Aliya，终端不做路由判断。
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections import Counter, OrderedDict, deque
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path
from statistics import median
from typing import ClassVar, Literal, cast, override

from agent.cosmos.contracts import (
    HistoryPattern,
    InboundEnvelope,
    LinkState,
    MessageKind,
    MessageMeta,
    PresenceInput,
    PresenceSnapshot,
    ProactiveReceipt,
    TerminalMessage,
)
from agent.cosmos.services.conversation import ConversationService
from agent.cosmos.services.presence import PresenceService
from agent.cosmos.services.runtime import AgentRuntimeService
from agent.cosmos.transport import BusService, MemoryUserChannel, UserChannelPort
from core.config import AgentDeliverySettings, AgentProactiveSettings, AgentSettings
from core.service import ClockService, Service

#: 进入 `ConversationContext` 的消息类型：进度、结果与系统提示都不进对话轮次
CONVERSATION_KINDS: frozenset[str] = frozenset({"chat", "task", "query", "mixed"})

#: 客户端幂等键的保留条数；超出后淘汰最旧记录，避免无界增长
INBOUND_DEDUPE_LIMIT = 500


@dataclass
class _DayStats:
    """按天聚合的会话统计：只存计数与时间，不存消息原文。"""

    date: str
    user_messages: int = 0
    aliya_messages: int = 0
    proactive_sent: int = 0
    proactive_replied: int = 0
    first_at: datetime | None = None
    last_at: datetime | None = None
    hours: list[int] = field(default_factory=list)
    latency_sum: float = 0.0
    latency_count: int = 0
    proactive_hours: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "user_messages": self.user_messages,
            "aliya_messages": self.aliya_messages,
            "proactive_sent": self.proactive_sent,
            "proactive_replied": self.proactive_replied,
            "first_at": self.first_at.isoformat() if self.first_at else None,
            "last_at": self.last_at.isoformat() if self.last_at else None,
            "hours": list(self.hours),
            "latency_sum": self.latency_sum,
            "latency_count": self.latency_count,
            "proactive_hours": dict(self.proactive_hours),
        }

    @classmethod
    def from_dict(cls, date: str, data: dict[str, object]) -> _DayStats:
        raw_hours = data.get("hours")
        hours = (
            [_as_int(item) for item in cast("list[object]", raw_hours)]
            if isinstance(raw_hours, list)
            else []
        )
        raw_proactive = data.get("proactive_hours")
        proactive_hours = (
            {
                str(key): _as_int(value)
                for key, value in cast("dict[str, object]", raw_proactive).items()
            }
            if isinstance(raw_proactive, dict)
            else {}
        )
        return cls(
            date=date,
            user_messages=_as_int(data.get("user_messages")),
            aliya_messages=_as_int(data.get("aliya_messages")),
            proactive_sent=_as_int(data.get("proactive_sent")),
            proactive_replied=_as_int(data.get("proactive_replied")),
            first_at=_as_datetime(data.get("first_at")),
            last_at=_as_datetime(data.get("last_at")),
            hours=hours,
            latency_sum=_as_float(data.get("latency_sum")),
            latency_count=_as_int(data.get("latency_count")),
            proactive_hours=proactive_hours,
        )


class TerminalService(Service):
    """双向消息、路由、链路与主动消息节流。"""

    name: ClassVar[str] = "cosmos.terminal"
    dependencies: ClassVar[tuple[type[Service], ...]] = (
        ConversationService,
        PresenceService,
        AgentRuntimeService,
        BusService,
        ClockService,
    )

    def __init__(
        self,
        conversation: ConversationService,
        presence: PresenceService,
        runtime: AgentRuntimeService,
        bus: BusService,
        clock: ClockService,
        config: AgentSettings,
    ) -> None:
        super().__init__()
        self._conversation: ConversationService = conversation
        self._presence: PresenceService = presence
        self._runtime: AgentRuntimeService = runtime
        self._bus: BusService = bus
        self._clock: ClockService = clock
        self._config: AgentSettings = config
        self._proactive_config: AgentProactiveSettings = config.cosmos.terminal.proactive
        self._delivery_config: AgentDeliverySettings = config.cosmos.terminal.delivery
        self._state_dir: Path = Path(config.cosmos.contract.state_dir)
        self._stats_path: Path = self._state_dir / "terminal_stats.json"
        self._dedupe_path: Path = self._state_dir / "inbound_dedupe.json"
        self._dead_letter_path: Path = self._state_dir / "dead_letters.jsonl"
        self._dead_letters: deque[TerminalMessage] = deque(maxlen=config.cosmos.link.queue_limit)

        self._channel: UserChannelPort = MemoryUserChannel(limit=config.cosmos.link.queue_limit)
        self._link: LinkState = "up"
        self._link_since: datetime = clock.now()
        #: 最近一次链路心跳；None 表示还没有任何通道上报，不主动判 down
        self._link_heartbeat_at: datetime | None = None
        #: 区分巡检推断的 down（可由用户发言恢复）与显式 down（需显式恢复）
        self._link_inferred_down: bool = False
        self._link_observer: Callable[[LinkState], None] | None = None
        self._pending: deque[TerminalMessage] = deque(maxlen=config.cosmos.link.queue_limit)
        self._pending_inbound: deque[InboundEnvelope] = deque(maxlen=config.cosmos.link.queue_limit)
        #: 她所在舱室的通信条件：none 积压，weak 延迟，strong 正常
        self._comms: Literal["strong", "weak", "none"] = "strong"
        self._comms_delay_seconds: float = 20.0
        self._comms_replay_task: asyncio.Task[object] | None = None
        self._comms_outbound: deque[TerminalMessage] = deque()
        self._comms_outbound_worker: asyncio.Task[object] | None = None
        self._comms_outbound_event: asyncio.Event = asyncio.Event()
        self._history: dict[str, deque[TerminalMessage]] = {}
        #: (会话, 客户端幂等键) → 首次入站消息；重发时据此重建信封，不再入站
        self._inbound_dedupe: OrderedDict[tuple[str, str], TerminalMessage] = OrderedDict()
        self._last_session_id: str = "default"
        self._last_user_speech: datetime | None = None

        self._last_proactive_at: datetime | None = None
        self._last_proactive_day: str = ""
        self._awaiting_proactive_reply: bool = False
        self._last_aliya_at: datetime | None = None
        self._stats: dict[str, _DayStats] = {}
        self._proactive_count: int = 0
        self._proactive_date: str = ""
        self._proactive_unanswered: int = 0
        self._pending_proactive: asyncio.TimerHandle | None = None
        self._pending_proactive_at: datetime | None = None

        self._tasks: set[asyncio.Task[object]] = set()
        self._timers: set[asyncio.TimerHandle] = set()
        self._active: bool = False

    # ---------- 生命周期 ----------

    def set_user_channel(self, channel: UserChannelPort) -> None:
        """注入真实用户通道；默认是内存队列。"""
        self._channel = channel

    def set_link_observer(self, observer: Callable[[LinkState], None]) -> None:
        """链路状态变化的旁路观察者（供 WorldBridge 解耦）。"""
        self._link_observer = observer

    @override
    async def start(self) -> None:
        if self._active:
            return
        self._active = True
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._load_proactive()
        self._load_stats()
        self._load_inbound_dedupe()
        _ = self._spawn(self._link_loop())

    @override
    async def stop(self) -> None:
        if not self._active:
            return
        self._active = False
        if self._pending_proactive is not None:
            self._pending_proactive.cancel()
            self._pending_proactive = None
        for timer in list(self._timers):
            timer.cancel()
        self._timers.clear()
        tasks = list(self._tasks)
        for task in tasks:
            _ = task.cancel()
        if tasks:
            _ = await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._persist_stats()

    # ---------- 链路 ----------

    def link_state(self) -> LinkState:
        return self._link

    def link_since_minutes(self) -> int:
        """当前链路状态已持续的分钟数，供世界推理判断断链多久。"""
        return max(0, int((self._clock.now() - self._link_since).total_seconds() / 60))

    def last_aliya_at(self) -> datetime | None:
        """Aliya 最近一次发出消息的时间。"""
        return self._last_aliya_at

    def queue_depth(self) -> tuple[int, int]:
        """(用户积压, Aliya 待发)。"""
        return len(self._pending_inbound), len(self._pending)

    # ---------- 通信条件 ----------

    def comms_state(self) -> str:
        return self._comms

    def set_comms(self, level: Literal["strong", "weak", "none"]) -> None:
        """Aliya 上报所在舱室通信条件：`none` 积压，`weak` 延迟，`strong` 重放。"""
        if level == self._comms:
            return
        previous = self._comms
        self._comms = level
        self.log.info("信号变化", 从=previous, 到=level)
        self._ensure_comms_worker()
        if level == "none":
            _ = self._spawn(
                self.deliver(
                    "信号中断，消息会在恢复后送达。",
                    session_id=self._last_session_id,
                    kind="system",
                    force=True,
                )
            )
        elif previous == "none" and level == "strong":
            _ = self._spawn(self._replay())

    def _ensure_comms_worker(self) -> None:
        """确保弱信号出站 worker 在跑；有积压时主动唤醒，不等下一次投递。"""
        if self._comms == "none" or self._comms_outbound_worker is not None:
            return
        self._comms_outbound_worker = self._spawn(self._comms_outbound_loop())
        if self._comms_outbound:
            self._comms_outbound_event.set()

    async def _comms_outbound_loop(self) -> None:
        """弱信号出站：按入队顺序延迟发送，保序不丢。"""
        try:
            while self._comms != "none":
                self._comms_outbound_event.clear()
                if not self._comms_outbound:
                    _ = await self._comms_outbound_event.wait()
                    continue
                message = self._comms_outbound.popleft()
                delay = self._comms_delay_seconds * (
                    0.5 + (hash(message.message_id) % 100) / 100
                )
                await asyncio.sleep(delay)
                # 用方法取值避免被 while 条件的类型窄化误判为「永远不成立」
                if self.comms_state() == "none" or self._link == "down":
                    # 信号或链路又断了：放回队头，等恢复后按原顺序再发
                    self._comms_outbound.appendleft(message)
                    break
                _ = await self._send(message)
        finally:
            self._comms_outbound_worker = None

    async def report_presence(self, signal: PresenceInput) -> PresenceSnapshot:
        """通道上报在线/输入/设备等状态；真实通道可直接调用。"""
        return await self._presence.apply(signal)

    async def note_link_heartbeat(self) -> None:
        """记录一次链路心跳；收到心跳说明链路至少是通的。"""
        self._link_heartbeat_at = self._clock.now()
        if self._link != "up":
            await self.set_link_state("up")

    async def _link_loop(self) -> None:
        """按心跳间隔推导 unstable/down，避免链路永远停在 up。"""
        heartbeat = max(1.0, self._config.cosmos.link.heartbeat_seconds)
        down_after = max(heartbeat, self._config.cosmos.link.down_after_seconds)
        interval = max(1.0, min(heartbeat, down_after) / 2)
        while True:
            await asyncio.sleep(interval)
            if self._link_heartbeat_at is None:
                continue
            elapsed = (self._clock.now() - self._link_heartbeat_at).total_seconds()
            if elapsed > down_after:
                target: LinkState = "down"
            elif elapsed > heartbeat:
                target = "unstable"
            else:
                target = "up"
            if target != self._link:
                await self.set_link_state(target, inferred=True)

    async def set_link_state(self, state: LinkState, *, inferred: bool = False) -> None:
        """更新链路；`down` 时停止投递，恢复时按序重放。

        巡检推断的 down 可由用户发言自动恢复；显式 down 保持排队语义直到显式恢复。
        """
        if state == "up":
            self._link_heartbeat_at = self._clock.now()
        self._link_inferred_down = inferred and state == "down"
        if state == self._link:
            return
        self._link = state
        self._link_since = self._clock.now()
        if self._link_observer is not None:
            self._link_observer(state)
        queued_user, queued_aliya = self.queue_depth()
        payload: dict[str, object] = {
            "state": state,
            "queued_user_messages": queued_user,
            "queued_aliya_messages": queued_aliya,
        }
        _ = await self._bus.events.publish("link.state.changed", payload)
        if state == "up":
            # 链路恢复后先让弱信号队列继续跑，再重放链路中断期积压
            self._ensure_comms_worker()
            if self._config.cosmos.link.replay_on_recover:
                await self._replay()

    async def _replay(self) -> None:
        """恢复后按序重放双向队列。"""
        inbound = list(self._pending_inbound)
        self._pending_inbound.clear()
        for envelope in inbound:
            await self._bus.messages.publish("aliya", envelope)
        pending, self._pending = list(self._pending), deque(maxlen=self._pending.maxlen)
        for message in pending:
            _ = await self._send(message)

    async def _replay_weak(self) -> None:
        """弱信号的一次性延迟重放；信号断掉时保留积压等恢复。"""
        try:
            await asyncio.sleep(self._comms_delay_seconds)
            if self._comms != "none":
                await self._replay()
        finally:
            self._comms_replay_task = None

    # ---------- 入口 ----------

    async def ingest(self, message: TerminalMessage) -> InboundEnvelope:
        """接收用户消息：刷新链路与状态 → 组装信封 → 发布给 Aliya。

        带 `client_msg_id` 时按「会话 + 幂等键」去重：客户端断线重发只会入站一次，
        重发请求沿用首次入站的消息标识，不重复记账、不重复发布。幂等键落盘，
        进程重启后仍能识别重发。
        """
        key = (message.session_id, message.client_msg_id) if message.client_msg_id else None
        if key is not None:
            cached = self._inbound_dedupe.get(key)
            if cached is not None:
                self._inbound_dedupe.move_to_end(key)
                self.log.info(
                    "命中客户端幂等键，跳过重复入站",
                    message_id=cached.message_id,
                    session_id=message.session_id,
                )
                return await self._assemble_envelope(cached)
        now = self._clock.now()
        normalized = message.model_copy(
            update={
                "message_id": message.message_id or f"msg_{uuid.uuid4().hex}",
                "at": message.at or now,
            }
        )
        self._last_session_id = normalized.session_id
        self._last_user_speech = now
        if self._link == "down" and self._link_inferred_down:
            # 用户还能发言说明链路是通的：先恢复并重放积压，本条随后按序发布。
            await self.set_link_state("up")
        else:
            # 显式 down 保留排队语义，不因入站消息自动恢复。
            self._link_heartbeat_at = now
        self._mark_proactive_reply(now)
        self._record_user_stats(now)
        self._cancel_pending_proactive()
        self._proactive_unanswered = 0

        await self._presence.touch(session_id=normalized.session_id)
        _ = await self._bus.events.publish(
            "user.active",
            {"session_id": normalized.session_id, "at": now.isoformat()},
        )
        await self._conversation.record_user(normalized)
        mood_messages = self._conversation.recent_user_texts(
            normalized.session_id,
            limit=self._config.cosmos.presence.mood.window_messages,
        )
        if mood_messages:
            _ = self._spawn(self._presence.update_mood(mood_messages))
        envelope = await self._assemble_envelope(normalized)

        if self._link == "down" or self._comms == "none":
            limit = self._pending_inbound.maxlen
            if limit is not None and len(self._pending_inbound) >= limit:
                self.log.warning("消息积压已满，丢弃最旧消息", 上限=limit)
            self._pending_inbound.append(envelope)
            self._remember_inbound(key, normalized)
            self.log.info(
                "消息进入待发队列",
                原因=self._comms if self._link != "down" else "link_down",
                message_id=normalized.message_id,
                session_id=normalized.session_id,
            )
            return envelope

        if self._comms == "weak":
            # 弱信号：先积压，延迟一段时间后统一按序重放
            self._pending_inbound.append(envelope)
            self._remember_inbound(key, normalized)
            if self._comms_replay_task is None:
                self._comms_replay_task = self._spawn(self._replay_weak())
            return envelope

        await self._bus.messages.publish("aliya", envelope)
        self._remember_inbound(key, normalized)
        self.log.info(
            "消息已接收",
            message_id=normalized.message_id,
            session_id=normalized.session_id,
        )
        return envelope

    async def _assemble_envelope(self, message: TerminalMessage) -> InboundEnvelope:
        """按当前状态组装信封；重复入站时也用它重建，不产生副作用。"""
        # 当前消息由 envelope.message 单独携带，历史里必须排除，否则模型会看到两遍。
        context = await self._conversation.build_context(
            message.session_id, exclude_message_id=message.message_id
        )
        snapshot = self._presence.snapshot()
        tasks = await self._runtime.list_tasks(limit=5)
        presence_pending = sum(
            1 for task in tasks if task.status in {"running", "queued", "stalled"}
        )
        self._presence.set_pending(presence_pending)
        return InboundEnvelope(
            message=message,
            conversation=context,
            presence=snapshot.readable if snapshot.readable.tone != "unknown" else None,
            tasks=tasks,
        )

    def _remember_inbound(self, key: tuple[str, str] | None, message: TerminalMessage) -> None:
        """登记幂等键并落盘；超出窗口淘汰最旧记录。"""
        if key is None:
            return
        self._inbound_dedupe[key] = message
        self._inbound_dedupe.move_to_end(key)
        while len(self._inbound_dedupe) > INBOUND_DEDUPE_LIMIT:
            _ = self._inbound_dedupe.popitem(last=False)
        self._persist_inbound_dedupe()

    def _load_inbound_dedupe(self) -> None:
        """读取上次进程留下的幂等键；损坏时按空窗口启动。"""
        try:
            payload = cast("object", json.loads(self._dedupe_path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            self.log.warning("幂等键记录读取失败", 路径=str(self._dedupe_path), 原因=str(exc))
            return
        if not isinstance(payload, dict):
            return
        entries = cast("dict[str, object]", payload).get("entries")
        if not isinstance(entries, list):
            return
        for entry in cast("list[object]", entries):
            try:
                message = TerminalMessage.model_validate(entry)
            except ValueError:
                continue
            if not message.client_msg_id:
                continue
            key = (message.session_id, message.client_msg_id)
            self._inbound_dedupe[key] = message
        while len(self._inbound_dedupe) > INBOUND_DEDUPE_LIMIT:
            _ = self._inbound_dedupe.popitem(last=False)

    def _persist_inbound_dedupe(self) -> None:
        payload = {
            "entries": [
                message.model_dump(mode="json") for message in self._inbound_dedupe.values()
            ]
        }
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._dedupe_path.with_suffix(".tmp")
            _ = tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._dedupe_path)
        except OSError as exc:
            self.log.warning("幂等键记录写盘失败", 原因=str(exc))

    # ---------- 出口 ----------

    async def deliver(
        self,
        text: str,
        *,
        session_id: str,
        kind: MessageKind = "chat",
        force: bool = False,
    ) -> None:
        """回应用户：分段 → 脱敏扫描 → 投递 → 写入上下文。

        `kind="system"` 只投递：不写对话轮次，也不计入 Aliya 消息统计。
        """
        if not text.strip():
            self.log.warning("空文本不投递", session_id=session_id, kind=kind)
            return
        now = self._clock.now()
        parts = await self._conversation.format(text, kind=kind)
        total = len(parts)
        recorded = False

        for index, part in enumerate(parts, start=1):
            redacted = await self._conversation.redact(part)
            safe_text = redacted.text
            message = TerminalMessage(
                message_id=f"msg_{uuid.uuid4().hex}",
                direction="aliya_to_user",
                kind=kind,
                text=safe_text,
                at=now,
                session_id=session_id,
                meta=MessageMeta(part_index=index, part_total=total),
            )
            if self._link == "down":
                limit = self._pending.maxlen
                if limit is not None and len(self._pending) >= limit:
                    self.log.warning("链路中断期积压已满，丢弃最旧消息", 上限=limit)
                self._pending.append(message)
            elif self._comms == "none" and not force:
                self._pending.append(message)
            elif self._comms == "weak" and not force:
                self._comms_outbound.append(message)
                self._comms_outbound_event.set()
            else:
                _ = await self._send(message)
            if not recorded:
                await self._conversation.record_aliya(message)
                self._record_aliya_stats(now, kind)
                recorded = True
            self._remember(message)

        if kind in {"chat", "result"}:
            self._presence.set_awaiting(None)
        self.log.info(
            "消息已发出",
            session_id=session_id,
            kind=kind,
            字数=len(text),
            分段数=total,
        )

    async def notify(self, text: str, *, dispatch_id: str | None = None) -> ProactiveReceipt:
        """进展通知：受免打扰与输入状态约束，被拦时进延迟队列。"""
        now = self._clock.now()
        quiet_end = self._quiet_end(now)
        if quiet_end is not None:
            self._schedule_delivery(text, quiet_end, dispatch_id=dispatch_id)
            return ProactiveReceipt(accepted=False, reason="quiet_hours", deliver_after=quiet_end)
        if self._presence.snapshot().fields.typing.is_typing:
            deliver_after = now + timedelta(seconds=self._proactive_config.input_delay_seconds)
            self._schedule_delivery(text, deliver_after, dispatch_id=dispatch_id)
            return ProactiveReceipt(
                accepted=False, reason="user_active", deliver_after=deliver_after
            )
        await self.deliver(text, session_id=self._last_session_id, kind="progress")
        return ProactiveReceipt(accepted=True, reason="ok")

    async def send_proactive(self, text: str, *, urgency: float = 0.5) -> ProactiveReceipt:
        """主动发起对话：最小间隔、每小时/每日上限、免打扰、未回应降频。

        `urgency` ≥ 0.8 时可以突破小时预算（仍受每日上限约束），用于极少数
        真正紧急的窗口；普通建议按预算排队/丢弃。
        """
        now = self._clock.now()
        if not self._proactive_config.enabled:
            return ProactiveReceipt(accepted=False)
        if self._link == "down" and self._config.cosmos.link.suppress_proactive_when_down:
            return ProactiveReceipt(accepted=False)

        self._roll_proactive_date(now)
        if self._proactive_count >= self._proactive_config.daily_limit:
            return ProactiveReceipt(accepted=False, reason="daily_limit")

        hourly_limit = self._proactive_config.hourly_limit
        hour_count = self._stats_day(now).proactive_hours.get(str(now.hour), 0)
        if hourly_limit and hour_count >= hourly_limit and urgency < 0.8:
            return ProactiveReceipt(accepted=False, reason="hourly_limit")

        if self._last_proactive_at is not None:
            interval = self._effective_interval()
            elapsed = (now - self._last_proactive_at).total_seconds()
            if elapsed < interval:
                return ProactiveReceipt(accepted=False, reason="too_frequent")

        quiet_end = self._quiet_end(now)
        if quiet_end is not None:
            self._schedule_delivery(text, quiet_end, proactive=True)
            return ProactiveReceipt(accepted=False, reason="quiet_hours", deliver_after=quiet_end)

        if self._presence.snapshot().fields.typing.is_typing:
            deliver_after = now + timedelta(seconds=self._proactive_config.input_delay_seconds)
            self._schedule_delivery(text, deliver_after, proactive=True)
            return ProactiveReceipt(
                accepted=False, reason="user_active", deliver_after=deliver_after
            )

        if (
            self._last_user_speech is not None
            and (now - self._last_user_speech).total_seconds()
            < self._proactive_config.cancel_window_seconds
        ):
            deliver_after = self._last_user_speech + timedelta(
                seconds=self._proactive_config.cancel_window_seconds
            )
            self._schedule_delivery(text, deliver_after, proactive=True)
            return ProactiveReceipt(accepted=True, reason="ok", deliver_after=deliver_after)

        await self._proactive_deliver(text)
        return ProactiveReceipt(accepted=True, reason="ok")

    async def history(self, session_id: str, limit: int = 20) -> list[TerminalMessage]:
        """对话历史（不含 progress / result）。"""
        messages = list(self._history.get(session_id, deque()))
        return messages[-limit:]

    def _remember(self, message: TerminalMessage) -> None:
        if message.kind not in CONVERSATION_KINDS:
            return
        bucket = self._history.setdefault(message.session_id, deque(maxlen=200))
        bucket.append(message)

    async def _send(self, message: TerminalMessage) -> bool:
        """出站投递：按配置重试，耗尽后写死信。"""
        attempts: int = max(0, self._delivery_config.retry) + 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                await self._channel.send(message)
                return True
            except Exception as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    break
                delay: float = self._delivery_config.retry_backoff_seconds * (2.0**attempt)
                if delay:
                    await asyncio.sleep(delay)
        self.log.error(
            "消息投递失败",
            message_id=message.message_id,
            session_id=message.session_id,
            kind=message.kind,
            尝试=attempts,
            错误类型=type(last_error).__name__ if last_error else "unknown",
        )
        self._dead_letter(message)
        return False

    def _dead_letter(self, message: TerminalMessage) -> None:
        if not self._delivery_config.dead_letter:
            return
        self._dead_letters.append(message)
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            with self._dead_letter_path.open("a", encoding="utf-8") as handle:
                _ = handle.write(message.model_dump_json() + "\n")
        except OSError as exc:
            self.log.warning("死信写盘失败", 原因=str(exc))

    def dead_letters(self) -> list[TerminalMessage]:
        """最近的重试耗尽消息，供诊断与测试。"""
        return list(self._dead_letters)

    # ---------- 主动消息内部 ----------

    def _effective_interval(self) -> float:
        base = float(self._proactive_config.min_interval_seconds)
        if not self._proactive_config.unanswered_backoff or self._proactive_unanswered < 2:
            return base
        multiplier = min(2, self._proactive_config.max_backoff_multiplier)
        return base * multiplier

    def _roll_proactive_date(self, now: datetime) -> None:
        today = now.date().isoformat()
        if today != self._proactive_date:
            self._proactive_date = today
            self._proactive_count = 0

    async def _proactive_deliver(self, text: str) -> None:
        now = self._clock.now()
        self._last_proactive_at = now
        self._roll_proactive_date(now)
        self._proactive_count += 1
        self._proactive_unanswered += 1
        self._record_proactive_sent(now)
        self._persist_proactive()
        await self.deliver(text, session_id=self._last_session_id, kind="chat")

    def _schedule_delivery(
        self,
        text: str,
        deliver_after: datetime,
        *,
        proactive: bool = False,
        dispatch_id: str | None = None,
    ) -> None:
        """延迟投递：用户发言时取消（仅主动消息）。"""
        del dispatch_id
        now = self._clock.now()
        delay = max(0.0, (deliver_after - now).total_seconds())
        loop = asyncio.get_running_loop()

        def _fire() -> None:
            self._timers.discard(handle)
            if proactive and self._pending_proactive is not None:
                self._pending_proactive = None
            _ = self._spawn(self._deliver_later(text, proactive))

        handle = loop.call_later(delay, _fire)
        self._timers.add(handle)
        if proactive:
            self._pending_proactive = handle
            self._pending_proactive_at = now

    async def _deliver_later(self, text: str, proactive: bool) -> None:
        if proactive:
            await self._proactive_deliver(text)
        else:
            await self.deliver(text, session_id=self._last_session_id, kind="progress")

    def _cancel_pending_proactive(self) -> None:
        """用户 5s 内发言：取消仍在窗口内的待发主动消息。"""
        if self._pending_proactive is None or self._pending_proactive_at is None:
            return
        now = self._clock.now()
        elapsed = (now - self._pending_proactive_at).total_seconds()
        if elapsed <= self._proactive_config.cancel_window_seconds:
            self._pending_proactive.cancel()
            self._timers.discard(self._pending_proactive)
        self._pending_proactive = None
        self._pending_proactive_at = None

    def _quiet_end(self, now: datetime) -> datetime | None:
        """命中免打扰时段时返回结束时间。"""
        if not self._proactive_config.quiet_hours:
            return None
        current = now.time()
        for window in self._proactive_config.quiet_hours:
            parsed = _parse_window(window)
            if parsed is None:
                continue
            start, end = parsed
            if _in_window(current, start, end):
                target = datetime.combine(now.date(), end, tzinfo=now.tzinfo)
                if target <= now:
                    target += timedelta(days=1)
                return target
        return None

    # ---------- 计数持久化 ----------

    def _proactive_path(self) -> Path:
        return self._state_dir / "proactive.json"

    def _load_proactive(self) -> None:
        path = self._proactive_path()
        try:
            payload = cast("object", json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            self.log.warning("主动消息计数读取失败", 路径=str(path), 原因=str(exc))
            return
        if not isinstance(payload, dict):
            return
        data = cast("dict[str, object]", payload)
        count = data.get("count")
        date = data.get("date")
        self._proactive_count = count if isinstance(count, int) else 0
        self._proactive_date = date if isinstance(date, str) else ""

    def _persist_proactive(self) -> None:
        payload = {"date": self._proactive_date, "count": self._proactive_count}
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._proactive_path().with_suffix(".tmp")
            _ = tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._proactive_path())
        except OSError as exc:
            self.log.warning("主动消息计数写盘失败", 原因=str(exc))

    # ---------- 历史画像 ----------

    def history_pattern(self, *, window_days: int | None = None) -> HistoryPattern:
        """给世界推理的历史画像：只由聚合计数算出，不含消息原文。"""
        now = self._clock.now()
        span = (
            window_days if window_days is not None else self._proactive_config.history_window_days
        )
        cutoff = (now - timedelta(days=max(1, span))).date().isoformat()
        days = [day for day in self._stats.values() if day.user_messages > 0 and day.date >= cutoff]
        hour_counter: Counter[int] = Counter()
        for day in days:
            hour_counter.update(day.hours)
        active_hours = [hour for hour, _ in hour_counter.most_common(6)]
        active_hours.sort()

        sessions = [
            (day.last_at - day.first_at).total_seconds() / 60
            for day in days
            if day.first_at is not None and day.last_at is not None and day.last_at >= day.first_at
        ]
        latencies = [day.latency_sum / day.latency_count for day in days if day.latency_count > 0]
        last_proactive_minutes = (
            max(0, int((now - self._last_proactive_at).total_seconds() / 60))
            if self._last_proactive_at is not None
            else None
        )
        return HistoryPattern(
            active_hours=active_hours,
            typical_session_minutes=int(median(sessions)) if sessions else None,
            response_latency_p50_seconds=int(median(latencies)) if latencies else None,
            proactive_sent=sum(day.proactive_sent for day in days),
            proactive_replied=sum(day.proactive_replied for day in days),
            last_proactive_minutes_ago=last_proactive_minutes,
            sample_days=len(days),
        )

    def _stats_day(self, now: datetime) -> _DayStats:
        key = now.date().isoformat()
        day = self._stats.get(key)
        if day is None:
            day = _DayStats(date=key)
            self._stats[key] = day
        return day

    def _record_user_stats(self, now: datetime) -> None:
        day = self._stats_day(now)
        day.user_messages += 1
        day.first_at = day.first_at or now
        day.last_at = now
        day.hours.append(now.hour)
        if self._last_aliya_at is not None and now >= self._last_aliya_at:
            day.latency_sum += (now - self._last_aliya_at).total_seconds()
            day.latency_count += 1
        self._trim_stats(now)
        self._persist_stats()

    def _record_aliya_stats(self, now: datetime, kind: MessageKind) -> None:
        if kind not in CONVERSATION_KINDS and kind != "result":
            return
        day = self._stats_day(now)
        day.aliya_messages += 1
        self._last_aliya_at = now
        self._persist_stats()

    def _record_proactive_sent(self, now: datetime) -> None:
        day = self._stats_day(now)
        day.proactive_sent += 1
        key = str(now.hour)
        day.proactive_hours[key] = day.proactive_hours.get(key, 0) + 1
        self._last_proactive_day = day.date
        self._awaiting_proactive_reply = True
        self._persist_stats()

    def _mark_proactive_reply(self, now: datetime) -> None:
        """用户回话：把上一条主动消息标记为「有回应」。"""
        if not self._awaiting_proactive_reply:
            return
        self._awaiting_proactive_reply = False
        if self._last_proactive_at is None:
            return
        window = self._proactive_config.reply_window_minutes * 60
        if (now - self._last_proactive_at).total_seconds() > window:
            return
        day = self._stats.get(self._last_proactive_day)
        if day is not None:
            day.proactive_replied += 1
            self._persist_stats()

    def _trim_stats(self, now: datetime) -> None:
        cutoff = (now - timedelta(days=self._proactive_config.history_window_days)).date()
        expired: list[str] = []
        for key in self._stats:
            try:
                day = datetime.fromisoformat(key).date()
            except ValueError:
                expired.append(key)
                continue
            if day < cutoff:
                expired.append(key)
        for key in expired:
            _ = self._stats.pop(key, None)

    def _load_stats(self) -> None:
        try:
            raw = cast("object", json.loads(self._stats_path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            self.log.warning("会话统计读取失败", 路径=str(self._stats_path), 原因=str(exc))
            return
        if not isinstance(raw, dict):
            return
        days = cast("dict[str, object]", raw).get("days")
        if not isinstance(days, dict):
            return
        for key, value in cast("dict[str, object]", days).items():
            if isinstance(value, dict):
                self._stats[key] = _DayStats.from_dict(key, cast("dict[str, object]", value))
        self._trim_stats(self._clock.now())

    def _persist_stats(self) -> None:
        payload = {"days": {key: day.to_dict() for key, day in self._stats.items()}}
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._stats_path.with_suffix(".tmp")
            _ = tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._stats_path)
        except OSError as exc:
            self.log.warning("会话统计写盘失败", 原因=str(exc))

    def _spawn(self, coro: Coroutine[object, object, object]) -> asyncio.Task[object]:
        task: asyncio.Task[object] = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task


def _as_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def _as_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _as_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_window(value: str) -> tuple[time, time] | None:
    """解析 `HH:MM-HH:MM`；非法值返回 None（启动期配置校验另做）。"""
    if "-" not in value:
        return None
    start_text, end_text = value.split("-", 1)
    try:
        start = time.fromisoformat(start_text.strip())
        end = time.fromisoformat(end_text.strip())
    except ValueError:
        return None
    return start, end


def _in_window(current: time, start: time, end: time) -> bool:
    if start <= end:
        return start <= current < end
    # 跨午夜
    return current >= start or current < end
