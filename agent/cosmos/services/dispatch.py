"""调度服务：把「点击」与「自然语言」归一成同一套管线。

管线（设计 05 章 2 节）：Resolve → Plan → Confirm → Submit → Observe。

- 显式入口不语义匹配：Aliya 说用哪个就用哪个，只做存在性、禁用、参数顶层结构检查。
- 自然语言入口先跑 `registry.match()` 粗筛；单候选高分直接 `single`，
  多候选才付一次模型调用做精排与拆解；模型失败按检索结果降级。
- `suggest` 模式只产出计划，`auto` 且置信度达标才直接提交。
- 并行任务若写同一批文件，自动改成串行（`depends_on`），宁可慢也不并发改同一文件。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections.abc import Coroutine, Sequence
from dataclasses import dataclass, field
from typing import ClassVar, cast, override

from agent.cosmos.contracts import (
    AgentCard,
    AgentMatch,
    AgentResult,
    DispatchFinished,
    DispatchHandle,
    DispatchIntent,
    DispatchOutcome,
    DispatchPlan,
    PlannedTask,
    RejectedTask,
    TaskHandle,
)
from agent.cosmos.errors import AgentNotFoundError, DispatchError
from agent.cosmos.services.conversation import ConversationService
from agent.cosmos.services.presence import PresenceService
from agent.cosmos.services.registry import AgentRegistryService
from agent.cosmos.services.runtime import AgentRuntimeService
from agent.cosmos.transport import BusService
from core.config import AgentSettings
from core.llm.messages import user
from core.llm.service import LLMService
from core.service import ClockService, Service

#: `plan()` 结果缓存时长（09 章 10 节：同输入同结果）
PLAN_CACHE_SECONDS = 30.0
#: params 里可能出现的路径字段：用于并行冲突检测
PATH_KEYS: tuple[str, ...] = ("paths", "path", "files", "file")


@dataclass
class _Batch:
    """一个调度批次的内部记录。"""

    dispatch_id: str
    session_id: str
    task_ids: list[str] = field(default_factory=list)
    started_perf: float = 0.0
    outcome: DispatchOutcome | None = None


class DispatchService(Service):
    """匹配、拆解、提交与聚合。"""

    name: ClassVar[str] = "cosmos.dispatch"
    dependencies: ClassVar[tuple[type[Service], ...]] = (
        AgentRegistryService,
        AgentRuntimeService,
        ConversationService,
        PresenceService,
        LLMService,
        BusService,
        ClockService,
    )

    def __init__(
        self,
        registry: AgentRegistryService,
        runtime: AgentRuntimeService,
        conversation: ConversationService,
        presence: PresenceService,
        llm: LLMService,
        bus: BusService,
        clock: ClockService,
        config: AgentSettings,
    ) -> None:
        super().__init__()
        self._registry: AgentRegistryService = registry
        self._runtime: AgentRuntimeService = runtime
        self._conversation: ConversationService = conversation
        self._presence: PresenceService = presence
        self._llm: LLMService = llm
        self._bus: BusService = bus
        self._clock: ClockService = clock
        self._config: AgentSettings = config
        self._plan_cache: dict[str, tuple[DispatchPlan, float]] = {}
        self._dedupe: dict[str, tuple[str, float]] = {}
        self._batches: dict[str, _Batch] = {}
        self._background: set[asyncio.Task[object]] = set()

    # ---------- 计划 ----------

    async def plan(self, intent: DispatchIntent) -> DispatchPlan:
        """只生成计划，不执行。"""
        key = intent.model_dump_json()
        cached = self._plan_cache.get(key)
        if cached is not None and time.perf_counter() - cached[1] <= PLAN_CACHE_SECONDS:
            return cached[0]

        if intent.agent_id is not None:
            plan = self._plan_explicit(intent, intent.agent_id)
        else:
            plan = await self._plan_natural(intent)

        plan = self._serialize_shared_files(plan)
        self._plan_cache[key] = (plan, time.perf_counter())
        self.log.info(
            "调度计划生成",
            dispatch_decision=plan.decision,
            任务数=len(plan.tasks),
            置信度=plan.confidence,
            rationale=plan.rationale,
        )
        return plan

    def _plan_explicit(self, intent: DispatchIntent, agent_id: str) -> DispatchPlan:
        """显式入口：不匹配，只做存在性、禁用与参数顶层检查。"""
        card = self._registry.get(agent_id)
        if not self._registry.enabled(agent_id):
            raise AgentNotFoundError(f"agent 已停用：{agent_id}", reason="disabled")

        params = dict(intent.params or {})
        missing = self._missing_required(params, card)
        if missing:
            return DispatchPlan(
                decision="clarify",
                confidence=1.0,
                rationale="还差几个必填参数",
                missing=missing,
                session_id=intent.session_id,
            )
        return DispatchPlan(
            decision="single",
            tasks=[
                PlannedTask(
                    agent_id=agent_id,
                    params=params,
                    label=intent.hint or f"交给{card.name}处理",
                )
            ],
            confidence=1.0,
            rationale="目标由 Aliya 指定",
            session_id=intent.session_id,
        )

    async def _plan_natural(self, intent: DispatchIntent) -> DispatchPlan:
        """自然语言入口：检索粗筛 → 必要时模型精排与拆解。"""
        text = intent.user_text or ""
        candidates = self._registry.match(text, top_k=self._config.cosmos.dispatch.max_candidates)
        if not candidates:
            return DispatchPlan(
                decision="rejected",
                confidence=0.0,
                rationale="没有匹配的 agent",
                session_id=intent.session_id,
            )

        best = candidates[0]
        high = self._config.cosmos.dispatch.high_threshold
        if len(candidates) == 1 and best.score >= high:
            card = self._registry.get(best.agent_id)
            return DispatchPlan(
                decision="single",
                tasks=[
                    PlannedTask(
                        agent_id=best.agent_id,
                        params=dict(intent.params or {}),
                        label=intent.hint or f"交给{card.name}处理",
                    )
                ],
                confidence=best.score,
                rationale=f"交给{card.name}处理",
                session_id=intent.session_id,
            )

        try:
            plan = await self._ask_model(intent, candidates)
        except Exception as exc:
            self.log.warning(
                "匹配服务失败，回退检索结果",
                错误类型=type(exc).__name__,
            )
            return self._fallback_plan(intent, candidates, exc)

        if not plan.tasks:
            return self._fallback_plan(intent, candidates, DispatchError("模型没有给出任务"))
        allowed = {match.agent_id for match in candidates}
        if any(task.agent_id not in allowed for task in plan.tasks):
            return self._fallback_plan(
                intent, candidates, DispatchError("模型给出了候选之外的 agent")
            )

        plan = plan.model_copy(update={"session_id": intent.session_id})
        if plan.confidence < self._config.cosmos.dispatch.min_confidence:
            missing = plan.missing or ["需要确认用哪个工具"]
            return plan.model_copy(update={"decision": "clarify", "missing": missing, "tasks": []})
        return plan

    async def _ask_model(
        self, intent: DispatchIntent, candidates: Sequence[AgentMatch]
    ) -> DispatchPlan:
        """把候选卡片与文本交给模型精排/拆解，超时按配置处理。"""
        cards = [self._registry.get(match.agent_id) for match in candidates]
        prompt = self._build_prompt(intent, cards)
        timeout = self._config.cosmos.dispatch.plan_timeout_seconds
        async with asyncio.timeout(timeout):
            return await self._llm.chat_structured([user(prompt)], DispatchPlan)

    @staticmethod
    def _build_prompt(intent: DispatchIntent, cards: Sequence[AgentCard]) -> str:
        lines = [
            "根据用户请求，从候选工具里选择并拆解任务，输出 DispatchPlan。",
            "decision 取 single / parallel / clarify / rejected；",
            "tasks[].label 用一句话描述，不要出现 agent id；",
            "depends_on 用同批次任务的 0 基序号。",
            f"用户请求：{intent.user_text or ''}",
        ]
        if intent.hint:
            lines.append(f"额外提示：{intent.hint}")
        for card in cards:
            lines.append(
                f"- {card.id}｜{card.name}｜{card.summary}｜能力：{'、'.join(card.capabilities)}"
            )
        return "\n".join(lines)

    def _fallback_plan(
        self, intent: DispatchIntent, candidates: Sequence[AgentMatch], exc: Exception
    ) -> DispatchPlan:
        """模型失败：唯一高分候选 → single，否则 clarify（05 章 8 节）。"""
        if not self._config.cosmos.dispatch.match_fallback_on_error:
            raise DispatchError(f"匹配服务不可用：{exc}") from exc
        best = candidates[0]
        score = best.score
        agent_id = best.agent_id
        reason = "匹配服务超时" if isinstance(exc, TimeoutError) else "匹配服务失败"
        card = self._registry.get(agent_id)
        if score >= self._config.cosmos.dispatch.high_threshold:
            return DispatchPlan(
                decision="single",
                tasks=[
                    PlannedTask(
                        agent_id=agent_id,
                        params=dict(intent.params or {}),
                        label=intent.hint or f"交给{card.name}处理",
                    )
                ],
                confidence=float(score),
                rationale=f"{reason}，按关键词结果建议",
                session_id=intent.session_id,
            )
        return DispatchPlan(
            decision="clarify",
            confidence=float(score),
            rationale=f"{reason}，需要确认用哪个工具",
            missing=["需要确认用哪个工具"],
            session_id=intent.session_id,
        )

    def _missing_required(self, params: dict[str, object], card: AgentCard) -> list[str]:
        """只校验 object 与必填键；字段级校验留给子 agent。"""
        if card.params_schema.get("type") != "object":
            raise DispatchError(f"{card.id} 的参数 schema 不是 object")
        required = card.params_schema.get("required")
        if not isinstance(required, list):
            return []
        keys = cast("list[object]", required)
        return [str(key) for key in keys if str(key) not in params]

    def _serialize_shared_files(self, plan: DispatchPlan) -> DispatchPlan:
        """并行任务写同一批文件时改成串行。"""
        if len(plan.tasks) < 2:
            return plan
        tasks = list(plan.tasks)
        changed = False
        for index in range(len(tasks)):
            for previous in range(index):
                if _paths(tasks[index]).isdisjoint(_paths(tasks[previous])):
                    continue
                if previous in tasks[index].depends_on:
                    continue
                tasks[index] = tasks[index].model_copy(
                    update={"depends_on": [*tasks[index].depends_on, previous]}
                )
                changed = True
        if not changed:
            return plan
        return plan.model_copy(
            update={
                "tasks": tasks,
                "rationale": f"{plan.rationale}（有任务写同一批文件，已改为串行）",
            }
        )

    # ---------- 执行 ----------

    async def execute(self, plan: DispatchPlan) -> DispatchHandle:
        """执行计划：提交是异步的，结果由 `dispatch.finished` 事件送达。"""
        dispatch_id = f"dsp_{uuid.uuid4().hex}"
        accepted_at = self._clock.now()

        if plan.decision not in {"single", "parallel"} or not plan.tasks:
            return DispatchHandle(
                dispatch_id=dispatch_id,
                accepted_at=accepted_at,
                status="accepted",
            )

        deduped = self._dedupe_tasks(plan)
        if deduped is not None:
            self.log.info("命中幂等窗口，返回已有任务", dispatch_id=dispatch_id, task_id=deduped)
            return DispatchHandle(
                dispatch_id=dispatch_id,
                task_ids=[deduped],
                accepted_at=accepted_at,
            )

        handles: list[TaskHandle] = await self._runtime.submit_many(
            plan.tasks, session_id=plan.session_id, dispatch_id=dispatch_id
        )
        self._remember(plan, handles)
        for handle in handles:
            _ = await self._bus.events.publish("agent.submitted", handle)
        if handles:
            self._presence.set_awaiting(handles[0].task_id)

        rejected: list[RejectedTask] = []
        for handle in handles:
            brief = self._runtime.task_brief(handle.task_id)
            if brief is not None and brief.status == "abnormal":
                rejected.append(RejectedTask(agent_id=handle.agent_id, reason="任务提交失败"))

        batch = _Batch(
            dispatch_id=dispatch_id,
            session_id=plan.session_id,
            task_ids=[handle.task_id for handle in handles],
            started_perf=time.perf_counter(),
        )
        self._batches[dispatch_id] = batch
        self._spawn(self._observe(batch, handles))
        self.log.info(
            "任务已提交",
            dispatch_id=dispatch_id,
            任务数=len(handles),
            agent="、".join(handle.agent_id for handle in handles),
            拒绝数=len(rejected),
        )
        return DispatchHandle(
            dispatch_id=dispatch_id,
            task_ids=batch.task_ids,
            accepted_at=accepted_at,
            status="partial" if rejected else "accepted",
            rejected=rejected,
        )

    async def dispatch(self, intent: DispatchIntent) -> DispatchHandle:
        """`plan` + `execute`；只有 auto 模式且置信度达标才真的提交。"""
        plan = await self.plan(intent)
        mode = self._config.cosmos.dispatch.mode
        eligible = (
            mode == "auto"
            and plan.decision in {"single", "parallel"}
            and plan.confidence >= self._config.cosmos.dispatch.auto_threshold
            and not plan.missing
        )
        if not eligible:
            return DispatchHandle(
                dispatch_id=f"dsp_{uuid.uuid4().hex}",
                accepted_at=self._clock.now(),
            )
        return await self.execute(plan)

    async def cancel(self, dispatch_id: str) -> None:
        """取消该批次未完成的任务。"""
        batch = self._batches.get(dispatch_id)
        if batch is None:
            raise DispatchError(f"批次不存在：{dispatch_id}")
        for task_id in batch.task_ids:
            try:
                await self._runtime.cancel(task_id)
            except (AgentNotFoundError, TimeoutError):
                continue

    async def outcome(self, dispatch_id: str) -> DispatchOutcome:
        """读取批次结果；未完成时抛 `DispatchError`。"""
        batch = self._batches.get(dispatch_id)
        if batch is None:
            raise DispatchError(f"批次不存在：{dispatch_id}")
        if batch.outcome is None:
            raise DispatchError(f"批次尚未结束：{dispatch_id}")
        return batch.outcome

    # ---------- 内部：幂等与聚合 ----------

    @staticmethod
    def _dedupe_key(session_id: str, task: PlannedTask) -> str:
        payload = json.dumps(
            [session_id, task.agent_id, task.params],
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _dedupe_tasks(self, plan: DispatchPlan) -> str | None:
        """单任务计划命中窗口时返回已有 task_id。"""
        if len(plan.tasks) != 1:
            return None
        key = self._dedupe_key(plan.session_id, plan.tasks[0])
        cached = self._dedupe.get(key)
        if cached is None:
            return None
        task_id, created = cached
        window = self._config.cosmos.dispatch.dedupe_window_seconds
        if window <= 0 or time.perf_counter() - created > window:
            _ = self._dedupe.pop(key, None)
            return None
        return task_id

    def _remember(self, plan: DispatchPlan, handles: Sequence[TaskHandle]) -> None:
        for task, handle in zip(plan.tasks, handles, strict=False):
            key = self._dedupe_key(plan.session_id, task)
            self._dedupe[key] = (handle.task_id, time.perf_counter())

    async def _observe(self, batch: _Batch, handles: Sequence[TaskHandle]) -> None:
        """等全部任务结束、聚合结果、发 `dispatch.finished`。"""
        results: list[AgentResult] = []
        for handle in handles:
            try:
                results.append(await self._runtime.wait(handle.task_id))
            except AgentNotFoundError:
                results.append(
                    AgentResult.failed(code="internal", message="任务记录丢失", retriable=False)
                )

        elapsed = time.perf_counter() - batch.started_perf
        outcome = self._aggregate(batch.dispatch_id, results, elapsed)
        batch.outcome = outcome
        self._presence.set_awaiting(None)
        self._spawn_publish(outcome)
        self.log.info(
            "调度聚合完成",
            dispatch_id=batch.dispatch_id,
            状态=outcome.status,
            任务数=len(results),
            耗时毫秒=round(elapsed * 1000, 1),
        )

    @staticmethod
    def _aggregate(
        dispatch_id: str, results: Sequence[AgentResult], elapsed: float
    ) -> DispatchOutcome:
        ok = sum(1 for result in results if result.status == "ok")
        failed = sum(1 for result in results if result.status in {"failed"})
        cancelled = sum(1 for result in results if result.status == "cancelled")
        if ok == len(results):
            status = "ok"
        elif cancelled == len(results):
            status = "cancelled"
        elif ok == 0:
            status = "failed"
        else:
            status = "partial"
        return DispatchOutcome(
            dispatch_id=dispatch_id,
            status=status,
            results=list(results),
            elapsed_seconds=elapsed,
            summary=f"{ok} 个任务完成，{failed} 个失败",
        )

    def _spawn_publish(self, outcome: DispatchOutcome) -> None:
        event = DispatchFinished(outcome=outcome)
        self._spawn(self._bus.events.publish("dispatch.finished", event))

    def _spawn(self, coro: Coroutine[object, object, object]) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # ---------- 生命周期 ----------

    @override
    async def stop(self) -> None:
        tasks = list(self._background)
        for task in tasks:
            _ = task.cancel()
        if tasks:
            _ = await asyncio.gather(*tasks, return_exceptions=True)
        self._background.clear()


def _paths(task: PlannedTask) -> set[str]:
    """从 params 的路径字段里取写目标。"""
    result: set[str] = set()
    for key in PATH_KEYS:
        value = task.params.get(key)
        if isinstance(value, str):
            result.add(value)
        elif isinstance(value, list):
            result |= {item for item in cast("list[object]", value) if isinstance(item, str)}
    return result


__all__ = ["PLAN_CACHE_SECONDS", "DispatchService"]
