"""浏览器/用户侧 WebSocket 终端通道。

`TerminalService` 只依赖 `UserChannelPort`：入站消息统一进 `ingest()`，出站消息由
`send()` 投递。本服务把浏览器接成这个端口，协议仍是 JSON（一个 WebSocket 帧一条报文）：

浏览器 → 服务端：
- `{"type": "hello", "session_id": "web", "token": "..."}`：首帧，令牌按配置可选
- `{"type": "message", "text": "...", "kind": "chat", "client_msg_id": "..."}`
- `{"type": "typing", "typing": true}`
- `{"type": "ping", "at": "..."}`

服务端 → 浏览器：
- `{"type": "ready", "protocol": "1.0", "session_id": "web"}`
- `{"type": "history", "messages": [...]}`：握手后补发最近对话；0 条时不发
- `{"type": "message", "message": {...TerminalMessage...}}`：`message.kind=system` 时按系统提示渲染
- `{"type": "pong", "at": "..."}`
- `{"type": "error", "code": "...", "message": "..."}`

安全边界：默认只监听回环地址；绑定非回环地址时必须通过 `token_env` 配置令牌，
否则拒绝启动。令牌在首帧校验，失败以关闭码 `1008` 断开。
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import ClassVar, cast, override

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from agent.cosmos.contracts import MessageKind, MessageMeta, PresenceInput, TerminalMessage
from agent.cosmos.services.terminal import TerminalService
from agent.cosmos.transport import MemoryUserChannel
from core.config import AgentSettings
from core.config.settings import AgentWebSocketChannelSettings
from core.service import Service

#: 首帧 hello 的等待上限（秒）
HELLO_TIMEOUT_SECONDS = 10.0
#: 浏览器允许声明的消息类型；进度与结果只能由 cosmos 产生
BROWSER_KINDS: frozenset[str] = frozenset({"chat", "query", "task"})
#: 允许免令牌监听的地址；其它地址视为对外暴露
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost"})
#: 自带测试页：与 WS 同端口托管，浏览器直接打开 http://<host>:<port>/
TEST_PAGE_PATH = Path(__file__).resolve().parent.parent / "web" / "ws-test.html"


class _HandshakeError(Exception):
    """握手失败：带机器可读的 code 与可展示的消息。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code: str = code
        self.message: str = message


@dataclass(eq=False)
class _Client:
    """一条已握手的浏览器连接。"""

    connection: ServerConnection
    session_id: str
    queue: asyncio.Queue[dict[str, object]]


