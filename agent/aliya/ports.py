"""Aliya 侧控制面端口：生产用 RemoteControl，测试用 InProcessControl。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Protocol

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
    PresenceField,
    PresenceSnapshot,
    ProactiveReceipt,
    TaskBrief,
    TaskHandle,
    TerminalMessage,
    WorldInference,
)
from agent.cosmos.services.control import ControlService


class ControlPort(Protocol):
    """Aliya 依赖的全部控制面能力；方法名与 ControlService 对齐。"""

    async def handshake(self) -> CosmosCapabilities: ...

    def inbound(self) -> AsyncIterator[InboundEnvelope]: ...

    async def ack_inbound(self, message_id: str) -> None: ...

    def subscribe(
        self,
        topics: Sequence[str],
        *,
        since_sequence: int | None = None,
        cursors: Mapping[str, int] | None = None,
    ) -> AsyncIterator[CosmosEvent]: ...

    async def describe_desktop(self) -> DesktopSnapshot: ...

    async def get_agent(self, agent_id: str) -> AgentDetail: ...

    async def open_agent(self, agent_id: str) -> AgentDetail: ...

    async def patch_layout(self, patch: DesktopLayoutPatch) -> DesktopSnapshot: ...

    async def plan(self, intent: DispatchIntent) -> DispatchPlan: ...

    async def invoke_agent(
        self, agent_id: str, params: dict[str, object], *, session_id: str
    ) -> TaskHandle: ...

    async def execute_plan(self, plan: DispatchPlan) -> DispatchHandle: ...

    async def cancel_task(self, task_id: str) -> None: ...

    async def cancel_dispatch(self, dispatch_id: str) -> None: ...

    async def retry_task(self, task_id: str) -> TaskHandle: ...

    async def list_tasks(self, agent_id: str | None = None, limit: int = 20) -> list[TaskBrief]: ...

    def get_presence(self) -> PresenceSnapshot: ...

    async def set_presence_visibility(self, fields: Sequence[PresenceField]) -> None: ...

    async def get_link_state(self) -> LinkState: ...

    async def get_world_view(self, force: bool = False) -> WorldInference | None: ...

    async def refresh_world(self) -> WorldInference | None: ...

    async def set_aliya_context(self, ctx: AliyaContext) -> None: ...

    async def deliver(self, text: str, *, session_id: str, kind: MessageKind = "chat") -> None: ...

    async def notify(self, text: str, *, dispatch_id: str | None = None) -> ProactiveReceipt: ...

    async def send_proactive(self, text: str, *, urgency: float = 0.5) -> ProactiveReceipt: ...

    async def history(self, session_id: str, limit: int = 20) -> list[TerminalMessage]: ...


class InProcessControl:
    """测试与同进程演练用：直接转给 ControlService。"""

    def __init__(self, control: ControlService) -> None:
        self._control = control

    async def handshake(self) -> CosmosCapabilities:
        return await self._control.handshake()

    def inbound(self) -> AsyncIterator[InboundEnvelope]:
        return self._control.inbound()

    async def ack_inbound(self, message_id: str) -> None:
        """同进程没有待确认队列，确认是空操作。"""

    def subscribe(
        self,
        topics: Sequence[str],
        *,
        since_sequence: int | None = None,
        cursors: Mapping[str, int] | None = None,
    ) -> AsyncIterator[CosmosEvent]:
        if since_sequence is None and cursors:
            since_sequence = min(cursors.values())
        return self._control.subscribe(topics, since_sequence=since_sequence)

    async def describe_desktop(self) -> DesktopSnapshot:
        return await self._control.describe_desktop()

    async def get_agent(self, agent_id: str) -> AgentDetail:
        return await self._control.get_agent(agent_id)

    async def open_agent(self, agent_id: str) -> AgentDetail:
        return await self._control.open_agent(agent_id)

    async def patch_layout(self, patch: DesktopLayoutPatch) -> DesktopSnapshot:
        return await self._control.patch_layout(patch)

    async def plan(self, intent: DispatchIntent) -> DispatchPlan:
        return await self._control.plan(intent)

    async def invoke_agent(
        self, agent_id: str, params: dict[str, object], *, session_id: str
    ) -> TaskHandle:
        return await self._control.invoke_agent(agent_id, params, session_id=session_id)

    async def execute_plan(self, plan: DispatchPlan) -> DispatchHandle:
        return await self._control.execute_plan(plan)

    async def cancel_task(self, task_id: str) -> None:
        await self._control.cancel_task(task_id)

    async def cancel_dispatch(self, dispatch_id: str) -> None:
        await self._control.cancel_dispatch(dispatch_id)

    async def retry_task(self, task_id: str) -> TaskHandle:
        return await self._control.retry_task(task_id)

    async def list_tasks(self, agent_id: str | None = None, limit: int = 20) -> list[TaskBrief]:
        return await self._control.list_tasks(agent_id, limit)

    def get_presence(self) -> PresenceSnapshot:
        return self._control.get_presence()

    async def set_presence_visibility(self, fields: Sequence[PresenceField]) -> None:
        await self._control.set_presence_visibility(fields)

    async def get_link_state(self) -> LinkState:
        return await self._control.get_link_state()

    async def get_world_view(self, force: bool = False) -> WorldInference | None:
        return await self._control.get_world_view(force=force)

    async def refresh_world(self) -> WorldInference | None:
        return await self._control.refresh_world()

    async def set_aliya_context(self, ctx: AliyaContext) -> None:
        await self._control.set_aliya_context(ctx)

    async def deliver(self, text: str, *, session_id: str, kind: MessageKind = "chat") -> None:
        await self._control.deliver(text, session_id=session_id, kind=kind)

    async def notify(self, text: str, *, dispatch_id: str | None = None) -> ProactiveReceipt:
        return await self._control.notify(text, dispatch_id=dispatch_id)

    async def send_proactive(self, text: str, *, urgency: float = 0.5) -> ProactiveReceipt:
        return await self._control.send_proactive(text, urgency=urgency)

    async def history(self, session_id: str, limit: int = 20) -> list[TerminalMessage]:
        return await self._control.history(session_id, limit)
