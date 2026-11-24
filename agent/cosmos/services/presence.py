"""用户状态线索：采集、派生、去抖与隐私。

六条硬约束（设计 04 章 1 节）：最小必要、可关闭、不猜、不编造情绪、永不回显、对等。
实现上的落点：

- 每类字段独立开关；关闭时 `availability.reason="disabled_by_config"`。
- `online` 只按时间差判定，`busy` 只认显式上报。
- 情绪必须有证据与置信度；数值不进日志、不落盘。
- `readable` 统一用「你」，按优先级表派生，超长截断而不是抛错。
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Coroutine, Sequence
from datetime import datetime, timedelta
from typing import ClassVar, Literal, cast, override

from agent.cosmos.contracts import (
    ActivityInfo,
    Availability,
    EnvInfo,
    MoodInfo,
    OnlineInfo,
    PresenceChanged,
    PresenceField,
    PresenceFields,
    PresenceInput,
    PresenceSnapshot,
    ReadablePresence,
    TaskContextInfo,
    TypingInfo,
)
from agent.cosmos.transport import BusService
from core.config import AgentPresenceSettings, AgentSettings
from core.decision.service import DecisionService
from core.llm.service import LLMService
from core.service import ClockService, Service

#: 六个字段的固定顺序：availability 与事件里的 changed 都按它输出
FIELDS: tuple[PresenceField, ...] = (
    "online",
    "activity",
    "typing",
    "mood",
    "task_context",
    "env",
)

#: 文案长度上限（04 章 3）
SUMMARY_MAX = 30
HINT_MAX = 30

#: 各字段的推送最小间隔（秒）；typing 用 settle 单独处理
PUSH_INTERVALS: dict[str, float] = {
    "online": 30.0,
    "activity": 5.0,
    "mood": 300.0,
    "task_context": 1.0,
    "env": 60.0,
    "device": 30.0,
}

#: 情绪打分的档位：decision 端点用
MOOD_CRITERIA: tuple[str, ...] = ("很低", "偏低", "一般", "偏高", "很高")
MOOD_INSTRUCTIONS = "判断这段话里说话人的情绪积极程度"


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit]


def _period(now: datetime) -> Literal["morning", "afternoon", "evening", "night"]:
    hour = now.hour
    if 6 <= hour <= 11:
        return "morning"
    if 12 <= hour <= 17:
        return "afternoon"
    if 18 <= hour <= 22:
        return "evening"
    return "night"


class PresenceService(Service):
    """状态采集与派生。"""

    name: ClassVar[str] = "cosmos.presence"
    dependencies: ClassVar[tuple[type[Service], ...]] = (
        ClockService,
        DecisionService,
        LLMService,
        BusService,
    )

    def __init__(
        self,
        clock: ClockService,
        decision: DecisionService,
        llm: LLMService,
        bus: BusService,
        config: AgentSettings,
    ) -> None:
        super().__init__()
        self._clock: ClockService = clock
        self._decision: DecisionService = decision
        self._llm: LLMService = llm
        self._bus: BusService = bus
        self._config: AgentSettings = config
        self._presence: AgentPresenceSettings = config.cosmos.presence

        self._revision: int = 0
        self._last_seen: datetime | None = None
        self._message_times: deque[datetime] = deque(maxlen=500)
        self._last_message_at: datetime | None = None
        self._online_override: Literal["online", "away", "busy", "offline"] | None = None
        self._online_since: datetime | None = None
        self._typing: bool = False
        self._typing_started_at: datetime | None = None
        self._typing_paused_at: datetime | None = None
        self._mood: MoodInfo = MoodInfo()
        self._awaiting_task_id: str | None = None
        self._awaiting_since: datetime | None = None
        self._pending_count: int = 0
        self._timezone: str | None = None
        self._device: str = "unknown"
        self._hidden: set[PresenceField] = set()
        self._history: deque[PresenceSnapshot] = deque(maxlen=64)

        self._pending_fields: set[str] = set()
        self._flush_handle: asyncio.TimerHandle | None = None
        self._last_push_at: dict[str, datetime] = {}
        self._background: set[asyncio.Task[object]] = set()
        self._watcher: asyncio.Task[None] | None = None
        self._last_online_state: str = "unknown"
        self._active: bool = False

    # ---------- 生命周期 ----------

    @override
    async def start(self) -> None:
        if self._active:
            return
        self._active = True
        if self._presence.enabled:
            self._watcher = asyncio.create_task(self._watch_online())

    @override
    async def stop(self) -> None:
        if not self._active:
            return
        self._active = False
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        if self._watcher is not None:
            _ = self._watcher.cancel()
            self._watcher = None
        tasks = list(self._background)
        for task in tasks:
            _ = task.cancel()
        if tasks:
            _ = await asyncio.gather(*tasks, return_exceptions=True)
        self._background.clear()

    # ---------- 采集 ----------

    async def touch(self, *, session_id: str | None = None) -> None:
        """用户有交互：刷新活跃度并清空输入状态。"""
        del session_id
        now = self._now()
        self._last_seen = now
        self._last_message_at = now
        self._message_times.append(now)
        self._typing = False
        self._typing_started_at = None
        self._typing_paused_at = None
        self._online_since = self._online_since or now
        self._mark_changed({"online", "activity", "typing"})

    async def apply(self, signal: PresenceInput) -> PresenceSnapshot:
        """应用一次通道上报。"""
        now = self._now()
        changed: set[str] = set()

        if signal.online is not None and self._set_online(signal.online, now):
            changed.add("online")
        if signal.busy is not None and self._set_online("busy" if signal.busy else "online", now):
            changed.add("online")
        if signal.is_typing is not None:
            changed |= self._set_typing(signal.is_typing, now)
        if signal.device is not None and signal.device != self._device:
            self._device = signal.device
            changed.add("env")
        if signal.timezone is not None and signal.timezone != self._timezone:
            self._timezone = signal.timezone
            changed.add("env")

        self._mark_changed(changed)
        return self.snapshot()

    def set_visibility(self, fields: Sequence[PresenceField]) -> None:
        """Aliya 要求不看某些字段：停止推送，快照仍保留原始值。"""
        if fields and not self._presence.panel.aliya_can_hide:
            return
        self._hidden = set(fields)

    def set_awaiting(self, task_id: str | None) -> None:
        """维护「用户是否在等结果」。"""
        if task_id == self._awaiting_task_id:
            return
        self._awaiting_task_id = task_id
        self._awaiting_since = self._now() if task_id is not None else None
        self._mark_changed({"task_context"})

    def set_pending(self, count: int) -> None:
        """维护未完成任务数；值没变就不推。"""
        normalized = max(0, count)
        if normalized == self._pending_count:
            return
        self._pending_count = normalized
        self._mark_changed({"task_context"})

    # ---------- 情绪 ----------

    def set_explicit_mood(self, valence: float, energy: float) -> None:
        """用户自己说了情绪：优先级最高。"""
        candidate = MoodInfo(
            valence=max(-1.0, min(1.0, valence)),
            energy=max(0.0, min(1.0, energy)),
            confidence=1.0,
            source="explicit",
            evidence_count=1,
            updated_at=self._now(),
        )
        changed = self._mood_meaningfully_changed(candidate)
        self._mood = candidate
        if changed:
            self._mark_changed({"mood"})

    async def update_mood(self, messages: Sequence[str]) -> None:
        """基于最近消息打分；失败只告警，不影响主链路，也不记日志正文。"""
        if not messages or self._presence.mood.provider == "off":
            return
        text = "\n".join(messages)
        try:
            if self._presence.mood.provider == "decision":
                answer = await self._decision.rate(
                    text, MOOD_INSTRUCTIONS, MOOD_CRITERIA, backend=self._presence.mood.backend
                )
                candidate = MoodInfo(
                    valence=max(-1.0, min(1.0, answer.score * 2 - 1)),
                    energy=max(0.0, min(1.0, answer.score)),
                    confidence=max(0.0, min(1.0, answer.confidence)),
                    source="decision",
                    evidence_count=len(messages),
                    updated_at=self._now(),
                )
            else:
                result = await self._llm.chat_structured(
                    [{"role": "user", "content": text}], MoodInfo
                )
                candidate = result.model_copy(
                    update={
                        "source": "llm",
                        "evidence_count": len(messages),
                        "updated_at": self._now(),
                    }
                )
        except Exception as exc:
            # 只记类型与消息，不记情绪数值与正文
            self.log.warning("情绪打分失败，保留旧值", 错误类型=type(exc).__name__)
            return

        if candidate.confidence < self._presence.mood.min_confidence:
            candidate = MoodInfo(source="none", updated_at=candidate.updated_at)
        changed = self._mood_meaningfully_changed(candidate)
        self._mood = candidate
        if changed:
            self._mark_changed({"mood"})

    # ---------- 快照 ----------

    def snapshot(self) -> PresenceSnapshot:
        """构造当前快照；总开关关闭时返回 unknown 快照，不抛错。"""
        now = self._now()
        if not self._presence.enabled:
            return self._unknown_snapshot(now, enabled=False)

        fields = PresenceFields(
            online=self._online(now),
            activity=self._activity(now),
            typing=self._typing_info(),
            mood=self._mood_info(now),
            task_context=self._task_context(),
            env=self._env(now),
        )
        # 关闭的字段只保留 availability 标记，值本身也不交给消费方，
        # 否则「关了情绪」仍能从快照里读到原始数值。
        switches = self._presence.fields
        if not switches.online:
            fields.online = OnlineInfo()
        if not switches.activity:
            fields.activity = ActivityInfo()
        if not switches.typing:
            fields.typing = TypingInfo()
        if not switches.mood:
            fields.mood = MoodInfo()
        if not switches.task_context:
            fields.task_context = TaskContextInfo()
        if not switches.env:
            fields.env = EnvInfo()
        availability = self._availability(fields)
        readable = self._readable(fields)
        snapshot = PresenceSnapshot(
            captured_at=now,
            ttl_seconds=self._presence.ttl_seconds,
            fields=fields,
            availability=availability,
            readable=readable,
            revision=self._revision,
        )
        self._history.append(snapshot)
        self._trim_history(now)
        return snapshot

    # ---------- 内部：字段 ----------

    def _now(self) -> datetime:
        return self._clock.now()

    def _set_online(
        self, value: Literal["online", "away", "busy", "offline"], now: datetime
    ) -> bool:
        # 「online」是活跃信号，不是永久覆盖：清掉显式状态，继续按时间差判定；
        # busy / away / offline 才是通道明确声明、需要压住推断的状态。
        target = None if value == "online" else value
        changed = target != self._online_override
        self._last_seen = now
        if changed:
            self._online_override = target
            self._online_since = now
            self._last_online_state = self._current_online(now)
        return changed

    def _set_typing(self, typing: bool, now: datetime) -> set[str]:
        if typing == self._typing:
            return set()
        self._typing = typing
        if typing:
            self._typing_started_at = now
            self._typing_paused_at = None
            self._last_seen = now
        else:
            self._typing_paused_at = now
        return {"typing"}

    def _current_online(self, now: datetime) -> str:
        if self._online_override is not None:
            return self._online_override
        if self._last_seen is None:
            return "unknown"
        silence = (now - self._last_seen).total_seconds()
        if silence < self._presence.idle_after_seconds:
            return "online"
        if silence < self._presence.offline_after_seconds:
            return "away"
        return "offline"

    def _online(self, now: datetime) -> OnlineInfo:
        state = self._current_online(now)
        if state != self._online_override and self._online_since is None:
            self._online_since = now
        return OnlineInfo(
            state=cast("Literal['online', 'away', 'busy', 'offline', 'unknown']", state),
            since=self._online_since,
            last_seen_at=self._last_seen,
        )

    def _activity(self, now: datetime) -> ActivityInfo:
        recent = sum(1 for moment in self._message_times if (now - moment).total_seconds() <= 900)
        today = sum(1 for moment in self._message_times if moment.date() == now.date())
        silence = (
            int((now - self._last_message_at).total_seconds())
            if self._last_message_at is not None
            else None
        )
        session = (
            int((now - self._message_times[0]).total_seconds()) if self._message_times else None
        )
        return ActivityInfo(
            last_message_at=self._last_message_at,
            messages_last_15m=recent,
            messages_today=today,
            session_duration_seconds=session,
            silence_seconds=silence,
        )

    def _typing_info(self) -> TypingInfo:
        paused = (
            int((self._now() - self._typing_paused_at).total_seconds())
            if self._typing_paused_at is not None and not self._typing
            else None
        )
        return TypingInfo(
            is_typing=self._typing,
            typing_started_at=self._typing_started_at,
            typing_paused_seconds=paused,
        )

    def _mood_meaningfully_changed(self, candidate: MoodInfo) -> bool:
        """情绪变化超过阈值才推送；首次有结论或回落为 unknown 时仍推。"""
        previous = self._mood
        if previous.source == "none" and candidate.source == "none":
            return False
        if previous.source == "none" or candidate.source == "none":
            return True
        delta = max(
            abs(candidate.valence - previous.valence),
            abs(candidate.energy - previous.energy),
        )
        return delta >= self._presence.mood.delta_threshold

    def _mood_info(self, now: datetime) -> MoodInfo:
        if self._presence.mood.provider == "off":
            return MoodInfo()
        mood = self._mood
        if mood.updated_at is not None:
            age = (now - mood.updated_at).total_seconds()
            if age > self._presence.mood.stale_after_seconds:
                return MoodInfo()
        if mood.confidence < self._presence.mood.min_confidence:
            return MoodInfo(source="none", updated_at=mood.updated_at)
        return mood

    def _task_context(self) -> TaskContextInfo:
        return TaskContextInfo(
            awaiting_result=self._awaiting_task_id is not None,
            awaiting_task_id=self._awaiting_task_id,
            awaiting_since=self._awaiting_since,
            has_pending_tasks=self._pending_count > 0,
            pending_count=self._pending_count,
        )

    def _env(self, now: datetime) -> EnvInfo:
        timezone = (
            self._timezone or str(now.tzinfo or "UTC")
            if self._presence.privacy.allow_timezone
            else "UTC"
        )
        return EnvInfo(
            timezone=timezone,
            local_time=now,
            period=_period(now),
            device=cast(
                "Literal['desktop', 'mobile', 'web', 'unknown']",
                self._device if self._presence.privacy.allow_device else "unknown",
            ),
        )

    def _availability(self, fields: PresenceFields) -> dict[str, Availability]:
        switches = self._presence.fields
        result: dict[str, Availability] = {}
        mapping: dict[PresenceField, bool] = {
            "online": switches.online,
            "activity": switches.activity,
            "typing": switches.typing,
            "mood": switches.mood,
            "task_context": switches.task_context,
            "env": switches.env,
        }
        for field in FIELDS:
            if not mapping[field]:
                result[field] = Availability(available=False, reason="disabled_by_config")
            elif field == "mood" and fields.mood.source == "none":
                result[field] = Availability(available=False, reason="no_evidence")
            else:
                result[field] = Availability(available=True)
        return result

    # ---------- 内部：可解读表述 ----------

    def _readable(self, fields: PresenceFields) -> ReadablePresence:
        if not self._presence.enabled:
            return self._unknown_readable()

        state = fields.online.state
        silence = fields.activity.silence_seconds
        minutes = max(1, int((silence or 0) / 60))
        if state == "online" and fields.typing.is_typing:
            return self._presence_text("你在线，正在输入", "engaged", "稍等，别插话", 0.9)
        if state == "online" and silence is not None and silence < 60:
            return self._presence_text("你在线，刚发过消息", "engaged", "可以正常聊", 0.9)
        if state == "online" and fields.task_context.awaiting_result:
            return self._presence_text("你在线，还在等结果", "waiting", "可以先说一声进度", 0.85)
        if state == "online" and silence is not None and silence < 300:
            summary = self._wording(
                self._presence.wording.online_idle_template,
                f"你在线，{minutes} 分钟没说话了",
                minutes=minutes,
            )
            return self._presence_text(summary, "calm", "可能在看上一轮的回复", 0.85)
        if state == "online":
            return self._presence_text(
                f"你还在线，{minutes} 分钟没动静", "calm", "可以搭句话，风险不大", 0.8
            )
        if state == "away":
            return self._presence_text(
                f"你暂时离开了，{minutes} 分钟没动静", "away", "这会儿发消息可能看不到", 0.8
            )
        if state == "offline":
            hours = max(1, int((silence or 0) / 3600))
            if fields.env.period == "night":
                summary = self._wording(
                    self._presence.wording.offline_night_template,
                    f"你离线 {hours} 小时了，现在是深夜",
                    hours=hours,
                )
                return self._presence_text(summary, "away", "别打扰", 0.8)
            return self._presence_text(
                f"你离线 {hours} 小时了", "away", "可以留言，别指望马上回", 0.8
            )
        return self._presence_text("不知道你现在什么情况", "unknown", "按最保守的来", 0.3)

    @staticmethod
    def _wording(template: str | None, default: str, **values: int) -> str:
        """配置了文案模板就用模板；格式占位符不匹配时退回默认句。"""
        if not template:
            return default
        try:
            return template.format(**values)
        except (KeyError, ValueError, IndexError):
            return template

    def _presence_text(
        self, summary: str, tone: str, hint: str, confidence: float
    ) -> ReadablePresence:
        return ReadablePresence(
            summary=_truncate(summary, SUMMARY_MAX),
            tone=cast("Literal['calm', 'engaged', 'waiting', 'away', 'unknown']", tone),
            hint=_truncate(hint, HINT_MAX),
            confidence=confidence,
        )

    def _unknown_readable(self) -> ReadablePresence:
        summary = self._presence.wording.unknown_summary
        return ReadablePresence(
            summary=_truncate(summary, SUMMARY_MAX),
            tone="unknown",
            hint="按最保守的来",
            confidence=0.0,
        )

    def _unknown_snapshot(self, now: datetime, *, enabled: bool) -> PresenceSnapshot:
        availability = {
            field: Availability(
                available=False,
                reason="disabled_by_config" if not enabled else "no_evidence",
            )
            for field in FIELDS
        }
        return PresenceSnapshot(
            captured_at=now,
            ttl_seconds=self._presence.ttl_seconds,
            fields=PresenceFields(),
            availability=availability,
            readable=self._unknown_readable(),
            revision=self._revision,
        )

    def _trim_history(self, now: datetime) -> None:
        cutoff = now - timedelta(hours=self._presence.privacy.retain_raw_hours)
        while self._history and self._history[0].captured_at < cutoff:
            _ = self._history.popleft()

    # ---------- 内部：推送 ----------

    def _mark_changed(self, fields: set[str]) -> None:
        if not fields:
            return
        self._pending_fields |= fields
        if not self._active:
            return
        loop = asyncio.get_running_loop()
        delay = self._next_delay()
        if self._flush_handle is not None:
            if "typing" in fields:
                self._flush_handle.cancel()
                self._flush_handle = None
            else:
                return
        self._flush_handle = loop.call_later(delay, self._flush)

    def _next_delay(self) -> float:
        """取待推字段里最早到期的等待时间。"""
        if "typing" in self._pending_fields:
            return self._presence.push.typing_settle_seconds
        now = self._now()
        delays: list[float] = []
        for field in self._pending_fields:
            interval = (
                self._presence.push.mood_min_interval_seconds
                if field == "mood"
                else PUSH_INTERVALS.get(field, self._presence.push.min_interval_seconds)
            )
            last = self._last_push_at.get(field)
            if last is None:
                return 0.0
            delays.append(interval - (now - last).total_seconds())
        if not delays:
            return 0.0
        # 取最早到期的那个字段，而不是等最晚的：否则 5 分钟的情绪窗口会拖住在线状态
        return min(delays)

    def _flush(self) -> None:
        self._flush_handle = None
        now = self._now()
        pending = self._pending_fields - self._hidden
        self._pending_fields = set()
        if not pending:
            return

        ready: set[str] = set()
        for field in pending:
            if field == "typing":
                # typing 的「结束」由 settle 窗口保证，窗口一到就必须推
                ready.add(field)
                continue
            interval = (
                self._presence.push.mood_min_interval_seconds
                if field == "mood"
                else PUSH_INTERVALS.get(field, self._presence.push.min_interval_seconds)
            )
            last = self._last_push_at.get(field)
            if last is None or (now - last).total_seconds() >= interval:
                ready.add(field)
            else:
                # 还没到该字段的最小间隔：留在待推集合里，等下一次窗口
                self._pending_fields.add(field)

        if not ready:
            self._schedule_flush()
            return

        for field in ready:
            self._last_push_at[field] = now
        self._revision += 1
        snapshot = self.snapshot()
        event = PresenceChanged(
            revision=self._revision,
            changed=sorted(ready),
            snapshot=snapshot,
            readable=snapshot.readable,
            at=now,
        )
        self._spawn(self._bus.events.publish("presence.changed", event))
        if self._pending_fields:
            self._schedule_flush()

    def _schedule_flush(self) -> None:
        """按最早到期的字段安排下一次补发。"""
        if not self._active or self._flush_handle is not None:
            return
        loop = asyncio.get_running_loop()
        delay = max(0.001, self._next_delay())
        self._flush_handle = loop.call_later(delay, self._flush)

    async def _watch_online(self) -> None:
        """定时检查时间差引起的在线状态变化（online → away → offline）。"""
        tick = max(1.0, float(self._presence.ttl_seconds))
        while True:
            await asyncio.sleep(tick)
            if not self._presence.enabled:
                continue
            state = self._current_online(self._now())
            if state != self._last_online_state:
                self._last_online_state = state
                self._mark_changed({"online"})

    def _spawn(self, coro: Coroutine[object, object, object]) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)


__all__ = ["FIELDS", "HINT_MAX", "SUMMARY_MAX", "PresenceService"]
