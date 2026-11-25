"""控制面服务端：把 ControlService 暴露成 UDS + NDJSON（设计 6 章）。"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections import OrderedDict
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel

from agent.cosmos.contracts import (
    AliyaContext,
    CosmosEvent,
    DesktopLayoutPatch,
    DispatchIntent,
    DispatchPlan,
)
from agent.cosmos.contracts.rpc import (
    AckInbound,
    Event,
    Hello,
    HelloOk,
    Inbound,
    RpcError,
    RpcRequest,
    RpcResponse,
    Subscribe,
    Subscribed,
    parse_rpc_line,
)
from agent.cosmos.errors import CosmosError
from agent.cosmos.services.control import EVENT_TOPICS, ControlService
from core.config import AgentSettings
from core.service import ClockService, Service

#: 允许客户端调用的方法白名单（设计 6.3）。
_METHODS = frozenset(
    {
        "describe_desktop",
        "get_agent",
        "open_agent",
        "patch_layout",
        "plan",
        "invoke_agent",
        "execute_plan",
        "cancel_task",
        "cancel_dispatch",
        "retry_task",
        "list_tasks",
        "get_presence",
        "set_presence_visibility",
        "get_link_state",
        "get_world_view",
        "refresh_world",
        "set_aliya_context",
        "deliver",
        "notify",
        "send_proactive",
        "history",
    }
)

#: 出站队列满时可以丢弃的低优先级事件。
_LOW_PRIORITY = frozenset({"presence.changed", "agent.progress"})


class ControlSession:
    """一个已连接客户端的读、写与待确认入站消息。"""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        outbox_limit: int,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.pending: set[str] = set()
        self.events: asyncio.Queue[CosmosEvent] = asyncio.Queue(maxsize=outbox_limit)
        self.subscribed = asyncio.Event()
        self.topics: dict[str, int] = {}
        self.tasks: list[asyncio.Task[None]] = []
        self.closed = False


class ControlServerService(Service):
    """单客户端 UDS 服务端：握手、RPC、推送。"""

    name: ClassVar[str] = "cosmos.control_server"
    dependencies: ClassVar[tuple[type[Service], ...]] = (ControlService, ClockService)

    def __init__(
        self,
        control: ControlService,
        clock: ClockService,
        config: AgentSettings,
    ) -> None:
        super().__init__()
        self._control: ControlService = control
        self._clock: ClockService = clock
        self._config = config.control
        self._server: asyncio.AbstractServer | None = None
        self._sessions: set[ControlSession] = set()
        self._dedupe: OrderedDict[str, object] = OrderedDict()
        self._socket_path = Path(self._config.socket_path)

    async def start(self) -> None:
        if self._server is not None:
            return
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self._socket_path.exists():
            if await self._is_listening(self._socket_path):
                msg = f"控制面 socket 已被占用：{self._socket_path}"
                raise RuntimeError(msg)
            self._socket_path.unlink()
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(self._socket_path)
        )
        os.chmod(self._socket_path, 0o600)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for session in list(self._sessions):
            await self._cancel_tasks(session)
            await self._close(session)
        self._socket_path.unlink(missing_ok=True)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if len(self._sessions) >= self._config.max_clients:
            await self._send(
                writer,
                RpcResponse(
                    type="error",
                    id="",
                    ok=False,
                    error=RpcError(code="busy", message="已有客户端连接", retriable=True),
                ),
            )
            writer.close()
            await writer.wait_closed()
            return
        session = ControlSession(reader, writer, self._config.outbox_limit)
        self._sessions.add(session)
        try:
            hello = parse_rpc_line((await reader.readline()).decode())
            if not isinstance(hello, Hello) or hello.token != self._token():
                await self._send(
                    writer,
                    RpcResponse(
                        type="error",
                        id="",
                        ok=False,
                        error=RpcError(code="unauthorized", message="令牌不匹配"),
                    ),
                )
                return
            caps = await self._control.handshake()
            await self._send(
                writer,
                HelloOk(
                    capabilities=caps.model_dump(mode="json"),
                    server_time=self._clock.now(),
                    timezone=str(self._clock.now().tzinfo),
                ),
            )
            session.tasks = [
                asyncio.create_task(self._pump_inbound(session)),
                asyncio.create_task(self._pump_events(session)),
                asyncio.create_task(self._send_events(session)),
            ]
            await self._serve(session)
        finally:
            self._sessions.discard(session)
            await self._cancel_tasks(session)
            await self._close(session)

    async def _serve(self, session: ControlSession) -> None:
        while not session.closed:
            line = await session.reader.readline()
            if not line:
                return
            await self._dispatch(session, line.decode())

    async def _dispatch(self, session: ControlSession, line: str) -> None:
        message = parse_rpc_line(line)
        if isinstance(message, Subscribe):
            session.topics = dict(message.topics)
            session.subscribed.set()
            await self._send(session.writer, Subscribed(topics=message.topics))
            return
        if isinstance(message, AckInbound):
            session.pending.discard(message.message_id)
            return
        if isinstance(message, RpcRequest):
            await self._handle_request(session, message)

    async def _handle_request(self, session: ControlSession, message: RpcRequest) -> None:
        if message.method == "now":
            await self._send(
                session.writer,
                RpcResponse(
                    id=message.id,
                    result={
                        "server_time": self._clock.now().isoformat(),
                        "timezone": str(self._clock.now().tzinfo),
                    },
                ),
            )
            return
        if message.method not in _METHODS:
            await self._send(
                session.writer,
                RpcResponse(
                    id=message.id,
                    ok=False,
                    type="error",
                    error=RpcError(code="not_implemented", message=message.method),
                ),
            )
            return

        client_msg_id = message.params.pop("client_msg_id", None)
        if isinstance(client_msg_id, str) and client_msg_id in self._dedupe:
            await self._send(
                session.writer,
                RpcResponse(id=message.id, result=self._dedupe[client_msg_id]),
            )
            return

        try:
            result = await asyncio.wait_for(
                self._call(message.method, message.params),
                timeout=self._config.request_timeout,
            )
        except TimeoutError:
            await self._send(
                session.writer,
                RpcResponse(
                    id=message.id,
                    ok=False,
                    type="error",
                    error=RpcError(code="timeout", message="请求超时", retriable=True),
                ),
            )
            return
        except CosmosError as exc:
            await self._send(
                session.writer,
                RpcResponse(
                    id=message.id,
                    ok=False,
                    type="error",
                    error=RpcError(code=exc.code, message=str(exc), retriable=exc.retriable),
                ),
            )
            return
        except Exception as exc:
            await self._send(
                session.writer,
                RpcResponse(
                    id=message.id,
                    ok=False,
                    type="error",
                    error=RpcError(code="internal", message=str(exc)),
                ),
            )
            return

        payload = result.model_dump(mode="json") if isinstance(result, BaseModel) else result
        if isinstance(client_msg_id, str):
            self._dedupe[client_msg_id] = payload
            self._trim_dedupe()
        await self._send(session.writer, RpcResponse(id=message.id, result=payload))

    async def _call(self, method: str, params: dict[str, object]) -> object:
        control = self._control
        if method == "plan":
            return await control.plan(DispatchIntent.model_validate(params["intent"]))
        if method == "execute_plan":
            return await control.execute_plan(DispatchPlan.model_validate(params["plan"]))
        if method == "invoke_agent":
            raw_params = params.get("params", {})
            return await control.invoke_agent(
                str(params["agent_id"]),
                dict(raw_params),  # type: ignore[arg-type]
            )
        if method == "patch_layout":
            return await control.patch_layout(DesktopLayoutPatch.model_validate(params["patch"]))
        if method == "set_aliya_context":
            return await control.set_aliya_context(AliyaContext.model_validate(params["context"]))
        return await getattr(control, method)(**params)

    def _trim_dedupe(self) -> None:
        while len(self._dedupe) > 500:
            self._dedupe.popitem(last=False)

    async def _pump_inbound(self, session: ControlSession) -> None:
        async for envelope in self._control.inbound():
            session.pending.add(envelope.message.message_id)
            await self._send(
                session.writer,
                Inbound(envelope=envelope.model_dump(mode="json")),
            )

    async def _pump_events(self, session: ControlSession) -> None:
        await session.subscribed.wait()
        topics = list(session.topics) or list(EVENT_TOPICS)
        since = min(session.topics.values()) if session.topics else None
        async for event in self._control.subscribe(topics, since_sequence=since):
            try:
                session.events.put_nowait(event)
            except asyncio.QueueFull:
                if event.topic in _LOW_PRIORITY:
                    continue
                await session.events.put(event)

    async def _send_events(self, session: ControlSession) -> None:
        while True:
            event = await session.events.get()
            await self._send(session.writer, Event(event=event.model_dump(mode="json")))

    @property
    def pending_count(self) -> int:
        return sum(len(session.pending) for session in self._sessions)

    async def _send(self, writer: asyncio.StreamWriter, message: BaseModel) -> None:
        writer.write(message.model_dump_json().encode() + b"\n")
        await writer.drain()

    async def _cancel_tasks(self, session: ControlSession) -> None:
        for task in session.tasks:
            task.cancel()
        if session.tasks:
            await asyncio.gather(*session.tasks, return_exceptions=True)
        session.tasks.clear()

    async def _close(self, session: ControlSession) -> None:
        if session.closed:
            return
        session.closed = True
        session.writer.close()
        with contextlib.suppress(ConnectionResetError, TimeoutError):
            await asyncio.wait_for(session.writer.wait_closed(), timeout=1.0)

    def _token(self) -> str:
        return os.environ.get(self._config.token_env, "")

    @staticmethod
    async def _is_listening(path: Path) -> bool:
        try:
            _reader, writer = await asyncio.open_unix_connection(path)
        except OSError:
            return False
        writer.close()
        await writer.wait_closed()
        return True
