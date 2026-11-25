"""控制面服务端：把 ControlService 暴露成 UDS + NDJSON（设计 6 章）。"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import time
from collections import OrderedDict
from pathlib import Path
from typing import ClassVar, cast, override

from pydantic import BaseModel

from agent.cosmos.contracts import (
    AliyaContext,
    CosmosEvent,
    DesktopLayoutPatch,
    DispatchIntent,
    DispatchPlan,
    InboundEnvelope,
)
from agent.cosmos.contracts.rpc import (
    AckInbound,
    Event,
    Hello,
    HelloOk,
    Inbound,
    Ping,
    Pong,
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
from core.config.settings import AgentControlSettings
from core.logger import log
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
        self.reader: asyncio.StreamReader = reader
        self.writer: asyncio.StreamWriter = writer
        self.pending: set[str] = set()
        self.events: asyncio.Queue[CosmosEvent] = asyncio.Queue(maxsize=outbox_limit)
        self.subscribed: asyncio.Event = asyncio.Event()
        self.topics: dict[str, int] = {}
        self.tasks: list[asyncio.Task[None]] = []
        self.closed: bool = False


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
        self._config: AgentControlSettings = config.control
        self._server: asyncio.AbstractServer | None = None
        self._sessions: set[ControlSession] = set()
        self._dedupe: OrderedDict[str, object] = OrderedDict()
        self._pending_inbound: OrderedDict[str, InboundEnvelope] = OrderedDict()
        self._socket_path: Path = Path(self._config.socket_path)

    @override
    async def start(self) -> None:
        if self._server is not None:
            return
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self._socket_path.exists():
            if await self._is_listening(self._socket_path):
                log.error("控制面 socket 已被占用", 路径=str(self._socket_path))
                msg = f"控制面 socket 已被占用：{self._socket_path}"
                raise RuntimeError(msg)
            log.warning("清理残留的控制面 socket", 路径=str(self._socket_path))
            self._socket_path.unlink()
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(self._socket_path)
        )
        os.chmod(self._socket_path, 0o600)
        log.info(
            "控制面服务端已启动",
            路径=str(self._socket_path),
            最大客户端数=self._config.max_clients,
            请求超时秒=self._config.request_timeout,
            出站队列上限=self._config.outbox_limit,
        )

    @override
    async def stop(self) -> None:
        sessions = len(self._sessions)
        running = self._server is not None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for session in list(self._sessions):
            await self._cancel_tasks(session)
            await self._close(session)
        self._socket_path.unlink(missing_ok=True)
        if running or sessions:
            log.info("控制面服务端已停止", 关闭会话数=sessions)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if len(self._sessions) >= self._config.max_clients:
            log.warning(
                "控制面客户端数已达上限，拒绝连接",
                上限=self._config.max_clients,
                当前=len(self._sessions),
            )
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
            if not isinstance(hello, Hello):
                log.warning("控制面握手失败：报文不是 Hello", 会话数=len(self._sessions))
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
            if hello.token != self._token():
                log.warning("控制面握手失败：令牌不匹配", 会话数=len(self._sessions))
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
            log.info(
                "控制面客户端已连接",
                客户端=hello.client_id,
                合约版本=hello.contract_version,
                会话数=len(self._sessions),
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
            log.info("控制面客户端已断开", 剩余会话数=len(self._sessions))

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
            log.info(
                "客户端已订阅事件",
                主题数=len(message.topics),
                主题="、".join(sorted(message.topics)),
            )
            return
        if isinstance(message, AckInbound):
            session.pending.discard(message.message_id)
            _ = self._pending_inbound.pop(message.message_id, None)
            log.debug(
                "客户端已确认入站消息",
                消息=message.message_id,
                未确认数=len(session.pending),
            )
            return
        if isinstance(message, Ping):
            await self._send(session.writer, Pong(at=message.at))
            log.debug("客户端心跳", 时间=message.at.isoformat())
            return
        if isinstance(message, RpcRequest):
            await self._handle_request(session, message)
            return
        log.debug("忽略无法识别的控制面报文", 类型=type(message).__name__)

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
            log.warning("客户端调用了未实现的方法", 方法=message.method, 请求=message.id)
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

        client_msg_id = cast("object", message.params.pop("client_msg_id", None))
        if isinstance(client_msg_id, str) and client_msg_id in self._dedupe:
            log.debug("命中请求去重，直接返回上次结果", 方法=message.method, 请求=message.id)
            await self._send(
                session.writer,
                RpcResponse(id=message.id, result=self._dedupe[client_msg_id]),
            )
            return

        started = time.perf_counter()
        try:
            result = await asyncio.wait_for(
                self._call(message.method, message.params),
                timeout=self._config.request_timeout,
            )
        except TimeoutError:
            log.warning(
                "RPC 请求超时",
                方法=message.method,
                请求=message.id,
                超时秒=self._config.request_timeout,
                耗时毫秒=round((time.perf_counter() - started) * 1000),
            )
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
            log.warning(
                "RPC 请求被业务规则拒绝",
                方法=message.method,
                请求=message.id,
                错误码=exc.code,
                错误消息=str(exc),
                可重试=exc.retriable,
            )
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
            log.exception(
                "RPC 请求内部错误",
                方法=message.method,
                请求=message.id,
                错误类型=type(exc).__name__,
            )
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
        log.debug(
            "RPC 请求完成",
            方法=message.method,
            请求=message.id,
            耗时毫秒=round((time.perf_counter() - started) * 1000),
        )
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
                cast("dict[str, object]", raw_params),
                session_id=str(params["session_id"]),
            )
        if method == "patch_layout":
            return await control.patch_layout(DesktopLayoutPatch.model_validate(params["patch"]))
        if method == "set_aliya_context":
            return await control.set_aliya_context(AliyaContext.model_validate(params["context"]))
        result = cast("object", getattr(control, method)(**params))
        # ControlService 里存在同步方法（如 get_presence），统一在这里兼容。
        if inspect.isawaitable(result):
            return cast("object", await result)
        return result

    def _trim_dedupe(self) -> None:
        while len(self._dedupe) > 500:
            _ = self._dedupe.popitem(last=False)

    async def _pump_inbound(self, session: ControlSession) -> None:
        # 重连先补发未确认的入站消息；客户端按 message_id 去重，保证至少一次。
        if self._pending_inbound:
            log.info("重放未确认的入站消息", 数量=len(self._pending_inbound))
        for envelope in list(self._pending_inbound.values()):
            session.pending.add(envelope.message.message_id)
            await self._send(
                session.writer,
                Inbound(envelope=envelope.model_dump(mode="json")),
            )
        async for envelope in self._control.inbound():
            message_id = envelope.message.message_id
            self._pending_inbound[message_id] = envelope
            session.pending.add(message_id)
            log.debug(
                "推送入站消息",
                消息=message_id,
                会话=envelope.message.session_id,
                字数=len(envelope.message.text),
            )
            await self._send(
                session.writer,
                Inbound(envelope=envelope.model_dump(mode="json")),
            )

    async def _pump_events(self, session: ControlSession) -> None:
        _ = await session.subscribed.wait()
        topics = list(session.topics) or list(EVENT_TOPICS)
        cursors = dict(session.topics) if session.topics else None
        since = min(cursors.values()) if cursors else None
        async for event in self._control.subscribe(
            topics, since_sequence=since, cursors=cursors
        ):
            try:
                session.events.put_nowait(event)
            except asyncio.QueueFull:
                if event.topic in _LOW_PRIORITY:
                    log.debug("出站队列已满，丢弃低优先级事件", 主题=event.topic)
                    continue
                log.warning("出站队列已满，等待客户端消费", 主题=event.topic)
                await session.events.put(event)

    async def _send_events(self, session: ControlSession) -> None:
        while True:
            event = await session.events.get()
            await self._send(session.writer, Event(event=event.model_dump(mode="json")))

    @property
    def pending_count(self) -> int:
        return len(self._pending_inbound)

    async def _send(self, writer: asyncio.StreamWriter, message: BaseModel) -> None:
        writer.write(message.model_dump_json().encode() + b"\n")
        await writer.drain()

    async def _cancel_tasks(self, session: ControlSession) -> None:
        for task in session.tasks:
            _ = task.cancel()
        if session.tasks:
            _ = await asyncio.gather(*session.tasks, return_exceptions=True)
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
