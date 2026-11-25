"""Aliya 侧 UDS + NDJSON 控制面客户端（设计 6.4）。"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections import deque
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime

from pydantic import BaseModel

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
    PresenceSnapshot,
    ProactiveReceipt,
    ReadablePresence,
    TaskBrief,
    TaskHandle,
    TerminalMessage,
    WorldInference,
)
from agent.cosmos.contracts.rpc import (
    AckInbound,
    Event,
    Hello,
    HelloOk,
    Inbound,
    RpcRequest,
    RpcResponse,
    Subscribe,
    parse_rpc_line,
)

CONTRACT_VERSION = "1.0"


class RemoteError(Exception):
    """控制面返回的业务错误。"""

    def __init__(self, code: str, message: str, *, retriable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retriable = retriable


class RemoteControl:
    """生产环境客户端：一条连接、一个读循环、一个有界 outbox。"""

    def __init__(
        self,
        socket_path: str,
        *,
        token: str,
        request_timeout: float = 15.0,
        outbox_limit: int = 200,
    ) -> None:
        self._socket_path = socket_path
        self._token = token
        self._timeout = request_timeout
        self._outbox: deque[tuple[str, str, dict[str, object]]] = deque(
            maxlen=outbox_limit
        )
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[str, asyncio.Future[object]] = {}
        self._inbound: asyncio.Queue[InboundEnvelope] = asyncio.Queue()
        self._events: asyncio.Queue[CosmosEvent] = asyncio.Queue()
        self._presence: PresenceSnapshot | None = None
        self._capabilities: CosmosCapabilities | None = None
        self._topics: set[str] = set()
        self._since: int | None = None
        self._connected = False
        self._closed = False
        self._backoff_seconds = 1.0
        self._read_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None

    # ---------- 连接 ----------

    async def connect(self) -> None:
        if self._connected:
            return
        while not self._closed:
            try:
                reader, writer = await asyncio.open_unix_connection(self._socket_path)
            except OSError:
                await self._sleep_backoff()
                continue
            try:
                capabilities = await self._hello(reader, writer)
            except (ConnectionError, OSError, ValueError):
                writer.close()
                with contextlib.suppress(ConnectionResetError):
                    await writer.wait_closed()
                await self._sleep_backoff()
                continue
            self._reader, self._writer = reader, writer
            self._capabilities = capabilities
            self._connected = True
            self._backoff_seconds = 1.0
            self._read_task = asyncio.create_task(self._read_loop())
            await self._resubscribe()
            await self._replay_outbox()
            return

    async def close(self) -> None:
        self._closed = True
        if self._read_task is not None:
            self._read_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._read_task
            self._read_task = None
        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reconnect_task
            self._reconnect_task = None
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(ConnectionResetError, TimeoutError):
                await asyncio.wait_for(self._writer.wait_closed(), timeout=1.0)
            self._writer = None
        self._connected = False

    async def handshake(self) -> CosmosCapabilities:
        if self._capabilities is None:
            await self.connect()
        if self._capabilities is None:
            raise ConnectionError("控制面尚未握手")
        return self._capabilities

    async def call(self, method: str, params: dict[str, object]) -> object:
        if not self._connected:
            await self.connect()
        request_id = uuid.uuid4().hex
        future: asyncio.Future[object] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send(RpcRequest(id=request_id, method=method, params=params))
            return await asyncio.wait_for(future, timeout=self._timeout)
        finally:
            self._pending.pop(request_id, None)

    async def _hello(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> CosmosCapabilities:
        writer.write(
            Hello(
                token=self._token,
                contract_version=CONTRACT_VERSION,
                client_id="aliya",
            )
            .model_dump_json()
            .encode()
            + b"\n"
        )
        await writer.drain()
        line = await reader.readline()
        if not line:
            raise ConnectionError("控制面在握手前断开")
        message = parse_rpc_line(line.decode())
        if isinstance(message, RpcResponse) and message.error is not None:
            raise RemoteError(
                message.error.code,
                message.error.message,
                retriable=message.error.retriable,
            )
        if not isinstance(message, HelloOk):
            raise ConnectionError("控制面握手响应非法")
        return CosmosCapabilities.model_validate(message.capabilities)

    async def _read_loop(self) -> None:
        reader = self._reader
        if reader is None:
            return
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                self._handle_line(line.decode())
        finally:
            self._connected = False
            self._fail_pending()
            if not self._closed:
                self._reconnect_task = asyncio.create_task(self._reconnect())

    def _handle_line(self, line: str) -> None:
        message = parse_rpc_line(line)
        if isinstance(message, RpcResponse):
            future = self._pending.pop(message.id, None)
            if future is None or future.done():
                return
            if message.ok:
                future.set_result(message.result)
            elif message.error is not None:
                future.set_exception(
                    RemoteError(
                        message.error.code,
                        message.error.message,
                        retriable=message.error.retriable,
                    )
                )
            else:
                future.set_exception(RemoteError("unknown", "控制面返回错误"))
        elif isinstance(message, Inbound):
            self._inbound.put_nowait(InboundEnvelope.model_validate(message.envelope))
        elif isinstance(message, Event):
            event = CosmosEvent.model_validate(message.event)
            if event.topic == "presence.changed":
                changed = PresenceChanged.model_validate(event.payload)
                self._presence = changed.snapshot
            self._events.put_nowait(event)

    def _fail_pending(self) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError("控制面连接断开"))
                # 标记异常已读取，避免无人 await 时刷 "never retrieved"。
                _ = future.exception()
        self._pending.clear()

    async def _reconnect(self) -> None:
        if self._closed:
            return
        await self.connect()

    async def _sleep_backoff(self) -> None:
        await asyncio.sleep(self._backoff_seconds)
        self._backoff_seconds = min(self._backoff_seconds * 2, 30)

    async def _send(self, message: BaseModel) -> None:
        if self._writer is None:
            raise ConnectionError("控制面未连接")
        self._writer.write(message.model_dump_json().encode() + b"\n")
        await self._writer.drain()

    async def _resubscribe(self) -> None:
        if not self._topics:
            return
        await self._send(
            Subscribe(topics={topic: self._since or 0 for topic in self._topics})
        )

    async def _replay_outbox(self) -> None:
        for client_msg_id, method, params in list(self._outbox):
            try:
                await self.call(method, params)
            except (RemoteError, ConnectionError, TimeoutError):
                return
            self._remove_outbox(client_msg_id)

    def _remove_outbox(self, client_msg_id: str) -> None:
        self._outbox = deque(
            (item for item in self._outbox if item[0] != client_msg_id),
            maxlen=self._outbox.maxlen,
        )

    async def _call_replayable(
        self, method: str, params: dict[str, object]
    ) -> object:
        client_msg_id = uuid.uuid4().hex
        payload = {**params, "client_msg_id": client_msg_id}
        self._outbox.append((client_msg_id, method, payload))
        result = await self.call(method, payload)
        self._remove_outbox(client_msg_id)
        return result

    # ---------- 流 ----------

    async def inbound(self) -> AsyncIterator[InboundEnvelope]:
        if not self._connected:
            await self.connect()
        while True:
            yield await self._inbound.get()

    async def ack_inbound(self, message_id: str) -> None:
        if self._connected:
            await self._send(AckInbound(message_id=message_id))

    async def subscribe(
        self, topics: Sequence[str], *, since_sequence: int | None = None
    ) -> AsyncIterator[CosmosEvent]:
        self._topics = set(topics)
        self._since = since_sequence
        if not self._connected:
            await self.connect()
        else:
            await self._resubscribe()
        while True:
            event = await self._events.get()
            if event.topic in self._topics:
                yield event

    # ---------- 桌面 ----------

    async def describe_desktop(self) -> DesktopSnapshot:
        return DesktopSnapshot.model_validate(await self.call("describe_desktop", {}))

    async def get_agent(self, agent_id: str) -> AgentDetail:
        return AgentDetail.model_validate(
            await self.call("get_agent", {"agent_id": agent_id})
        )

    async def open_agent(self, agent_id: str) -> AgentDetail:
        return AgentDetail.model_validate(
            await self.call("open_agent", {"agent_id": agent_id})
        )

    async def patch_layout(self, patch: DesktopLayoutPatch) -> DesktopSnapshot:
        return DesktopSnapshot.model_validate(
            await self.call("patch_layout", {"patch": patch.model_dump(mode="json")})
        )

    # ---------- 调度 ----------

    async def plan(self, intent: DispatchIntent) -> DispatchPlan:
        return DispatchPlan.model_validate(
            await self.call("plan", {"intent": intent.model_dump(mode="json")})
        )

    async def invoke_agent(
        self, agent_id: str, params: dict[str, object], *, session_id: str
    ) -> TaskHandle:
        return TaskHandle.model_validate(
            await self.call(
                "invoke_agent",
                {"agent_id": agent_id, "params": params, "session_id": session_id},
            )
        )

    async def execute_plan(self, plan: DispatchPlan) -> DispatchHandle:
        return DispatchHandle.model_validate(
            await self.call("execute_plan", {"plan": plan.model_dump(mode="json")})
        )

    async def cancel_task(self, task_id: str) -> None:
        await self.call("cancel_task", {"task_id": task_id})

    async def cancel_dispatch(self, dispatch_id: str) -> None:
        await self.call("cancel_dispatch", {"dispatch_id": dispatch_id})

    async def retry_task(self, task_id: str) -> TaskHandle:
        return TaskHandle.model_validate(
            await self.call("retry_task", {"task_id": task_id})
        )

    async def list_tasks(
        self, agent_id: str | None = None, limit: int = 20
    ) -> list[TaskBrief]:
        result = await self.call("list_tasks", {"agent_id": agent_id, "limit": limit})
        if not isinstance(result, list):
            raise RemoteError("invalid_response", "list_tasks 返回非列表")
        return [TaskBrief.model_validate(item) for item in result]

    # ---------- 状态 ----------

    def get_presence(self) -> PresenceSnapshot:
        if self._presence is not None:
            return self._presence
        return PresenceSnapshot(
            captured_at=datetime.now(UTC),
            readable=ReadablePresence(summary="暂时看不到状态", tone="unknown"),
        )

    async def set_presence_visibility(self, fields: Sequence[PresenceField]) -> None:
        await self.call("set_presence_visibility", {"fields": list(fields)})

    async def get_link_state(self) -> LinkState:
        return await self.call("get_link_state", {})  # type: ignore[return-value]

    async def get_world_view(self, force: bool = False) -> WorldInference | None:
        result = await self.call("get_world_view", {"force": force})
        return None if result is None else WorldInference.model_validate(result)

    async def refresh_world(self) -> WorldInference | None:
        result = await self.call("refresh_world", {})
        return None if result is None else WorldInference.model_validate(result)

    async def set_aliya_context(self, ctx: AliyaContext) -> None:
        await self.call(
            "set_aliya_context", {"context": ctx.model_dump(mode="json")}
        )

    # ---------- 消息 ----------

    async def deliver(
        self, text: str, *, session_id: str, kind: MessageKind = "chat"
    ) -> None:
        await self._call_replayable(
            "deliver", {"text": text, "session_id": session_id, "kind": kind}
        )

    async def notify(
        self, text: str, *, dispatch_id: str | None = None
    ) -> ProactiveReceipt:
        result = await self._call_replayable(
            "notify", {"text": text, "dispatch_id": dispatch_id}
        )
        return ProactiveReceipt.model_validate(result)

    async def send_proactive(self, text: str, *, urgency: float = 0.5) -> ProactiveReceipt:
        result = await self._call_replayable(
            "send_proactive", {"text": text, "urgency": urgency}
        )
        return ProactiveReceipt.model_validate(result)

    async def history(self, session_id: str, limit: int = 20) -> list[TerminalMessage]:
        result = await self.call("history", {"session_id": session_id, "limit": limit})
        if not isinstance(result, list):
            raise RemoteError("invalid_response", "history 返回非列表")
        return [TerminalMessage.model_validate(item) for item in result]
