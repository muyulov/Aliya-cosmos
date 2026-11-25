"""控制面服务端：把 ControlService 暴露成 UDS + NDJSON（设计 6 章）。"""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel

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
from agent.cosmos.services.control import EVENT_TOPICS, ControlService
from core.config import AgentSettings
from core.service import ClockService, Service


class ControlSession:
    """一个已连接客户端的读、写与待确认入站消息。"""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.pending: set[str] = set()
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
        session = ControlSession(reader, writer)
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
            await self._send(session.writer, Subscribed(topics=message.topics))
            return
        if isinstance(message, AckInbound):
            session.pending.discard(message.message_id)
            return
        if isinstance(message, RpcRequest):
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
            await self._send(
                session.writer,
                RpcResponse(
                    id=message.id,
                    ok=False,
                    type="error",
                    error=RpcError(code="not_implemented", message=message.method),
                ),
            )

    async def _pump_inbound(self, session: ControlSession) -> None:
        async for envelope in self._control.inbound():
            session.pending.add(envelope.message.message_id)
            await self._send(
                session.writer,
                Inbound(envelope=envelope.model_dump(mode="json")),
            )

    async def _pump_events(self, session: ControlSession) -> None:
        async for event in self._control.subscribe(list(EVENT_TOPICS)):
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
