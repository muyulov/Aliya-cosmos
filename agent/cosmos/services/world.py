"""世界推理桥：喂数据、搬结论。

设计 07 章的硬约束：

- 世界推理不注册、不占桌面、不可被调度；它只收到聚合信号，拿不到消息原文。
- 触发有显著性门控与合并窗口：一次状态抖动会连带多个事件，不合并就会连环触发。
- 超时 / 抛错 / 返回非法结构都只影响本次推演，主链路不阻塞。
- `view()` 过期仍返回旧结果并标 `stale=True`——旧的参考比没有参考好。

优化后新增四层：

- 触发层：规则显著性打分 + 动态窗口（`world_trigger.py`）；
- 信念层：`BeliefState` 延续上一轮结论，不再每次从零推断；
- 校准层：置信度分解、证据接地校验与推演轨迹落盘。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections import deque
from collections.abc import Callable, Coroutine
from datetime import datetime
from pathlib import Path
from typing import ClassVar, cast, override

from agent.cosmos.contracts import (
    ActionChoice,
    ActionWindow,
    AgentSignals,
    AliyaContext,
    BeliefState,
    HistoryPattern,
    Hypothesis,
    LinkSignal,
    LinkState,
    OutcomeSignal,
    PresenceSignal,
    PresenceSnapshot,
    SelfState,
    ShipSignal,
    TaskSignal,
    TaskSignals,
    TimeSignal,
    WorldInference,
    WorldInferenceDigest,
    WorldInferenceEvent,
    WorldInput,
)
from agent.cosmos.services.presence import PresenceService
from agent.cosmos.services.registry import AgentRegistryService
from agent.cosmos.services.runtime import AgentRuntimeService
from agent.cosmos.services.world_policy import WorldActionSelector
from agent.cosmos.services.world_trigger import TriggerKind, TriggerScore, WorldTriggerPolicy
from agent.cosmos.transport import BusService, WorldReasoningPort
from agent.cosmos.world.reasoning import LLMWorldReasoningPort
from core.config import AgentSettings, AgentWorldSettings
from core.llm import LLMService
from core.service import ClockService, Service

#: 单条文本上限（07 章 4.1）
SNAPSHOT_MAX_CHARS = 120
STATEMENT_MAX_CHARS = 120

#: 连续失败几次后暂停自动触发
FAILURE_LIMIT = 3

#: 输入指纹缓存最多保留多少份
CACHE_SIZE = 32

#: 静默巡检周期：秒级精度足够，避免每个阈值都挂一个定时器
SILENCE_TICK_SECONDS = 60.0

#: 证据权重的默认分配；mood 缺失时自然拉低置信度
EVIDENCE_WEIGHTS: dict[str, float] = {
    "time": 0.15,
    "presence": 0.30,
    "tasks": 0.20,
    "history": 0.20,
    "mood": 0.15,
}


class WorldBridgeService(Service):
    """组装 `WorldInput`、调用端口、维护信念与缓存。"""

    name: ClassVar[str] = "cosmos.world"
    dependencies: ClassVar[tuple[type[Service], ...]] = (
        ClockService,
        PresenceService,
        AgentRuntimeService,
        AgentRegistryService,
        BusService,
        LLMService,
    )

    def __init__(
        self,
        clock: ClockService,
        presence: PresenceService,
        runtime: AgentRuntimeService,
        registry: AgentRegistryService,
        bus: BusService,
        llm: LLMService,
        config: AgentSettings,
    ) -> None:
        super().__init__()
        self._clock: ClockService = clock
        self._presence: PresenceService = presence
        self._runtime: AgentRuntimeService = runtime
        self._registry: AgentRegistryService = registry
        self._bus: BusService = bus
        self._llm: LLMService = llm
        self._config: AgentSettings = config
        self._world: AgentWorldSettings = config.cosmos.world
        self._policy: WorldTriggerPolicy = WorldTriggerPolicy(self._world)
        self._selector: WorldActionSelector = WorldActionSelector(
            horizon=self._world.policy.horizon,
            discount=self._world.policy.discount,
        )

        self._port: WorldReasoningPort | None = None
        self._inference: WorldInference | None = None
        self._degraded: bool = False
        self._belief: BeliefState | None = None
        self._revision: int = 0

        self._aliya_context: AliyaContext | None = None
        self._link_provider: Callable[[], LinkState] = lambda: "up"
        self._link_since_provider: Callable[[], int] | None = None
        self._queue_provider: Callable[[], tuple[int, int]] = lambda: (0, 0)
        self._last_aliya_provider: Callable[[], datetime | None] | None = None
        self._history_provider: Callable[[], HistoryPattern] | None = None
        self._last_online: str = "unknown"
        self._last_mood_valence: float | None = None

        self._last_run_perf: float | None = None
        self._last_salience: float | None = None
        self._last_silence_threshold: int = 0
        self._failures: int = 0
        self._backoff_until: float | None = None
        self._running: bool = False
        self._dirty: bool = False
        self._dirty_trigger: TriggerKind = "tick"
        self._dirty_features: dict[str, float] | None = None

        self._pending_handle: asyncio.TimerHandle | None = None
        self._pending_deadline: float | None = None
        self._pending_trigger: TriggerKind = "tick"
        self._pending_salience: float = 0.0
        self._pending_features: dict[str, float] | None = None
        self._recent_triggers: dict[str, float] = {}

        self._cache: dict[str, tuple[WorldInference, float]] = {}
        self._cache_order: deque[str] = deque()
        self._trace_path: Path = Path(config.cosmos.contract.state_dir) / "world_trace.jsonl"

        self._active: bool = False
        self._tasks: set[asyncio.Task[object]] = set()

    # ---------- 生命周期 ----------

    def set_port(self, port: WorldReasoningPort) -> None:
        """注入推理端口（测试或部署方替换实现）。"""
        self._port = port

    def _ensure_port(self) -> WorldReasoningPort:
        """未注入时使用内置 LLM 端口。"""
        if self._port is None:
            self._port = LLMWorldReasoningPort(self._llm, self._world)
        return self._port

    def set_link_provider(self, provider: Callable[[], LinkState]) -> None:
        """注入链路状态读取器，避免与 TerminalService 构造器循环依赖。"""
        self._link_provider = provider

    def set_link_since_provider(self, provider: Callable[[], int]) -> None:
        """注入「当前链路状态已持续多少分钟」读取器。"""
        self._link_since_provider = provider

    def set_queue_provider(self, provider: Callable[[], tuple[int, int]]) -> None:
        """注入积压条数读取器：(用户消息数, Aliya 待发消息数)。"""
        self._queue_provider = provider

    def set_last_aliya_provider(self, provider: Callable[[], datetime | None]) -> None:
        """注入 Aliya 最近发消息时间读取器。"""
        self._last_aliya_provider = provider

    def set_history_provider(self, provider: Callable[[], HistoryPattern]) -> None:
        """注入历史画像读取器；由装配层接 `TerminalService.history_pattern`。"""
        self._history_provider = provider

    def history_window_days(self) -> int:
        """世界推理侧希望的历史窗口，由装配层传给终端统计。"""
        return self._world.history.window_days

    @override
    async def start(self) -> None:
        """关闭时不建端口、不注册定时器；开启时装配 LLM 推理端口。"""
        if self._active or not self._world.enabled:
            return
        self._apply_calibration_report()
        port = self._ensure_port()
        if isinstance(port, LLMWorldReasoningPort):
            fields: dict[str, object] = {
                "模型": self._llm.chat_model,
                "地址": self._llm.chat_base_url,
            }
            if self._llm.available("chat"):
                self.log.info("世界推理使用 LLM", face=None, **fields)
            else:
                self.log.warning(
                    "世界推理使用 LLM，但 llm.chat 未配置 api_key；推演会失败并按降级策略处理",
                    face=None,
                    **fields,
                )
        self._active = True
        try:
            self._trace_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.log.warning("推演轨迹目录创建失败", 原因=str(exc))
        self._spawn(self._tick_loop())
        self._spawn(self._silence_loop())
        self._spawn(self._consume_events())

    @override
    async def stop(self) -> None:
        if not self._active:
            return
        self._active = False
        if self._pending_handle is not None:
            self._pending_handle.cancel()
            self._pending_handle = None
        tasks = list(self._tasks)
        for task in tasks:
            _ = task.cancel()
        if tasks:
            _ = await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    def _apply_calibration_report(self) -> None:
        """读取离线校准报告，回填温度与显著性阈值。

        报告不存在或字段非法时保持配置原值，不阻断启动。
        """
        raw_path = self._world.calibration.report_path
        if not raw_path:
            return
        path = Path(raw_path)
        if not path.exists():
            return
        try:
            raw: object = cast(object, json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            self.log.warning("世界校准报告读取失败", 路径=raw_path, 原因=str(exc))
            return
        if not isinstance(raw, dict):
            self.log.warning("世界校准报告结构非法", 路径=raw_path)
            return
        data = cast(dict[str, object], raw)
        temperature = data.get("temperature")
        min_salience = data.get("min_salience")
        # 只接受安全范围内的数值：异常报告不应把配置改成 0 温度或负阈值。
        applied: list[str] = []
        if (
            isinstance(temperature, (int, float))
            and not isinstance(temperature, bool)
            and 0.1 <= float(temperature) <= 10
        ):
            self._world.calibration.temperature = float(temperature)
            applied.append(f"temperature={temperature}")
        if (
            isinstance(min_salience, (int, float))
            and not isinstance(min_salience, bool)
            and 0 <= float(min_salience) <= 1
        ):
            self._world.triggers.min_salience = float(min_salience)
            applied.append(f"min_salience={min_salience}")
        if applied:
            self.log.info("世界推理校准已应用", 字段="、".join(applied))

    # ---------- 读写 ----------

    async def set_aliya_context(self, ctx: AliyaContext | None) -> None:
        """只存不解读；None 表示清除。"""
        self._aliya_context = ctx

    def view(self) -> WorldInference | None:
        """缓存视图；过期或上次失败时标 `stale=True`。"""
        if self._inference is None:
            return None
        age = (self._clock.now() - self._inference.generated_at).total_seconds()
        stale = self._degraded or age > self._inference.ttl_seconds
        if stale == self._inference.stale:
            return self._inference
        return self._inference.model_copy(update={"stale": stale})

    async def refresh(
        self,
        *,
        force: bool = False,
        trigger: TriggerKind | None = None,
        salience: float | None = None,
        features: dict[str, float] | None = None,
        reuse: bool = False,
    ) -> WorldInference | None:
        """触发一次推演；未启用或处于合并窗口时返回缓存。

        `force` 只绕过节流与退避；`reuse` 才表示允许命中指纹缓存。
        事件触发应传 `reuse=True`，显式刷新可保持 `False` 以拿到新结果。
        """
        if not self._world.enabled:
            return None
        if not force and (self._in_window(salience) or self._in_backoff()):
            return self.view()
        if self._running:
            self._dirty = True
            if trigger is not None:
                self._dirty_trigger = trigger
            if features is not None:
                self._dirty_features = features
            return self.view()

        self._running = True
        resolved: TriggerKind = trigger or ("manual" if force else "tick")
        # 非强制刷新默认允许复用；强制刷新默认拿新结果，只有调用方显式
        # 传 reuse=True（事件链路 / refresh_world）时才复用。
        allow_reuse = reuse or not force
        try:
            input_payload = self._assemble()
            fingerprint = _fingerprint(input_payload)

            if allow_reuse:
                cached = self._cache_get(fingerprint)
                if cached is not None:
                    self._inference = cached
                    # 命中缓存说明输入没变、结论仍可用；上次调用的失败不再影响新鲜度。
                    # 失败计数与退避不清：缓存命中不代表端点已经恢复。
                    self._degraded = False
                    return self.view()

            port = self._ensure_port()
            try:
                async with asyncio.timeout(self._world.infer_timeout_seconds):
                    raw = await port.infer(input_payload)
            except Exception as exc:
                self._on_failure(exc)
                return self.view()

            inference = self._validate(raw)
            if inference is None:
                self._on_failure(ValueError("返回结构非法"))
                return self.view()

            enriched = self._enrich(inference, input_payload)
            enriched = enriched.model_copy(
                update={"revision": self._next_revision(), "stale": False}
            )
            await self._commit(
                enriched,
                resolved,
                fingerprint,
                salience=salience,
                features=features,
            )
            self._degraded = False
            self._failures = 0
            self._backoff_until = None
            return enriched
        finally:
            self._running = False
            self._last_run_perf = asyncio.get_running_loop().time()
            if salience is not None:
                self._last_salience = salience
            if self._dirty:
                self._dirty = False
                dirty_features = self._dirty_features
                self._dirty_features = None
                self._spawn(
                    self.refresh(
                        force=True,
                        trigger=self._dirty_trigger,
                        salience=self._last_salience,
                        features=dirty_features,
                        reuse=True,
                    )
                )

    # ---------- 内部：触发门控 ----------

    async def on_event(self, kind: TriggerKind, payload: dict[str, object] | None = None) -> None:
        """事件入口：打分、去重、窗口合并，再决定是否推演。"""
        if not self._active:
            return
        score = self._policy.score(kind, payload)
        if score is None:
            return
        if self._is_duplicate(kind, score, payload):
            return
        features = self._policy.features(kind, payload)
        if self._running:
            self._dirty = True
            self._dirty_trigger = kind
            self._dirty_features = features
            return
        if self._in_backoff():
            return

        remaining = self._window_remaining(score.salience)
        if remaining > 0:
            self._schedule_pending(kind, score.salience, remaining, features)
            return
        _ = await self.refresh(
            force=True,
            trigger=kind,
            salience=score.salience,
            features=features,
            reuse=True,
        )

    def _is_duplicate(
        self,
        kind: TriggerKind,
        score: TriggerScore,
        payload: dict[str, object] | None,
    ) -> bool:
        now = asyncio.get_running_loop().time()
        key = self._policy.dedupe_key(kind, payload)
        last = self._recent_triggers.get(key)
        # 去重冷却取合并窗口的一半，最少 5 秒：既吸收同一次状态抖动的连锁事件，
        # 又不会把真正的连续翻转（在线→离线→在线）吞掉。
        cooldown = max(5.0, self._policy.window_seconds(score.salience) / 2)
        if last is not None and now - last < cooldown:
            return True
        self._recent_triggers[key] = now
        if len(self._recent_triggers) > 128:
            cutoff = now - max(300.0, cooldown)
            self._recent_triggers = {
                item: stamp for item, stamp in self._recent_triggers.items() if stamp >= cutoff
            }
        return False

    def _window_remaining(self, salience: float) -> float:
        if self._last_run_perf is None:
            return 0.0
        window = self._policy.window_seconds(salience)
        return max(0.0, window - (asyncio.get_running_loop().time() - self._last_run_perf))

    def _schedule_pending(
        self,
        kind: TriggerKind,
        salience: float,
        delay: float,
        features: dict[str, float] | None,
    ) -> None:
        """合并窗口内的事件：只保留最早的一次补发。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + delay
        if (
            self._pending_handle is not None
            and self._pending_deadline is not None
            and self._pending_deadline <= deadline
        ):
            return
        if self._pending_handle is not None:
            self._pending_handle.cancel()
        self._pending_trigger = kind
        self._pending_salience = salience
        self._pending_features = features
        self._pending_deadline = deadline
        self._pending_handle = loop.call_later(delay, self._flush_pending)

    def _flush_pending(self) -> None:
        self._pending_handle = None
        self._pending_deadline = None
        kind = self._pending_trigger
        salience = self._pending_salience
        features = self._pending_features
        self._pending_features = None
        self._spawn(self.refresh(force=True, trigger=kind, salience=salience, features=features))

    def _in_window(self, salience: float | None = None) -> bool:
        if self._last_run_perf is None:
            return False
        window = self._policy.window_seconds(
            self._world.triggers.min_salience if salience is None else salience
        )
        elapsed = asyncio.get_running_loop().time() - self._last_run_perf
        return elapsed < window

    def _in_backoff(self) -> bool:
        if self._backoff_until is None:
            return False
        return asyncio.get_running_loop().time() < self._backoff_until

    def _on_failure(self, exc: BaseException) -> None:
        self._failures += 1
        self._degraded = True
        self.log.warning(
            "世界推理失败，保留旧结果",
            错误类型=type(exc).__name__,
            连续失败=self._failures,
        )
        if self._failures >= FAILURE_LIMIT:
            self._backoff_until = (
                asyncio.get_running_loop().time() + self._world.failure_backoff_seconds
            )
            self._failures = 0

    # ---------- 内部：缓存与提交 ----------

    def _next_revision(self) -> int:
        return max(self._revision, self._inference.revision if self._inference else 0) + 1

    def _cache_get(self, fingerprint: str) -> WorldInference | None:
        cached = self._cache.get(fingerprint)
        if cached is None:
            return None
        inference, created = cached
        # 缓存与结果 TTL 保持一致：过期缓存不应被当成新鲜结论返回。
        if asyncio.get_running_loop().time() - created > self._world.ttl_seconds:
            _ = self._cache.pop(fingerprint, None)
            return None
        return inference

    def _cache_put(self, fingerprint: str, inference: WorldInference) -> None:
        self._cache[fingerprint] = (
            inference,
            asyncio.get_running_loop().time(),
        )
        if fingerprint in self._cache_order:
            self._cache_order.remove(fingerprint)
        self._cache_order.append(fingerprint)
        while len(self._cache_order) > CACHE_SIZE:
            stale = self._cache_order.popleft()
            _ = self._cache.pop(stale, None)

    async def _commit(
        self,
        inference: WorldInference,
        trigger: TriggerKind,
        fingerprint: str,
        *,
        salience: float | None,
        features: dict[str, float] | None,
    ) -> None:
        self._inference = inference
        self._revision = max(self._revision, inference.revision)
        self._cache_put(fingerprint, inference)
        event = WorldInferenceEvent(
            inference=inference,
            stale=False,
            age_seconds=0.0,
            trigger=trigger,
        )
        _ = await self._bus.events.publish("world.inference", event)
        await self._trace(inference, trigger, fingerprint, salience or 0.0, features or {})

    # ---------- 内部：校验、校准与信念 ----------

    def _validate(self, raw: object) -> WorldInference | None:
        """校验结构、截断文本，并要求假设能落到 signals_used 上。"""
        if not isinstance(raw, WorldInference):
            self.log.warning("世界推理返回非 WorldInference，已丢弃")
            return None
        snapshot = raw.snapshot
        if len(snapshot) > SNAPSHOT_MAX_CHARS:
            snapshot = snapshot[:SNAPSHOT_MAX_CHARS]
            self.log.warning("世界推理摘要超长，已截断", 字段="snapshot")

        signals = set(raw.signals_used)
        kept: list[Hypothesis] = []
        for hypothesis in raw.hypotheses:
            statement = hypothesis.statement
            if len(statement) > STATEMENT_MAX_CHARS:
                statement = statement[:STATEMENT_MAX_CHARS]
                self.log.warning("世界推理假设超长，已截断", 字段="hypotheses.statement")
            valid = [signal for signal in hypothesis.basis_signals if signal in signals]
            if hypothesis.basis_signals and not valid:
                self.log.warning("假设缺少有效证据，已丢弃", 字段="hypotheses.basis_signals")
                continue
            if hypothesis.confidence < self._world.calibration.min_hypothesis_confidence:
                continue
            kept.append(
                hypothesis.model_copy(
                    update={
                        "statement": statement,
                        "basis_signals": valid
                        if hypothesis.basis_signals
                        else hypothesis.basis_signals,
                    }
                )
            )
        if not kept:
            self.log.warning("世界推理返回结构非法：没有可用假设，已丢弃")
            return None

        windows = list(raw.action_windows)
        if len(windows) > 3:
            windows = windows[:3]
            self.log.warning("世界推理行动窗口过多，已截断", 上限=3)
        return raw.model_copy(
            update={
                "snapshot": snapshot,
                "hypotheses": kept[: self._world.max_hypotheses],
                "action_windows": windows,
            }
        )

    def _enrich(self, inference: WorldInference, payload: WorldInput) -> WorldInference:
        """合成校准后的置信度、紧迫度、可打扰度与信念更新。"""
        breakdown = self._confidence_breakdown(inference, payload)
        model = breakdown["model"]
        evidence = sum(EVIDENCE_WEIGHTS[key] * breakdown[key] for key in EVIDENCE_WEIGHTS) / sum(
            EVIDENCE_WEIGHTS.values()
        )
        calibration = self._world.calibration
        raw = calibration.model_weight * model + calibration.evidence_weight * evidence
        confidence = _temperature_scale(raw, calibration.temperature)

        urgency = self._urgency(payload)
        interruptibility = self._interruptibility(payload)
        expected = confidence * urgency * interruptibility

        belief, delta, error = self._update_belief(inference, payload, confidence)
        windows = [
            window.model_copy(
                update={
                    "expected_value": (
                        window.expected_value if window.expected_value is not None else expected
                    )
                }
            )
            for window in inference.action_windows
        ]
        choice = self._selector.select(
            inference.model_copy(update={"action_windows": windows}),
            belief,
            payload.presence,
            confidence,
            urgency,
            interruptibility,
        )
        # 把窗口上的目的地带到选择结果，Aliya 侧才不用再猜窗口文本
        choice = choice.model_copy(
            update={"target": _target_for_window(inference.action_windows, choice)}
        )
        return inference.model_copy(
            update={
                "confidence": confidence,
                "confidence_breakdown": {
                    **breakdown,
                    "evidence": evidence,
                    "raw": raw,
                },
                "belief": belief,
                "belief_delta": delta,
                "prediction_error": error,
                "urgency": urgency,
                "interruptibility": interruptibility,
                "expected_value": expected,
                "action_windows": windows,
                "action_choice": choice,
            }
        )

    def _confidence_breakdown(
        self, inference: WorldInference, payload: WorldInput
    ) -> dict[str, float]:
        days = payload.history.sample_days if payload.history is not None else 0
        presence = payload.presence
        online_known = presence is not None and presence.online_state != "unknown"
        tasks_known = bool(payload.tasks.running or payload.tasks.recent_outcomes)
        mood = presence.mood_confidence if presence and presence.mood_confidence else 0.0
        return {
            "model": max(0.0, min(1.0, inference.confidence)),
            "time": 0.9 if payload.time.period != "unknown" else 0.3,
            "presence": 0.9 if online_known else 0.25,
            "tasks": 0.8 if tasks_known else 0.4,
            "history": min(1.0, days / 7) if days else 0.0,
            "mood": max(0.0, min(1.0, mood)),
        }

    @staticmethod
    def _urgency(payload: WorldInput) -> float:
        scores: list[float] = [0.25]
        presence = payload.presence
        if presence is not None:
            if presence.awaiting_result:
                scores.append(0.8)
            if presence.silence_minutes is not None:
                if presence.silence_minutes >= 360:
                    scores.append(0.8)
                elif presence.silence_minutes >= 120:
                    scores.append(0.6)
                elif presence.silence_minutes >= 30:
                    scores.append(0.45)
            if presence.mood_valence is not None and abs(presence.mood_valence) >= 0.6:
                scores.append(0.5)
        if payload.agents.abnormal:
            scores.append(0.7)
        if payload.tasks.running:
            scores.append(0.5)
        return max(scores)

    @staticmethod
    def _interruptibility(payload: WorldInput) -> float:
        presence = payload.presence
        if presence is None:
            return 0.3
        if presence.is_typing:
            return 0.2
        return {
            "online": 0.9,
            "busy": 0.4,
            "away": 0.5,
            "offline": 0.1,
            "unknown": 0.3,
        }.get(presence.online_state, 0.3)

    def _update_belief(
        self,
        inference: WorldInference,
        payload: WorldInput,
        confidence: float,
    ) -> tuple[BeliefState | None, dict[str, float], float | None]:
        if not self._world.belief.enabled:
            return None, {}, None
        old = payload.belief or self._belief or BeliefState()
        presence = payload.presence
        state = presence.online_state if presence is not None else "unknown"
        observed_online = {
            "online": 1.0,
            "busy": 0.6,
            "away": 0.5,
            "offline": 0.0,
            "unknown": 0.5,
        }.get(state, 0.5)
        messages = presence.messages_last_15m if presence is not None else 0
        typing_bonus = 0.15 if presence is not None and presence.is_typing else 0.0
        engagement_obs = min(1.0, 0.2 + messages * 0.12 + typing_bonus)
        readiness_obs = confidence if inference.action_windows else max(0.2, confidence * 0.6)
        if inference.hypotheses:
            uncertainty_obs = 1.0 - sum(
                hypothesis.confidence for hypothesis in inference.hypotheses
            ) / len(inference.hypotheses)
        else:
            uncertainty_obs = 0.6

        weight = self._world.belief.observation_weight
        belief = BeliefState(
            online_prob=_blend(old.online_prob, observed_online, weight),
            engagement=_blend(old.engagement, engagement_obs, weight),
            readiness=_blend(old.readiness, readiness_obs, weight),
            uncertainty=_blend(old.uncertainty, uncertainty_obs, weight),
            evidence_count=old.evidence_count + len(inference.signals_used),
            updated_at=payload.generated_at,
        )
        delta = {
            "online_prob": belief.online_prob - old.online_prob,
            "engagement": belief.engagement - old.engagement,
            "readiness": belief.readiness - old.readiness,
            "uncertainty": belief.uncertainty - old.uncertainty,
        }
        error = abs(old.online_prob - observed_online) if old.evidence_count > 0 else None
        self._belief = belief
        return belief, delta, error

    # ---------- 内部：组装输入 ----------

    def _assemble(self) -> WorldInput:
        now = self._clock.now()
        snapshot = self._presence.snapshot()
        presence_signal = self._presence_signal(snapshot) if self._presence_enabled() else None
        return WorldInput(
            generated_at=now,
            time=self._time_signal(now, snapshot),
            link=self._link_signal(),
            presence=presence_signal,
            tasks=self._task_signals(now),
            agents=self._agent_signals(),
            history=self._history(),
            aliya_context=self._aliya_context,
            self_state=(
                SelfState(
                    location=self._aliya_context.location,
                    location_name=None,
                    activity=self._aliya_context.activity,
                    since_minutes=None,
                    interruptibility=None,
                    energy=None,
                    comms=self._aliya_context.comms,
                )
                if self._aliya_context is not None
                and self._aliya_context.location is not None
                else None
            ),
            ship=(
                ShipSignal(comms=self._aliya_context.comms or "strong")
                if self._aliya_context is not None and self._aliya_context.comms is not None
                else None
            ),
            belief=self._belief,
            last_inference=self._digest(),
            anchors=self._anchors(),
        )

    def _presence_enabled(self) -> bool:
        return self._config.cosmos.presence.enabled

    def _time_signal(self, now: datetime, snapshot: PresenceSnapshot) -> TimeSignal:
        last_user = snapshot.fields.activity.last_message_at
        since_user = int((now - last_user).total_seconds()) if last_user else None
        last_aliya = self._last_aliya_provider() if self._last_aliya_provider is not None else None
        since_aliya = int((now - last_aliya).total_seconds()) if last_aliya else None
        return TimeSignal(
            now=now,
            timezone=str(now.tzinfo or "UTC"),
            period=snapshot.fields.env.period,
            since_last_user_message_seconds=since_user,
            since_last_aliya_message_seconds=since_aliya,
            session_duration_seconds=snapshot.fields.activity.session_duration_seconds,
        )

    def _link_signal(self) -> LinkSignal:
        state = self._link_provider()
        queued_user, queued_aliya = self._queue_provider()
        return LinkSignal(
            state=state,
            since_minutes=self._link_since_provider() if self._link_since_provider else None,
            queued_user_messages=queued_user,
            queued_aliya_messages=queued_aliya,
        )

    def _presence_signal(self, snapshot: PresenceSnapshot) -> PresenceSignal:
        fields = snapshot.fields
        unavailable = [
            name
            for name, availability in snapshot.availability.items()
            if not availability.available
        ]
        return PresenceSignal(
            online_state=fields.online.state,
            online_since_minutes=(
                max(0, int((self._clock.now() - fields.online.since).total_seconds() / 60))
                if fields.online.since is not None
                else None
            ),
            silence_minutes=(
                int(fields.activity.silence_seconds / 60)
                if fields.activity.silence_seconds is not None
                else None
            ),
            is_typing=fields.typing.is_typing,
            typing_paused_seconds=fields.typing.typing_paused_seconds,
            messages_last_15m=fields.activity.messages_last_15m,
            mood_valence=fields.mood.valence if fields.mood.source != "none" else None,
            mood_energy=fields.mood.energy if fields.mood.source != "none" else None,
            mood_confidence=fields.mood.confidence if fields.mood.source != "none" else None,
            awaiting_result=fields.task_context.awaiting_result,
            pending_count=fields.task_context.pending_count,
            device=fields.env.device,
            unavailable_fields=unavailable,
        )

    def _task_signals(self, now: datetime) -> TaskSignals:
        running: list[TaskSignal] = []
        queued = 0
        for card in self._registry.list():
            state = self._runtime.agent_state(card.id)
            queued += state.queued
            view = self._runtime.task_view(card.id)
            if state.status in {"running", "stalled"} and view is not None:
                running.append(
                    TaskSignal(
                        label=view.label,
                        agent_name=card.name,
                        elapsed_seconds=view.elapsed_seconds,
                        progress=view.progress,
                    )
                )

        outcomes: list[OutcomeSignal] = []
        for result, finished_at, elapsed in self._runtime.recent_results(limit=5):
            outcomes.append(
                OutcomeSignal(
                    status=result.status,
                    finished_minutes_ago=max(0, int((now - finished_at).total_seconds() / 60)),
                    elapsed_seconds=elapsed,
                )
            )
        return TaskSignals(running=running, queued=queued, recent_outcomes=outcomes)

    def _agent_signals(self) -> AgentSignals:
        total = idle = busy = abnormal = 0
        abnormal_names: list[str] = []
        for card in self._registry.list():
            total += 1
            state = self._runtime.agent_state(card.id)
            if state.status in {"running", "queued", "stalled"}:
                busy += 1
            elif state.status == "abnormal":
                abnormal += 1
                abnormal_names.append(card.name)
            else:
                idle += 1
        return AgentSignals(
            total=total,
            idle=idle,
            busy=busy,
            abnormal=abnormal,
            abnormal_agents=abnormal_names,
        )

    def _history(self) -> HistoryPattern:
        if self._history_provider is None:
            return HistoryPattern(sample_days=0)
        pattern = self._history_provider()
        if pattern.sample_days < self._world.history.min_sample_days:
            # 样本不足时派生结论不可信，只保留事实计数
            return pattern.model_copy(
                update={
                    "active_hours": [],
                    "typical_session_minutes": None,
                    "response_latency_p50_seconds": None,
                }
            )
        return pattern

    def _digest(self) -> WorldInferenceDigest | None:
        if self._inference is None:
            return None
        return WorldInferenceDigest(
            inference_id=self._inference.inference_id,
            revision=self._inference.revision,
            generated_at=self._inference.generated_at,
            snapshot=self._inference.snapshot,
            confidence=self._inference.confidence,
            signals_used=self._inference.signals_used,
        )

    def _anchors(self) -> list[str]:
        anchors: list[str] = []
        for card in self._registry.list():
            state = self._runtime.agent_state(card.id)
            if state.status in {"running", "stalled"}:
                view = self._runtime.task_view(card.id)
                if view is not None:
                    anchors.append(view.label)
        if self._aliya_context is not None:
            anchors.extend(self._aliya_context.open_topics)
        result: list[str] = []
        for anchor in anchors:
            if anchor and anchor not in result:
                result.append(anchor)
        return result[:6]

    # ---------- 内部：事件与循环 ----------

    async def _tick_loop(self) -> None:
        """定时兜底：绕过显著性门控，保证长时间无事件时仍有结论。"""
        while True:
            await asyncio.sleep(self._world.triggers.reconcile_seconds)
            _ = await self.refresh(force=True, trigger="tick", salience=0.2)

    async def _silence_loop(self) -> None:
        while True:
            await asyncio.sleep(SILENCE_TICK_SECONDS)
            await self._check_silence()

    async def _check_silence(self) -> None:
        """静默跨越阈值时补一次推演（30 / 120 / 360 分钟）。"""
        snapshot = self._presence.snapshot()
        seconds = snapshot.fields.activity.silence_seconds
        if seconds is None:
            return
        minutes = seconds // 60
        crossed = max(
            (
                value
                for value in self._world.triggers.silence_thresholds_minutes
                if minutes >= value
            ),
            default=0,
        )
        if crossed > self._last_silence_threshold:
            self._last_silence_threshold = crossed
            await self.on_event("silence", {"minutes": crossed, "entity": "silence"})

    async def _consume_events(self) -> None:
        topics = [
            "presence.changed",
            "dispatch.finished",
            "link.state.changed",
            "user.active",
        ]
        async for event in self._bus.events.subscribe(topics):
            if event.topic == "presence.changed":
                await self._on_presence_event(event.payload)
            elif event.topic == "dispatch.finished":
                if not self._world.triggers.dispatch_finished:
                    continue
                status = _payload_get(event.payload, "outcome", "status")
                await self.on_event(
                    "dispatch",
                    {"status": str(status or "ok"), "entity": "dispatch"},
                )
            elif event.topic == "link.state.changed":
                state = _payload_get(event.payload, "state")
                if isinstance(state, str):
                    await self.on_event("link", {"state": state, "entity": "link"})
            elif event.topic == "user.active":
                self._last_silence_threshold = 0
                if self._world.triggers.user_message:
                    session = _payload_get(event.payload, "session_id")
                    await self.on_event("user_message", {"entity": str(session or "")})

    async def _on_presence_event(self, payload: dict[str, object]) -> None:
        changed = payload.get("changed")
        changed_fields: set[str] = set()
        if isinstance(changed, list):
            for item in cast(list[object], changed):
                changed_fields.add(str(item))
        online = _payload_get(payload, "snapshot", "fields", "online", "state")
        if (
            self._world.triggers.presence_change
            and "online" in changed_fields
            and isinstance(online, str)
            and online != self._last_online
        ):
            self._last_online = online
            await self.on_event("presence", {"state": online, "entity": "online"})

        valence = _payload_get(payload, "snapshot", "fields", "mood", "valence")
        if "mood" in changed_fields and isinstance(valence, (int, float)):
            current = float(valence)
            previous = self._last_mood_valence
            self._last_mood_valence = current
            if previous is not None:
                await self.on_event("mood", {"delta": current - previous, "entity": "mood"})

    # ---------- 内部：轨迹 ----------

    async def _trace(
        self,
        inference: WorldInference,
        trigger: TriggerKind,
        fingerprint: str,
        salience: float,
        features: dict[str, float],
    ) -> None:
        if not self._world.trace_enabled:
            return
        belief: dict[str, object] | None = None
        if inference.belief is not None:
            source = inference.belief
            belief = {
                "online_prob": source.online_prob,
                "engagement": source.engagement,
                "readiness": source.readiness,
                "uncertainty": source.uncertainty,
                "evidence_count": source.evidence_count,
                "updated_at": (source.updated_at.isoformat() if source.updated_at else None),
            }
        hypotheses: list[dict[str, object]] = [
            {
                "statement": item.statement,
                "confidence": item.confidence,
                "basis": item.basis,
                "basis_signals": item.basis_signals,
            }
            for item in inference.hypotheses
        ]
        windows: list[dict[str, object]] = [
            {
                "window": item.window,
                "suggestion": item.suggestion,
                "rationale": item.rationale,
                "kind": item.kind,
                "expected_value": item.expected_value,
            }
            for item in inference.action_windows
        ]
        choice: dict[str, object] | None = None
        if inference.action_choice is not None:
            selected = inference.action_choice
            choice = {
                "action": selected.action,
                "window": selected.window,
                "expected_value": selected.expected_value,
                "pragmatic": selected.pragmatic,
                "epistemic": selected.epistemic,
                "cost": selected.cost,
                "efe": selected.efe,
                "lookahead": selected.lookahead,
                "total_efe": selected.total_efe,
                "rationale": selected.rationale,
            }
        payload: dict[str, object] = {
            "schema_version": 1,
            "at": self._clock.now().isoformat(),
            "inference_id": inference.inference_id,
            "revision": inference.revision,
            "trigger": trigger,
            "salience": salience,
            "trigger_features": features,
            "fingerprint": fingerprint,
            "confidence": inference.confidence,
            "confidence_breakdown": inference.confidence_breakdown,
            "urgency": inference.urgency,
            "interruptibility": inference.interruptibility,
            "expected_value": inference.expected_value,
            "prediction_error": inference.prediction_error,
            "belief": belief,
            "belief_delta": inference.belief_delta,
            "signals_used": inference.signals_used,
            "hypotheses": hypotheses,
            "action_windows": windows,
            "action_choice": choice,
        }
        try:
            await asyncio.to_thread(self._append_trace, payload)
        except OSError as exc:
            self.log.warning("推演轨迹写盘失败", 原因=str(exc))

    def _append_trace(self, payload: dict[str, object]) -> None:
        path = self._trace_path
        path.parent.mkdir(parents=True, exist_ok=True)
        max_bytes = self._world.trace_max_bytes
        if max_bytes and path.exists() and path.stat().st_size >= max_bytes:
            _ = path.replace(path.with_name(f"{path.name}.1"))
        with path.open("a", encoding="utf-8") as handle:
            _ = handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _spawn(self, coro: Coroutine[object, object, object]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def _target_for_window(windows: list[ActionWindow], choice: ActionChoice) -> str | None:
    """选出行动对应的目的地；窗口没标或选择没带窗口时返回 None。"""
    if choice.window is None:
        return None
    return next((item.target for item in windows if item.window == choice.window), None)


def _fingerprint(payload: WorldInput) -> str:
    """输入指纹：去掉时间戳，只保留「语义上是否变了」。"""
    data: dict[str, object] = cast(dict[str, object], cast(object, payload.model_dump(mode="json")))
    # 只保留「语义输入」：时间戳、上一轮结论、信念本身都会随每次推演变化，
    # 放进指纹会让缓存永远命中不了。
    for key in ("generated_at", "time", "belief", "last_inference"):
        _ = data.pop(key, None)
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _temperature_scale(probability: float, temperature: float) -> float:
    """温度缩放：把模型自报的过度置信往 0.5 拉。"""
    bounded = min(1.0 - 1e-6, max(1e-6, probability))
    logit = math.log(bounded / (1.0 - bounded))
    return 1.0 / (1.0 + math.exp(-logit / max(1e-6, temperature)))


def _blend(old: float, observed: float, weight: float) -> float:
    return max(0.0, min(1.0, weight * observed + (1.0 - weight) * old))


def _as_dict(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return cast(dict[str, object], value)


def _payload_get(payload: dict[str, object], *path: str) -> object:
    """按路径取事件载荷里的字段，缺失返回 None。"""
    current: object = payload
    for key in path:
        mapping = _as_dict(current)
        if mapping is None:
            return None
        current = mapping.get(key)
    return current


__all__ = [
    "FAILURE_LIMIT",
    "SNAPSHOT_MAX_CHARS",
    "WorldBridgeService",
    "WorldTriggerPolicy",
]
