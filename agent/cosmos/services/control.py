"""Aliya 控制面：能力协商、订阅、桌面与调度转发。

设计原则：能力够用、权限最小、错误可读。

- 本层不实现业务，只做白名单校验、超时与转发。
- Aliya 无注册 / 启用禁用 / 改采集开关 / 改世界配置的权限，这些方法**不暴露**。
- 所有转发方法套统一超时；超时抛 `CosmosError(code="timeout", retriable=True)`。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, Awaitable, Sequence
from typing import ClassVar

from agent.cosmos.contracts import (
    AgentDetail,
    AliyaContext,
    CosmosCapabilities,
    CosmosEvent,
    DesktopLayoutPatch,
    DesktopSnapshot,
    DispatchHandle,
    DispatchIntent,
    DispatchPlan,
    InboundEnvelope,
    LinkState,
    MessageKind,
    PresenceChanged,
    PresenceField,
    PresenceInput,
    PresenceSnapshot,
    ProactiveReceipt,
    TaskBrief,
    TaskHandle,
    TerminalMessage,
    WorldInference,
    WorldInferenceEvent,
)
from agent.cosmos.errors import (
    AgentInvokeError,
    ContractVersionError,
    CosmosError,
    DispatchError,
    MessageDeliveryError,
)
from agent.cosmos.services.desktop import DesktopService
from agent.cosmos.services.dispatch import DispatchService
from agent.cosmos.services.presence import PresenceService
from agent.cosmos.services.runtime import AgentRuntimeService
from agent.cosmos.services.terminal import TerminalService
from agent.cosmos.services.world import WorldBridgeService
from agent.cosmos.transport import BusService
from core.config import AgentSettings
from core.service import ClockService, Service

#: `handshake()` 声明的主题白名单（设计 09 章 7 节）
EVENT_TOPICS: tuple[str, ...] = (
    "agent.state.changed",
    "agent.progress",
    "agent.result",
    "agent.abnormal",
    "desktop.layout.changed",
    "presence.changed",
    "world.inference",
    "user.active",
    "link.state.changed",
    "dispatch.finished",
)

#: 各方法的超时（秒）；测试可 monkeypatch
TIMEOUTS: dict[str, float] = {
    "handshake": 3.0,
    "describe_desktop": 2.0,
    "get_agent": 2.0,
    "patch_layout": 2.0,
    "plan": 5.0,
    "invoke_agent": 2.0,
    "execute_plan": 2.0,
    "cancel": 5.0,
    "retry": 2.0,
    "list_tasks": 2.0,
    "deliver": 3.0,
    "notify": 3.0,
    "send_proactive": 3.0,
    "world": 12.0,
}

#: 当前支持的契约主版本
SUPPORTED_MAJOR = "1"


class ControlService(Service):
    """Aliya 能看到的全部能力。"""

    name: ClassVar[str] = "cosmos.control"
    dependencies: ClassVar[tuple[type[Service], ...]] = (
        TerminalService,
        DesktopService,
        DispatchService,
        PresenceService,
        WorldBridgeService,
        AgentRuntimeService,
        BusService,
        ClockService,
    )

    def __init__(
        self,
        terminal: TerminalService,
        desktop: DesktopService,
        dispatch: DispatchService,
        presence: PresenceService,
        world: WorldBridgeService,
        runtime: AgentRuntimeService,
        bus: BusService,
        clock: ClockService,
        config: AgentSettings,
    ) -> None:
        super().__init__()
        self._terminal: TerminalService = terminal
        self._desktop: DesktopService = desktop
        self._dispatch: DispatchService = dispatch
        self._presence: PresenceService = presence
        self._world: WorldBridgeService = world
        self._runtime: AgentRuntimeService = runtime
        self._bus: BusService = bus
        self._clock: ClockService = clock
        self._config: AgentSettings = config

    # ---------- 会话 ----------

    async def handshake(self) -> CosmosCapabilities:
        """版本与能力协商；契约主版本不一致直接报错。"""
        self._check_contract()
        return CosmosCapabilities(
            api_version=self._config.cosmos.contract.api_version,
            contract_version=self._config.cosmos.contract.contract_version,
            event_topics=list(EVENT_TOPICS),
            features=self._features(),
            limits={
                "max_parallel": self._config.cosmos.dispatch.max_parallel,
                "max_content_chars": self._config.cosmos.desktop.max_content_chars,
                "history_limit": self._config.cosmos.desktop.history_limit,
            },
        )

    def _check_contract(self) -> None:
        version = self._config.cosmos.contract.contract_version
        major = version.split(".", 1)[0]
        if major != SUPPORTED_MAJOR:
            raise ContractVersionError(
                f"契约主版本不兼容：对端 {version}，本端 {SUPPORTED_MAJOR}.x"
            )

    def _features(self) -> list[str]:
        features: list[str] = []
        if self._config.cosmos.world.enabled:
            features.append("world")
        if self._config.cosmos.presence.mood.provider != "off":
            features.append("presence.mood")
        if self._config.cosmos.dispatch.mode == "auto":
            features.append("dispatch.auto")
        return features

    def inbound(self) -> AsyncGenerator[InboundEnvelope, None]:
        """用户消息信封流。"""
        return self._bus.messages.subscribe("aliya")

    def subscribe(
        self, topics: Sequence[str], *, since_sequence: int | None = None
    ) -> AsyncGenerator[CosmosEvent, None]:
        """事件订阅：先校验白名单，非法主题直接拒绝。

        补发缺口过大时退回「当前快照 + 实时事件」，避免调用方拿到一个无法
        恢复的 MessageDeliveryError。
        """
        unknown = [topic for topic in topics if topic not in EVENT_TOPICS]
        if unknown:
            raise ValueError(f"未知事件主题：{'、'.join(unknown)}")
        try:
            return self._bus.events.subscribe(list(topics), since_sequence=since_sequence)
        except MessageDeliveryError as exc:
            self.log.warning("事件缺口过大，改推全量快照", 原因=str(exc))
            return self._snapshot_stream(list(topics))

    async def _snapshot_stream(self, topics: list[str]) -> AsyncGenerator[CosmosEvent, None]:
        """先推当前快照，再续实时事件；用于缺口过大后的重建。"""
        now = self._clock.now()
        for topic in topics:
            payload = await self._snapshot_payload(topic)
            if payload is None:
                continue
            yield CosmosEvent(
                event_id=f"evt_{uuid.uuid4().hex}",
                topic=topic,
                sequence=0,
                at=now,
                payload=payload,
            )
        async for event in self._bus.events.subscribe(topics):
            yield event

    async def _snapshot_payload(self, topic: str) -> dict[str, object] | None:
        if topic == "desktop.layout.changed":
            return (await self._desktop.describe()).model_dump(mode="json")
        if topic == "presence.changed":
            snapshot = self._presence.snapshot()
            return PresenceChanged(
                revision=snapshot.revision,
                changed=[],
                snapshot=snapshot,
                readable=snapshot.readable,
                at=snapshot.captured_at,
            ).model_dump(mode="json")
        if topic == "world.inference":
            view = self._world.view()
            if view is None:
                return None
            age = max(0.0, (self._clock.now() - view.generated_at).total_seconds())
            return WorldInferenceEvent(
                inference=view,
                stale=view.stale,
                age_seconds=age,
                trigger="tick",
            ).model_dump(mode="json")
        if topic == "link.state.changed":
            queued_user, queued_aliya = self._terminal.queue_depth()
            return {
                "state": await self.get_link_state(),
                "queued_user_messages": queued_user,
                "queued_aliya_messages": queued_aliya,
            }
        return None

    # ---------- 桌面 ----------

    async def describe_desktop(self) -> DesktopSnapshot:
        return await self._with_timeout("describe_desktop", self._desktop.describe())

    async def get_agent(self, agent_id: str) -> AgentDetail:
        return await self._with_timeout("get_agent", self._desktop.get(agent_id))

    async def open_agent(self, agent_id: str) -> AgentDetail:
        """桌面「打开」：只返回详情与参数 schema，不启动任何任务。"""
        return await self.get_agent(agent_id)

    async def patch_layout(self, patch: DesktopLayoutPatch) -> DesktopSnapshot:
        return await self._with_timeout("patch_layout", self._desktop.patch_layout(patch))

    # ---------- 调度 ----------

    async def plan(self, intent: DispatchIntent) -> DispatchPlan:
        return await self._with_timeout("plan", self._dispatch.plan(intent))

    async def invoke_agent(
        self,
        agent_id: str,
        params: dict[str, object],
        *,
        session_id: str,
    ) -> TaskHandle:
        """显式调用：桌面点击路径。"""
        plan = await self.plan(
            DispatchIntent(
                source="desktop",
                session_id=session_id,
                agent_id=agent_id,
                params=params,
            )
        )
        if plan.decision == "clarify" or not plan.tasks:
            missing = "、".join(plan.missing) if plan.missing else "参数"
            raise AgentInvokeError(
                f"参数不足，无法调用：{missing}",
                agent_id=agent_id,
                code="invalid_params",
            )
        if plan.decision != "single":
            raise DispatchError(f"无法调度：{plan.rationale}")

        first = plan.tasks[0]
        handle = await self._with_timeout("invoke_agent", self._dispatch.execute(plan))
        if not handle.task_ids:
            raise DispatchError("任务提交失败：没有返回任务标识")
        return TaskHandle(
            task_id=handle.task_ids[0],
            agent_id=agent_id,
            label=first.label,
            accepted_at=handle.accepted_at,
        )

    async def execute_plan(self, plan: DispatchPlan) -> DispatchHandle:
        return await self._with_timeout("execute_plan", self._dispatch.execute(plan))

    async def cancel_task(self, task_id: str) -> None:
        await self._with_timeout("cancel", self._runtime.cancel(task_id))

    async def cancel_dispatch(self, dispatch_id: str) -> None:
        await self._with_timeout("cancel", self._dispatch.cancel(dispatch_id))

    async def retry_task(self, task_id: str) -> TaskHandle:
        return await self._with_timeout("retry", self._runtime.retry(task_id))

    async def list_tasks(self, agent_id: str | None = None, limit: int = 20) -> list[TaskBrief]:
        return await self._with_timeout("list_tasks", self._runtime.list_tasks(agent_id, limit))

    # ---------- 状态 ----------

    def get_presence(self) -> PresenceSnapshot:
        """同步读取内存快照；采集关闭时返回 unknown 快照。"""
        return self._presence.snapshot()

    async def set_presence_visibility(self, fields: Sequence[PresenceField]) -> None:
        self._presence.set_visibility(fields)

    async def report_presence(self, signal: PresenceInput) -> PresenceSnapshot:
        """外部通道上报在线/输入/设备状态；与用户消息入口分离。"""
        return await self._terminal.report_presence(signal)

    async def get_link_state(self) -> LinkState:
        return self._terminal.link_state()

    async def get_world_view(self, force: bool = False) -> WorldInference | None:
        if force:
            _ = await self._with_timeout("world", self._world.refresh(force=True))
            return self._world.view()
        return self._world.view()

    async def refresh_world(self) -> WorldInference | None:
        """显式刷新：绕过窗口，但允许复用相同输入的缓存。"""
        _ = await self._with_timeout("world", self._world.refresh(force=True, reuse=True))
        return self._world.view()

    async def set_aliya_context(self, ctx: AliyaContext) -> None:
        await self._world.set_aliya_context(ctx)

    # ---------- 消息 ----------

    async def deliver(self, text: str, *, session_id: str, kind: MessageKind = "chat") -> None:
        await self._with_timeout(
            "deliver", self._terminal.deliver(text, session_id=session_id, kind=kind)
        )

    async def notify(self, text: str, *, dispatch_id: str | None = None) -> ProactiveReceipt:
        return await self._with_timeout(
            "notify", self._terminal.notify(text, dispatch_id=dispatch_id)
        )

    async def send_proactive(self, text: str, *, urgency: float = 0.5) -> ProactiveReceipt:
        return await self._with_timeout(
            "send_proactive", self._terminal.send_proactive(text, urgency=urgency)
        )

    async def history(self, session_id: str, limit: int = 20) -> list[TerminalMessage]:
        return await self._terminal.history(session_id, limit)

    # ---------- 内部 ----------

    async def _with_timeout[T](self, name: str, awaitable: Awaitable[T]) -> T:
        """统一超时：超时抛 `CosmosError`，让 Aliya 按 `retriable` 决定是否重试。"""
        timeout = TIMEOUTS.get(name, 2.0)
        try:
            async with asyncio.timeout(timeout):
                return await awaitable
        except TimeoutError as exc:
            raise CosmosError(f"{name} 超时（{timeout}s）", code="timeout", retriable=True) from exc