class WebSocketChannelService(Service):
    """把浏览器接成用户通道：入站进 `TerminalService`，出站按会话推送。"""

    name: ClassVar[str] = "cosmos.ws_channel"
    dependencies: ClassVar[tuple[type[Service], ...]] = (TerminalService,)

    def __init__(self, terminal: TerminalService, config: AgentSettings) -> None:
        super().__init__()
        self._terminal: TerminalService = terminal
        self._config: AgentWebSocketChannelSettings = config.cosmos.terminal.ws
        #: 停止或未启用时的兜底通道，避免 TerminalService 持有已关闭的连接
        self._fallback: MemoryUserChannel = MemoryUserChannel(limit=config.cosmos.link.queue_limit)
        self._server: Server | None = None
        self._clients: set[_Client] = set()
        self._token: str = ""
        self._page: bytes = b""

    # ---------- 生命周期 ----------

    @override
    async def start(self) -> None:
        if self._server is not None or not self._config.enabled:
            return
        token = os.environ.get(self._config.token_env, "") if self._config.token_env else ""
        if not token and self._config.host not in LOOPBACK_HOSTS:
            self.log.error(
                "WebSocket 终端通道绑定非回环地址但未配置令牌，拒绝启动",
                地址=self._config.host,
                环境变量=self._config.token_env,
            )
            msg = "对外监听 WebSocket 终端通道必须配置令牌"
            raise RuntimeError(msg)
        self._token = token
        self._page = self._load_page()
        self._terminal.set_user_channel(self)
        try:
            self._server = await serve(
                self._handle_client,
                self._config.host,
                self._config.port,
                max_size=1_048_576,
                compression=None,
                close_timeout=2,
                process_request=self._process_request,
            )
        except Exception:
            # 绑定失败时把通道交还给兜底实现，服务回滚后仍可正常投递。
            self._terminal.set_user_channel(self._fallback)
            raise
        address = self.address
        port = address[1] if address is not None else self._config.port
        self.log.info(
            "WebSocket 终端通道已启动",
            地址=f"ws://{self._config.host}:{self._config.port}{self._config.path}",
            实际端口=port,
            测试页=f"http://{self._config.host}:{port}/",
            认证=bool(token),
            默认会话=self._config.default_session_id,
        )

    @override
    async def stop(self) -> None:
        server = self._server
        if server is None:
            return
        self._server = None
        # close() 会一并关闭已建立的连接，处理协程随之退出。
        server.close()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(server.wait_closed(), timeout=2.0)
        self._clients.clear()
        self._terminal.set_user_channel(self._fallback)
        self.log.info("WebSocket 终端通道已停止")

    @property
    def address(self) -> tuple[str, int] | None:
        """实际监听地址；未启用或尚未启动时返回 None。"""
        server = self._server
        if server is None or not server.sockets:
            return None
        address = cast("tuple[str, int]", server.sockets[0].getsockname())
        return address[0], address[1]

    def _load_page(self) -> bytes:
        try:
            return TEST_PAGE_PATH.read_bytes()
        except OSError as exc:
            self.log.warning("WebSocket 测试页读取失败", 路径=str(TEST_PAGE_PATH), 原因=str(exc))
            return b""

    async def _process_request(
        self, connection: ServerConnection, request: Request
    ) -> Response | None:
        """WS 升级交给连接处理；普通 GET 用它托管测试页。"""
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return None
        path = request.path.split("?", 1)[0]
        if path not in {"/", "/ws-test"}:
            return connection.respond(HTTPStatus.NOT_FOUND, "not found")
        if not self._page:
            return connection.respond(HTTPStatus.NOT_FOUND, "test page not found")
        return Response(
            HTTPStatus.OK,
            "OK",
            Headers(
                {
                    "Content-Type": "text/html; charset=utf-8",
                    "Content-Length": str(len(self._page)),
                    "Cache-Control": "no-store",
                    "Connection": "close",
                }
            ),
            self._page,
        )

    # ---------- 连接处理 ----------

    async def _handle_client(self, connection: ServerConnection) -> None:
        request = connection.request
        # `Request.path` 含查询串；只比较路径部分，查询参数不影响握手。
        path = request.path.split("?", 1)[0] if request is not None else ""
        if path != self._config.path:
            self.log.warning("WebSocket 终端通道路径不匹配，拒绝连接", 路径=path)
            await connection.close(code=1008, reason="unknown path")
            return
        try:
            session_id = await self._handshake(connection)
        except _HandshakeError as exc:
            await self._safe_send(
                connection,
                {"type": "error", "code": exc.code, "message": exc.message},
            )
            await connection.close(code=1008, reason=exc.code)
            return
        except (ConnectionClosed, TimeoutError, OSError):
            return

        client = _Client(
            connection=connection,
            session_id=session_id,
            queue=asyncio.Queue(maxsize=self._config.queue_limit),
        )
        self._clients.add(client)
        writer = asyncio.create_task(self._write_loop(client))
        self.log.info("WebSocket 客户端已连接", 会话=session_id, 客户端数=len(self._clients))
        try:
            await self._read_loop(client)
        except (ConnectionClosed, OSError):
            pass
        finally:
            self._clients.discard(client)
            _ = writer.cancel()
            _ = await asyncio.gather(writer, return_exceptions=True)
            with contextlib.suppress(ConnectionClosed, OSError):
                await connection.close()
            self.log.info(
                "WebSocket 客户端已断开",
                会话=session_id,
                剩余客户端=len(self._clients),
            )

    async def _handshake(self, connection: ServerConnection) -> str:
        raw = await asyncio.wait_for(connection.recv(), timeout=HELLO_TIMEOUT_SECONDS)
        data = parse_message(raw)
        if data is None or data.get("type") != "hello":
            raise _HandshakeError("invalid_hello", "首帧必须是 hello")
        if self._token:
            token = data.get("token")
            if not isinstance(token, str) or not hmac.compare_digest(
                token.encode("utf-8"), self._token.encode("utf-8")
            ):
                raise _HandshakeError("unauthorized", "令牌不匹配")
        session_id = clean_session_id(data.get("session_id"), self._config.default_session_id)
        await connection.send(
            json.dumps(
                {"type": "ready", "protocol": "1.0", "session_id": session_id},
                ensure_ascii=False,
            )
        )
        if self._config.history_limit:
            messages = await self._terminal.history(session_id, limit=self._config.history_limit)
            if messages:
                await connection.send(
                    json.dumps(
                        {
                            "type": "history",
                            "messages": [message.model_dump(mode="json") for message in messages],
                        },
                        ensure_ascii=False,
                    )
                )
        await self._terminal.note_link_heartbeat()
        return session_id

    async def _read_loop(self, client: _Client) -> None:
        async for raw in client.connection:
            data = parse_message(raw)
            if data is None:
                self._offer(
                    client,
                    {"type": "error", "code": "invalid_json", "message": "报文必须是 JSON 对象"},
                )
                continue
            message_type = data.get("type")
            if message_type == "message":
                await self._on_message(client, data)
            elif message_type == "typing":
                await self._on_typing(client, data)
            elif message_type == "ping":
                await self._on_ping(client, data)
            else:
                self._offer(
                    client,
                    {
                        "type": "error",
                        "code": "invalid_type",
                        "message": f"不支持的报文类型：{message_type}",
                    },
                )

    async def _on_message(self, client: _Client, data: dict[str, object]) -> None:
        text = data.get("text")
        if not isinstance(text, str) or not text.strip():
            self._offer(
                client,
                {"type": "error", "code": "invalid_text", "message": "消息文本不能为空"},
            )
            return
        kind = data.get("kind", "chat")
        if not isinstance(kind, str) or kind not in BROWSER_KINDS:
            self._offer(
                client,
                {"type": "error", "code": "invalid_kind", "message": f"不支持的消息类型：{kind}"},
            )
            return
        client_msg_id = data.get("client_msg_id")
        message = TerminalMessage(
            direction="user_to_aliya",
            kind=cast("MessageKind", kind),
            text=text,
            session_id=client.session_id,
            client_msg_id=client_msg_id
            if isinstance(client_msg_id, str) and client_msg_id
            else None,
            meta=MessageMeta(channel="websocket", device="web"),
        )
        _ = await self._terminal.ingest(message)

    async def _on_typing(self, client: _Client, data: dict[str, object]) -> None:
        is_typing = data.get("typing")
        if not isinstance(is_typing, bool):
            self._offer(
                client,
                {"type": "error", "code": "invalid_typing", "message": "typing 必须是布尔值"},
            )
            return
        _ = await self._terminal.report_presence(
            PresenceInput(is_typing=is_typing, device="web", online="online")
        )

    async def _on_ping(self, client: _Client, data: dict[str, object]) -> None:
        await self._terminal.note_link_heartbeat()
        self._offer(client, {"type": "pong", "at": data.get("at")})

    # ---------- 出站 ----------

    async def send(self, message: TerminalMessage) -> None:
        """按会话投递；没有匹配会话时广播给当前全部客户端。"""
        clients = list(self._clients)
        if not clients:
            self.log.debug(
                "暂无 WebSocket 客户端，消息不投递",
                会话=message.session_id,
                kind=message.kind,
            )
            return
        targets = [client for client in clients if client.session_id == message.session_id]
        payload: dict[str, object] = {
            "type": "message",
            "message": message.model_dump(mode="json"),
        }
        for client in targets or clients:
            self._offer(client, payload)

    async def _write_loop(self, client: _Client) -> None:
        while True:
            payload = await client.queue.get()
            await client.connection.send(json.dumps(payload, ensure_ascii=False))

    def _offer(self, client: _Client, payload: dict[str, object]) -> None:
        queue = client.queue
        if queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                _ = queue.get_nowait()
            self.log.warning(
                "出站队列已满，丢弃最旧消息",
                会话=client.session_id,
                上限=self._config.queue_limit,
            )
        queue.put_nowait(payload)

    async def _safe_send(self, connection: ServerConnection, payload: dict[str, object]) -> None:
        with contextlib.suppress(ConnectionClosed, OSError):
            await connection.send(json.dumps(payload, ensure_ascii=False))


def parse_message(raw: str | bytes) -> dict[str, object] | None:
    """把一帧解析成 JSON 对象；非对象或非法 JSON 返回 None。"""
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    else:
        text = raw
    try:
        payload = cast("object", json.loads(text))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return cast("dict[str, object]", payload)


def clean_session_id(value: object, default: str) -> str:
    """收敛会话标识：非空字符串截断到 64 字符，否则用默认值。"""
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned:
            return cleaned[:64]
    return default
