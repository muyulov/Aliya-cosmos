"""任务运行时：并发、超时、取消、幂等与心跳。

一次 `submit()` 的生命周期：

```text
queued → running → (ok | partial | failed | cancelled)
                 ↘ stalled（心跳缺失）→ running（恢复上报）/ abnormal（超时）
```

约定：

- 每次状态变化发 `agent.state.changed`；进度发 `agent.progress`；终态额外发
  `agent.result`；异常发 `agent.abnormal`。事件载荷一律 `model_dump(mode="json")`。
- 日志只记 `agent_id` / `task_id` / 耗时 / 成败 / 错误类型，不记 `params` 与结果正文。
- `stop()` 取消所有运行任务与巡检任务，幂等。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from collections.abc import Coroutine, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import ClassVar, Literal, cast, override

import core.logger.context as log_context
from agent.cosmos.contracts import (
    ActivityInfo,
    AgentAbnormal,
    AgentCard,
    AgentContext,
    AgentErrorInfo,
    AgentProgress,
    AgentRequest,
    AgentResult,
    AgentRuntimeState,
    AgentStatus,
    AgentTaskView,
    Artifact,
    EnvInfo,
    MoodInfo,
    OnlineInfo,
    PlannedTask,
    PresenceSnapshot,
    ReadablePresence,
    RuntimeStats,
    TaskBrief,
    TaskContextInfo,
    TaskHandle,
    TypingInfo,
)
from agent.cosmos.errors import (
    AgentBusyError,
    AgentInvokeError,
    AgentNotFoundError,
    CosmosError,
)
from agent.cosmos.services.presence import PresenceService
from agent.cosmos.services.registry import AgentRegistryService
from agent.cosmos.transport import BusService
from core.config import AgentSettings
from core.service import ClockService, Service

#: 进度上报内部节流（08 章 3.1）
REPORT_THROTTLE_SECONDS = 1.0
#: 超时后的宽限：先置 cancelled 再 cancel，仍未退出才判 failed(timeout)
TIMEOUT_GRACE_SECONDS = 5.0
#: 心跳巡检周期
HEARTBEAT_TICK_SECONDS = 1.0
#: 幂等缓存保留时长
IDEMPOTENT_TTL_SECONDS = 600.0
#: 历史耗时样本数（P50 用）
DURATION_SAMPLES = 20


@dataclass(slots=True)
class _Record:
    """一次任务的全部内部状态。"""

    task_id: str
    request: AgentRequest
    label: str
    session_id: str
    dispatch_id: str | None
    handle: TaskHandle
    done: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None
    started_at: datetime | None = None
    started_perf: float | None = None
    finished_at: datetime | None = None
    result: AgentResult | None = None
    status: AgentStatus = "queued"
    cancelled: bool = False
    timed_out: bool = False
    stalled: bool = False
    progress: float | None = None
    step: str | None = None
    artifacts: list[Artifact] = field(default_factory=list)
    last_heartbeat_at: datetime | None = None
    submitted: bool = False
    elapsed: float = 0.0


@dataclass(slots=True)
class _Slot:
    """单个 agent 的并发槽与统计。"""

    max_concurrent: int
    queue_allowed: bool
    running: int = 0
    queue: deque[_Record] = field(default_factory=deque)
    error_streak: int = 0
    durations: deque[float] = field(default_factory=lambda: deque(maxlen=DURATION_SAMPLES))
    status: AgentStatus = "idle"
    current_task_id: str | None = None
    last_error: AgentErrorInfo | None = None
    last_used_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    stalled: bool = False


class _ChildScope(AbstractContextManager[None]):
    """日志子步骤上下文：包一层以免调用方自己记 token。"""

    def __init__(self, name: str) -> None:
        self._name: str = name
        self._token: log_context.ContextToken | None = None

    @override
    def __enter__(self) -> None:  # pyright: ignore[reportMissingSuperCall]
        self._token = log_context.bind(子步骤=self._name)

    @override
    def __exit__(self, *args: object) -> None:
        if self._token is not None:
            log_context.reset(self._token)
            self._token = None


class _TaskContext:
    """`AgentContext` 的运行时实现。"""

    def __init__(self, service: AgentRuntimeService, record: _Record) -> None:
        self._service: AgentRuntimeService = service
        self._record: _Record = record
        self._last_report_perf: float | None = None
        self._pending: tuple[float | None, str | None, str | None] | None = None
        self._flush_task: asyncio.Task[None] | None = None

    @property
    def request_id(self) -> str:
        return self._record.request.request_id

    @property
    def cancelled(self) -> bool:
        return self._record.cancelled

    @property
    def remaining_seconds(self) -> float | None:
        deadline = self._record.request.deadline_at
        if deadline is None:
            return None
        return max(0.0, (deadline - self._service.now()).total_seconds())

    async def report(
        self,
        *,
        progress: float | None = None,
        step: str | None = None,
        message: str | None = None,
    ) -> None:
        """上报进度：进度单调不减，内部按 1s 节流并保留尾沿。"""
        record = self._record
        if progress is not None:
            if record.progress is not None and progress < record.progress:
                self._service.log.debug("进度倒退，已丢弃", task_id=record.task_id)
                progress = None
            else:
                record.progress = progress
        if step is not None:
            record.step = step
        record.last_heartbeat_at = self._service.now()
        self._service.slot(record.request.agent_id).last_heartbeat_at = record.last_heartbeat_at
        if record.stalled:
            record.stalled = False
            slot = self._service.slot(record.request.agent_id)
            slot.stalled = False
            slot.status = "running"
            await self._service.publish_state(record.request.agent_id)

        now = time.perf_counter()
        if (
            self._last_report_perf is None
            or now - self._last_report_perf >= REPORT_THROTTLE_SECONDS
        ):
            self._last_report_perf = now
            await self._service.publish_progress(record, step, message)
            return

        # 节流窗口内：保留最后一次，窗口结束时补发（尾沿）
        self._pending = (progress, step, message)
        if self._flush_task is None:
            remaining = REPORT_THROTTLE_SECONDS - (now - (self._last_report_perf or now))
            self._flush_task = asyncio.create_task(self._flush(remaining))

    async def heartbeat(self) -> None:
        """只刷新心跳时间，不发进度事件。"""
        now = self._service.now()
        self._record.last_heartbeat_at = now
        self._service.slot(self._record.request.agent_id).last_heartbeat_at = now

    async def emit(self, artifact: Artifact) -> None:
        """登记中间产物；同一 artifact 不重复。"""
        key = (artifact.kind, artifact.name, artifact.uri)
        if all((item.kind, item.name, item.uri) != key for item in self._record.artifacts):
            self._record.artifacts.append(artifact)

    async def read_presence(self) -> PresenceSnapshot | None:
        """按卡片权限读取用户状态；未授权或隐私关闭时返回 None。"""
        return self._service.presence_for(self._record.request.agent_id)

    def child_scope(self, name: str) -> AbstractContextManager[None]:
        """日志子步骤：进入时绑定上下文，退出时还原。"""
        return _ChildScope(name)

    async def _flush(self, delay: float) -> None:
        await asyncio.sleep(max(0.0, delay))
        self._flush_task = None
        pending, self._pending = self._pending, None
        if pending is None:
            return
        _progress, step, message = pending
        self._last_report_perf = time.perf_counter()
        await self._service.publish_progress(self._record, step, message)

    def cancel_flush(self) -> None:
        """终态到达时取消尚未执行的尾沿补发。"""
        if self._flush_task is not None:
            _ = self._flush_task.cancel()
            self._flush_task = None


class AgentRuntimeService(Service):
    """任务生命周期管理。"""

    name: ClassVar[str] = "cosmos.runtime"
    dependencies: ClassVar[tuple[type[Service], ...]] = (
        AgentRegistryService,
        BusService,
        ClockService,
    )

    def __init__(
        self,
        registry: AgentRegistryService,
        bus: BusService,
        clock: ClockService,
        config: AgentSettings,
    ) -> None:
        super().__init__()
        self._registry: AgentRegistryService = registry
        self._bus: BusService = bus
        self._clock: ClockService = clock
        self._config: AgentSettings = config
        #: 由 assembly 注入；用于按 AgentPermissions 提供受限的 presence 读取
        self._presence: PresenceService | None = None
        self._parallel: asyncio.Semaphore | None = None
        self._slots: dict[str, _Slot] = {}
        self._tasks: dict[str, _Record] = {}
        self._idempotent: dict[str, tuple[TaskHandle, float]] = {}
        self._monitor: asyncio.Task[None] | None = None
        self._background: set[asyncio.Task[object]] = set()
        self._active: bool = False

    # ---------- 生命周期 ----------

    @override
    async def start(self) -> None:
        if self._active:
            return
        self._active = True
        self._parallel = asyncio.Semaphore(max(1, self._config.cosmos.dispatch.max_parallel))
        self._monitor = asyncio.create_task(self._watch_heartbeats())
        self.log.info(
            "运行时已就绪",
            并发上限=max(1, self._config.cosmos.dispatch.max_parallel),
            心跳巡检秒=HEARTBEAT_TICK_SECONDS,
        )

    @override
    async def stop(self) -> None:
        """取消所有运行任务与巡检任务，幂等。"""
        if not self._active:
            return
        self._active = False
        if self._monitor is not None:
            _ = self._monitor.cancel()
            self._monitor = None
        running = [record for record in self._tasks.values() if record.task is not None]
        if running:
            self.log.info("正在停止运行时", 运行任务数=len(running))
        for record in running:
            record.cancelled = True
        tasks = [record.task for record in running if record.task is not None]
        for task in tasks:
            if not task.done():
                _ = task.cancel()
        if tasks:
            # 只等宽限期：handler 若吞掉 CancelledError，关闭流程不能被它拖住
            _, pending = await asyncio.wait(tasks, timeout=TIMEOUT_GRACE_SECONDS)
            for task in pending:
                _ = task.cancel()

        # 排队中、以及「被取消时还没轮到执行」的任务都要收口：
        # 对尚未首次调度的 Task 调用 cancel() 不会进入协程体，except 分支不会执行。
        for record in self._tasks.values():
            if not record.done.is_set():
                self._cancel_record(record)

        background = list(self._background)
        for task in background:
            if not task.done():
                _ = task.cancel()
        if background:
            _ = await asyncio.gather(*background, return_exceptions=True)
        self._background.clear()

    def _cancel_record(self, record: _Record) -> None:
        """把未启动的任务收敛到 cancelled 终态，并唤醒等待者。"""
        record.cancelled = True
        record.result = AgentResult.cancelled()
        record.status = "idle"
        record.finished_at = self._clock.now()
        record.done.set()

    def set_presence(self, presence: PresenceService) -> None:
        """注入用户状态服务；不放进构造器，保持 runtime 单测可独立构造。"""
        self._presence = presence

    def presence_for(self, agent_id: str) -> PresenceSnapshot | None:
        """按卡片权限返回过滤后的状态；无权限或隐私关闭时为 None。"""
        presence = self._presence
        if presence is None or not self._config.cosmos.presence.privacy.expose_to_subagents:
            return None
        card = self._registry.get(agent_id)
        if not card.permissions.read_presence:
            return None
        allowed = set(card.permissions.presence_fields)
        snapshot = presence.snapshot()
        defaults: dict[str, object] = {
            "online": OnlineInfo(),
            "activity": ActivityInfo(),
            "typing": TypingInfo(),
            "mood": MoodInfo(),
            "task_context": TaskContextInfo(),
            "env": EnvInfo(),
        }
        fields = snapshot.fields.model_copy(
            update={name: default for name, default in defaults.items() if name not in allowed}
        )
        return snapshot.model_copy(
            update={
                "fields": fields,
                "readable": ReadablePresence(summary="仅授权字段", tone="unknown", confidence=0.0),
            }
        )

    # ---------- 查询 ----------

    def now(self) -> datetime:
        """时间统一来源。"""
        return self._clock.now()

    def slot(self, agent_id: str) -> _Slot:
        """取（或创建）agent 的并发槽。"""
        card = self._registry.get(agent_id)
        slot = self._slots.get(agent_id)
        if slot is None:
            slot = _Slot(
                max_concurrent=card.execution.max_concurrent,
                queue_allowed=card.execution.queue,
            )
            self._slots[agent_id] = slot
        return slot

    def agent_state(self, agent_id: str) -> AgentRuntimeState:
        """运行态快照。"""
        if not self._registry.enabled(agent_id):
            return AgentRuntimeState(agent_id=agent_id, status="disabled")
        slot = self._slots.get(agent_id)
        if slot is None:
            return AgentRuntimeState(agent_id=agent_id, status="idle")
        return AgentRuntimeState(
            agent_id=agent_id,
            status=slot.status,
            running=slot.running,
            queued=len(slot.queue),
            current_task_id=slot.current_task_id,
            last_error=slot.last_error,
            error_streak=slot.error_streak,
            last_used_at=slot.last_used_at,
            last_heartbeat_at=slot.last_heartbeat_at,
        )

    def stats(self, agent_id: str) -> RuntimeStats:
        """历史 P50 与连续失败次数；没有样本时 P50 为 None。"""
        slot = self._slots.get(agent_id)
        if slot is None or not slot.durations:
            return RuntimeStats()
        ordered = sorted(slot.durations)
        middle = len(ordered) // 2
        p50 = ordered[middle]
        return RuntimeStats(p50_seconds=p50, error_streak=slot.error_streak)

    def task_view(self, agent_id: str) -> AgentTaskView | None:
        """桌面视图：优先当前/排队任务，否则取最近一次。"""
        records = [record for record in self._tasks.values() if record.request.agent_id == agent_id]
        if not records:
            return None
        records.sort(
            key=lambda record: record.started_at or record.request.created_at,
            reverse=True,
        )
        active = next((record for record in records if not record.done.is_set()), records[0])
        slot = self._slots.get(agent_id)
        elapsed = active.elapsed
        if not active.done.is_set() and active.started_perf is not None:
            elapsed = time.perf_counter() - active.started_perf
        started_at = active.started_at or active.request.created_at
        return AgentTaskView(
            task_id=active.task_id,
            label=active.label,
            progress=active.progress,
            step=active.step,
            started_at=started_at,
            elapsed_seconds=max(0.0, elapsed),
            queue_depth=len(slot.queue) if slot is not None else 0,
        )

    def recent_results(self, limit: int = 5) -> list[tuple[AgentResult, datetime, float]]:
        """最近完成的任务结果：世界推理用，不含正文与 task_id。"""
        records = [
            record
            for record in self._tasks.values()
            if record.done.is_set() and record.result is not None
        ]
        records.sort(
            key=lambda record: record.finished_at or record.request.created_at,
            reverse=True,
        )
        results: list[tuple[AgentResult, datetime, float]] = []
        for record in records[:limit]:
            result = record.result
            if result is None:
                continue
            results.append(
                (result, record.finished_at or record.request.created_at, record.elapsed)
            )
        return results

    async def list_tasks(self, agent_id: str | None = None, limit: int = 20) -> list[TaskBrief]:
        """任务摘要：按开始时间倒序，不含正文。"""
        records = [
            record
            for record in self._tasks.values()
            if agent_id is None or record.request.agent_id == agent_id
        ]
        records.sort(key=lambda record: record.started_at or self._clock.now(), reverse=True)
        return [self._brief(record) for record in records[:limit]]

    async def wait(self, task_id: str) -> AgentResult:
        """等一个任务到终态；测试与调度聚合使用。"""
        record = self._tasks.get(task_id)
        if record is None:
            raise AgentNotFoundError(f"任务不存在：{task_id}", reason="task_not_found")
        _ = await record.done.wait()
        return record.result or AgentResult.failed(code="internal", message="任务没有结果")

    # ---------- 提交 ----------

    async def submit(
        self,
        agent_id: str,
        params: dict[str, object],
        *,
        label: str,
        session_id: str,
        request_id: str | None = None,
        deadline_at: datetime | None = None,
    ) -> TaskHandle:
        """提交单个任务：并发未满直接启动，满了按 `queue` 决定排队或拒绝。"""
        if request_id is not None:
            cached = self._cached(request_id)
            if cached is not None:
                # 幂等重放不能再触发 supersede，否则会把正在跑的旧任务取消。
                self.log.debug(
                    "命中幂等窗口，复用任务",
                    agent_id=agent_id,
                    task_id=cached.task_id,
                    request_id=request_id,
                )
                return cached
        await self._supersede(agent_id, session_id)
        record = self._prepare(
            agent_id,
            params,
            label=label,
            session_id=session_id,
            request_id=request_id,
            deadline_at=deadline_at,
        )
        if not record.submitted:
            self._start(record)
        return record.handle

    async def submit_many(
        self,
        tasks: Sequence[PlannedTask],
        *,
        session_id: str,
        dispatch_id: str,
    ) -> list[TaskHandle]:
        """按 `depends_on` 分层提交：同层并发、层间串行；失败的下游标 skipped。"""
        for agent_id in {task.agent_id for task in tasks}:
            await self._supersede(agent_id, session_id)
        records: list[_Record] = []
        for task in tasks:
            try:
                records.append(
                    self._prepare(
                        task.agent_id,
                        task.params,
                        label=task.label,
                        session_id=session_id,
                        request_id=None,
                        deadline_at=None,
                        dispatch_id=dispatch_id,
                    )
                )
            except (AgentBusyError, AgentNotFoundError, AgentInvokeError) as exc:
                records.append(self._rejected_record(task, session_id, dispatch_id, exc))
        self._spawn(self._run_batch(records, tasks))
        return [record.handle for record in records]

    async def cancel(self, task_id: str) -> None:
        """协作式取消：先置 cancelled，再 cancel；等待宽限期。"""
        record = self._tasks.get(task_id)
        if record is None:
            raise AgentNotFoundError(f"任务不存在：{task_id}", reason="task_not_found")
        if record.done.is_set():
            return
        self.log.info(
            "收到取消请求",
            agent_id=record.request.agent_id,
            task_id=task_id,
            已排队=record.task is None,
        )
        record.cancelled = True
        if record.task is None:
            # 排队中或尚未首次调度：直接收口，不能等一个不存在的协程。
            slot = self._slots.get(record.request.agent_id)
            if slot is not None and record in slot.queue:
                slot.queue.remove(record)
            self._cancel_record(record)
            return
        if not record.task.done():
            _ = record.task.cancel()
        try:
            _ = await asyncio.wait_for(record.done.wait(), TIMEOUT_GRACE_SECONDS)
        except TimeoutError:
            # 宽限内没退出：标 stalled，交给 Aliya 决定是否放弃
            record.stalled = True
            slot = self.slot(record.request.agent_id)
            slot.stalled = True
            slot.status = "stalled"
            await self.publish_state(record.request.agent_id)
            self.log.warning(
                "取消宽限内未退出，标记 stalled",
                agent_id=record.request.agent_id,
                task_id=task_id,
                宽限秒=TIMEOUT_GRACE_SECONDS,
            )
            raise

    async def retry(self, task_id: str) -> TaskHandle:
        """重试：仅 `retryable=true` 且上次为失败时可用，生成新 `task_id`。"""
        record = self._tasks.get(task_id)
        if record is None:
            raise AgentNotFoundError(f"任务不存在：{task_id}", reason="task_not_found")
        card = self._registry.get(record.request.agent_id)
        if not card.execution.retryable:
            raise AgentInvokeError("该任务不允许重试", agent_id=card.id, code="not_retriable")
        if record.result is None or record.result.status not in {"failed", "partial"}:
            raise AgentInvokeError("只有失败的任务可以重试", agent_id=card.id, code="not_retriable")
        self.log.info(
            "重试失败任务",
            agent_id=card.id,
            task_id=task_id,
            上次状态=record.result.status,
        )
        return await self.submit(
            record.request.agent_id,
            dict(record.request.params),
            label=record.label,
            session_id=record.session_id,
        )

    # ---------- 内部：提交与执行 ----------

    async def _supersede(self, agent_id: str, session_id: str) -> None:
        """同会话同 agent 的新任务按卡片策略取消旧任务。"""
        try:
            card = self._registry.get(agent_id)
        except AgentNotFoundError:
            # 不存在的 agent 交给 _prepare / _rejected_record 处理
            return
        if not card.execution.cancel_on_supersede:
            return
        for record in list(self._tasks.values()):
            if (
                record.request.agent_id == agent_id
                and record.session_id == session_id
                and not record.done.is_set()
            ):
                try:
                    await self.cancel(record.task_id)
                except (AgentNotFoundError, TimeoutError):
                    self.log.warning("取消失败或超时，继续提交新任务", task_id=record.task_id)

    def task_brief(self, task_id: str) -> TaskBrief | None:
        """单任务摘要；提交回执用它判断是否被拒绝。"""
        record = self._tasks.get(task_id)
        return self._brief(record) if record is not None else None

    def _prepare(
        self,
        agent_id: str,
        params: dict[str, object],
        *,
        label: str,
        session_id: str,
        request_id: str | None,
        deadline_at: datetime | None,
        dispatch_id: str | None = None,
    ) -> _Record:
        card = self._registry.get(agent_id)
        if not self._registry.enabled(agent_id):
            raise AgentNotFoundError(f"agent 已停用：{agent_id}", reason="disabled")
        _ = self._registry.handler(agent_id)

        if request_id is not None:
            cached = self._cached(request_id)
            if cached is not None:
                return self._tasks[cached.task_id]

        resolved_request_id = request_id or f"req_{uuid.uuid4().hex}"
        task_id = f"tsk_{uuid.uuid4().hex}"
        timeout = card.execution.timeout_seconds
        deadline = deadline_at or self._clock.now() + timedelta(seconds=timeout)
        request = AgentRequest(
            request_id=resolved_request_id,
            agent_id=agent_id,
            params=dict(params),
            label=label,
            created_at=self._clock.now(),
            deadline_at=deadline,
            correlation_id=dispatch_id or task_id,
        )
        handle = TaskHandle(
            task_id=task_id,
            agent_id=agent_id,
            label=label,
            accepted_at=self._clock.now(),
        )
        record = _Record(
            task_id=task_id,
            request=request,
            label=label,
            session_id=session_id,
            dispatch_id=dispatch_id,
            handle=handle,
        )
        self._tasks[task_id] = record
        if request_id is not None:
            self._idempotent[request_id] = (handle, time.perf_counter())
        return record

    def _spawn(self, coro: Coroutine[object, object, object]) -> None:
        """后台任务登记在册：既避免 RUF006 丢引用，也便于 stop 时统一取消。"""
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _cached(self, request_id: str) -> TaskHandle | None:
        """幂等缓存：同一 request_id 在窗口内返回同一句柄。"""
        cached = self._idempotent.get(request_id)
        if cached is None:
            return None
        handle, created = cached
        if time.perf_counter() - created > IDEMPOTENT_TTL_SECONDS:
            _ = self._idempotent.pop(request_id, None)
            return None
        return handle

    def _rejected_record(
        self,
        task: PlannedTask,
        session_id: str,
        dispatch_id: str,
        exc: Exception,
    ) -> _Record:
        """提交即失败的任务：造一条终态记录，让批次聚合能看到它。"""
        task_id = f"tsk_{uuid.uuid4().hex}"
        request = AgentRequest(
            request_id=f"req_{uuid.uuid4().hex}",
            agent_id=task.agent_id,
            params=dict(task.params),
            label=task.label,
            created_at=self._clock.now(),
            deadline_at=None,
            correlation_id=dispatch_id,
        )
        handle = TaskHandle(
            task_id=task_id,
            agent_id=task.agent_id,
            label=task.label,
            accepted_at=self._clock.now(),
            queued=False,
        )
        record = _Record(
            task_id=task_id,
            request=request,
            label=task.label,
            session_id=session_id,
            dispatch_id=dispatch_id,
            handle=handle,
            status="abnormal",
        )
        code = getattr(exc, "code", "agent_invoke")
        record.result = AgentResult.failed(code=str(code), message=str(exc))
        record.started_at = self._clock.now()
        record.finished_at = record.started_at
        record.done.set()
        self._tasks[task_id] = record
        self.log.warning(
            "任务提交被拒",
            agent_id=task.agent_id,
            task_id=task_id,
            错误码=str(code),
            错误消息=str(exc),
        )
        return record

    def _start(self, record: _Record) -> None:
        """按并发策略启动或入队；同步执行，判定不依赖事件循环调度。"""
        if not self._active:
            self.log.debug(
                "运行时已停止，任务直接取消",
                agent_id=record.request.agent_id,
                task_id=record.task_id,
            )
            self._cancel_record(record)
            return
        agent_id = record.request.agent_id
        record.submitted = True
        slot = self.slot(agent_id)
        if slot.running >= slot.max_concurrent:
            if not slot.queue_allowed:
                record.result = AgentResult.failed(
                    code="agent_busy",
                    message=f"{agent_id} 并发已满",
                    retriable=True,
                )
                record.status = "abnormal"
                record.done.set()
                self.log.warning(
                    "并发已满，任务被拒绝",
                    agent_id=agent_id,
                    task_id=record.task_id,
                    并发上限=slot.max_concurrent,
                )
                raise AgentBusyError(f"{agent_id} 并发已满", agent_id=agent_id)
            slot.queue.append(record)
            slot.status = "queued"
            record.status = "queued"
            record.handle.queued = True
            self.log.info(
                "任务已排队",
                agent_id=agent_id,
                task_id=record.task_id,
                排队数=len(slot.queue),
                运行中=slot.running,
                并发上限=slot.max_concurrent,
            )
            self._spawn(self.publish_state(agent_id))
            return
        slot.running += 1
        slot.status = "running"
        slot.current_task_id = record.task_id
        record.status = "running"
        record.handle.queued = False
        record.started_at = self._clock.now()
        record.started_perf = time.perf_counter()
        record.last_heartbeat_at = record.started_at
        slot.last_heartbeat_at = record.started_at
        self.log.info(
            "任务开始执行",
            agent_id=agent_id,
            task_id=record.task_id,
            运行中=slot.running,
            并发上限=slot.max_concurrent,
            标签=record.label,
        )
        record.task = asyncio.create_task(self._execute(record, slot))

    async def _execute(self, record: _Record, slot: _Slot) -> None:
        try:
            await self.publish_state(record.request.agent_id)
            if self._parallel is None:
                await self._run_handler(record, slot)
            else:
                async with self._parallel:
                    await self._run_handler(record, slot)
        except asyncio.CancelledError:
            self._cancel_record(record)
            raise
        except Exception as exc:
            # 兜底：任何未预期异常都不能让等待者永远挂住
            self.log_error(
                "任务执行异常", exc, agent_id=record.request.agent_id, task_id=record.task_id
            )
            record.result = AgentResult.failed(
                code="internal", message=f"{type(exc).__name__}: {exc}", retriable=False
            )
            record.status = "abnormal"
            record.done.set()
        finally:
            slot.running -= 1
            if slot.current_task_id == record.task_id:
                slot.current_task_id = None
            self._dequeue(slot)

    def _dequeue(self, slot: _Slot) -> None:
        """腾出并发位后按 FIFO 启动队首。"""
        if not self._active:
            while slot.queue:
                self._cancel_record(slot.queue.popleft())
            return
        if slot.running >= slot.max_concurrent or not slot.queue:
            return
        record = slot.queue.popleft()
        slot.running += 1
        slot.status = "running"
        slot.current_task_id = record.task_id
        record.status = "running"
        record.handle.queued = False
        record.started_at = self._clock.now()
        record.started_perf = time.perf_counter()
        record.last_heartbeat_at = record.started_at
        slot.last_heartbeat_at = record.started_at
        self.log.debug(
            "任务出队启动",
            agent_id=record.request.agent_id,
            task_id=record.task_id,
            剩余排队=len(slot.queue),
            运行中=slot.running,
        )
        record.task = asyncio.create_task(self._execute(record, slot))

    async def _run_handler(self, record: _Record, slot: _Slot) -> None:
        card = self._registry.get(record.request.agent_id)
        handler = self._registry.handler(record.request.agent_id)
        ctx = _TaskContext(self, record)
        task = asyncio.ensure_future(handler(record.request, cast("AgentContext", ctx)))
        timeout = card.execution.timeout_seconds
        loop = asyncio.get_running_loop()
        timed_out = False

        def _on_timeout() -> None:
            nonlocal timed_out
            timed_out = True
            record.cancelled = True
            record.timed_out = True
            if not task.done():
                _ = task.cancel()

        handle = loop.call_later(timeout, _on_timeout)
        try:
            result = await asyncio.wait_for(asyncio.shield(task), timeout + TIMEOUT_GRACE_SECONDS)
        except TimeoutError:
            result = None
        except asyncio.CancelledError:
            if not (timed_out or record.cancelled):
                raise
            result = None
        except Exception as exc:
            self.log_error(
                "子 agent 执行异常",
                exc,
                agent_id=record.request.agent_id,
                task_id=record.task_id,
            )
            result = AgentResult.failed(
                code="internal",
                message=f"{type(exc).__name__}: {exc}",
                retriable=False,
            )
        finally:
            handle.cancel()
            ctx.cancel_flush()
            if not task.done():
                _ = task.cancel()

        if record.timed_out:
            result = AgentResult.failed(
                code="timeout",
                message=f"超过 {timeout}s 未完成",
                retriable=True,
            )
        elif record.cancelled:
            if isinstance(result, AgentResult):
                # handler 可能选择在取消后返回自己的 cancelled 结果，两种都接受
                if result.status != "cancelled":
                    result = AgentResult.cancelled(summary=result.summary)
            else:
                result = AgentResult.cancelled()

        await self._finalize(record, slot, result)

    async def _finalize(self, record: _Record, slot: _Slot, result: AgentResult | None) -> None:
        normalized = self._normalize(record, result)
        record.result = normalized
        record.finished_at = self._clock.now()
        record.status = _status_from_result(normalized)
        slot.last_used_at = record.finished_at
        slot.current_task_id = None

        if record.started_perf is not None:
            record.elapsed = max(0.0, time.perf_counter() - record.started_perf)
            slot.durations.append(record.elapsed)

        if normalized.status in {"failed", "partial"}:
            slot.error_streak += 1
            slot.last_error = normalized.error
            # partial 也带着错误：桌面显示 abnormal，避免「部分失败」看起来像空闲
            slot.status = "abnormal"
        elif normalized.status == "ok":
            slot.error_streak = 0
            slot.last_error = None
            slot.status = "idle"
        else:
            slot.status = "idle"

        if normalized.status == "failed" and normalized.error is not None:
            error = normalized.error
            if error.code == "timeout":
                await self._publish_abnormal(record.request.agent_id, "timeout", error)
            else:
                await self._publish_abnormal(record.request.agent_id, "error", error)

        self._log_finished(record, normalized)
        record.done.set()
        await self.publish_state(record.request.agent_id)
        await self.publish_result(record)

    def _log_finished(self, record: _Record, result: AgentResult) -> None:
        """任务终态统一日志：成功 / 失败 / 取消 / 超时都经过 `_finalize`。

        失败与部分成功走 WARNING，便于错误日志文件单独收口；字段与
        「任务开始执行」同形（agent_id / task_id），可按任务 id 串起完整生命周期。
        """
        fields: dict[str, object] = {
            "agent_id": record.request.agent_id,
            "task_id": record.task_id,
            "状态": result.status,
            "耗时毫秒": round(record.elapsed * 1000, 1),
        }
        if record.step:
            fields["步骤"] = record.step
        if result.error is not None:
            fields["错误码"] = result.error.code
            fields["错误消息"] = result.error.message
        if result.status in {"failed", "partial"}:
            self.log.warning("任务结束", face=None, **fields)
        else:
            self.log.info("任务结束", face=None, **fields)

    def _normalize(self, record: _Record, result: AgentResult | None) -> AgentResult:
        """兜底：handler 没返回结果 / 摘要为空 / 内容超长。"""
        if not isinstance(result, AgentResult):
            return AgentResult.failed(
                code="internal",
                message="handler 没有返回 AgentResult",
                retriable=False,
            )
        if not result.summary.strip():
            return AgentResult.failed(
                code="internal", message="handler 返回了空摘要", retriable=False
            )
        updates: dict[str, object] = {}
        if not result.request_id:
            updates["request_id"] = record.request.request_id
        merged = list(result.artifacts)
        for artifact in record.artifacts:
            key = (artifact.kind, artifact.name, artifact.uri)
            if all((item.kind, item.name, item.uri) != key for item in merged):
                merged.append(artifact)
        if merged:
            updates["artifacts"] = merged
        # 正文长度由各工具自己控制；cosmos 不再做二次截断，避免把结论截掉
        if not updates:
            return result
        return result.model_copy(update=updates)

    # ---------- 批量依赖 ----------

    def _layers(self, records: list[_Record], tasks: Sequence[PlannedTask]) -> list[list[int]]:
        """按 `depends_on` 分层：层内无依赖，层间串行。"""
        depth: dict[int, int] = {}

        def visit(index: int) -> int:
            if index in depth:
                return depth[index]
            level = 0
            for dependency in tasks[index].depends_on:
                level = max(level, visit(dependency) + 1)
            depth[index] = level
            return level

        for index in range(len(records)):
            _ = visit(index)
        levels: list[list[int]] = [[] for _ in range(max(depth.values(), default=-1) + 1)]
        for index in range(len(records)):
            levels[depth[index]].append(index)
        return levels

    async def _run_batch(self, records: list[_Record], tasks: Sequence[PlannedTask]) -> None:
        """逐层提交；某任务失败时其下游标 skipped，其余分支继续。"""
        if not records:
            return
        failed: set[int] = {index for index, record in enumerate(records) if record.done.is_set()}
        for layer in self._layers(records, tasks):
            runnable: list[_Record] = []
            for index in layer:
                record = records[index]
                if record.done.is_set():
                    continue
                dependencies = tasks[index].depends_on
                if any(dependency in failed for dependency in dependencies):
                    self._skip(record)
                    failed.add(index)
                    continue
                runnable.append(record)
            # 全局并发上限：按窗口分批启动，而不是一次把整层全部拉起。
            limit = max(1, self._config.cosmos.dispatch.max_parallel)
            for offset in range(0, len(runnable), limit):
                chunk = runnable[offset : offset + limit]
                for record in chunk:
                    try:
                        self._start(record)
                    except CosmosError as exc:
                        # 提交期错误（并发满 / agent 被注销）都收敛成 skipped
                        self._skip(record, 原因=f"提交失败：{type(exc).__name__}")
                _ = await asyncio.gather(*(record.done.wait() for record in chunk))
            for index in layer:
                record = records[index]
                if record.result is not None and record.result.status in {"failed", "partial"}:
                    failed.add(index)

    def _skip(self, record: _Record, *, 原因: str = "上游任务失败") -> None:
        """上游失败或提交期错误 → 不执行，结果标 skipped。"""
        self.log.warning(
            "任务跳过",
            agent_id=record.request.agent_id,
            task_id=record.task_id,
            原因=原因,
        )
        record.result = AgentResult.failed(
            code="skipped",
            message=f"{原因}，已跳过",
            retriable=False,
        )
        record.status = "abnormal"
        record.finished_at = self._clock.now()
        record.done.set()

    # ---------- 事件 ----------

    async def publish_state(self, agent_id: str) -> None:
        """发布 `agent.state.changed`。"""
        state = self.agent_state(agent_id)
        _ = await self._bus.events.publish("agent.state.changed", state)

    async def publish_progress(
        self, record: _Record, step: str | None, message: str | None
    ) -> None:
        """发布 `agent.progress`。"""
        progress = AgentProgress(
            task_id=record.task_id,
            agent_id=record.request.agent_id,
            label=record.label,
            progress=record.progress,
            step=step or record.step,
            message=message,
            at=self._clock.now(),
        )
        _ = await self._bus.events.publish("agent.progress", progress)

    async def publish_result(self, record: _Record) -> None:
        """发布 `agent.result`：载荷带 `dispatch_id` 与结果。"""
        result = record.result
        if result is None:
            return
        payload: dict[str, object] = {
            "agent_id": record.request.agent_id,
            "task_id": record.task_id,
            "dispatch_id": record.dispatch_id,
            "result": result.model_dump(mode="json"),
        }
        _ = await self._bus.events.publish("agent.result", payload)

    async def _publish_abnormal(
        self,
        agent_id: str,
        reason: Literal["timeout", "stalled", "error", "internal"],
        error: AgentErrorInfo | None,
    ) -> None:
        abnormal = AgentAbnormal(
            agent_id=agent_id,
            reason=reason,
            error=error,
            at=self._clock.now(),
        )
        _ = await self._bus.events.publish("agent.abnormal", abnormal)

    # ---------- 心跳巡检 ----------

    async def _watch_heartbeats(self) -> None:
        """1s tick：超过 `heartbeat_seconds` 没有上报则判 stalled。"""
        while True:
            await asyncio.sleep(HEARTBEAT_TICK_SECONDS)
            now = self._clock.now()
            for record in list(self._tasks.values()):
                if record.done.is_set() or record.status != "running":
                    continue
                card = self._registry.get(record.request.agent_id)
                last = record.last_heartbeat_at or record.started_at
                if last is None:
                    continue
                elapsed = (now - last).total_seconds()
                if elapsed > card.execution.heartbeat_seconds and not record.stalled:
                    record.stalled = True
                    slot = self.slot(record.request.agent_id)
                    slot.stalled = True
                    slot.status = "stalled"
                    self.log.warning(
                        "心跳超时，标记 stalled",
                        agent_id=record.request.agent_id,
                        task_id=record.task_id,
                        静默秒=round(elapsed, 1),
                        阈值秒=card.execution.heartbeat_seconds,
                    )
                    await self.publish_state(record.request.agent_id)
                    await self._publish_abnormal(record.request.agent_id, "stalled", None)

    # ---------- 摘要 ----------

    def _brief(self, record: _Record) -> TaskBrief:
        try:
            card: AgentCard = self._registry.get(record.request.agent_id)
            agent_name = card.name
        except AgentNotFoundError:
            # 提交期被拒的任务可能没有卡片，摘要仍要能返回。
            agent_name = record.request.agent_id
        elapsed = record.elapsed
        if record.done.is_set() is False and record.started_perf is not None:
            elapsed = time.perf_counter() - record.started_perf
        return TaskBrief(
            task_id=record.task_id,
            agent_name=agent_name,
            label=record.label,
            status=record.status,
            progress=record.progress,
            elapsed_seconds=elapsed,
        )


def _status_from_result(result: AgentResult) -> AgentStatus:
    """终态结果映射回桌面状态。"""
    if result.status in {"failed", "partial"}:
        return "abnormal"
    return "idle"
