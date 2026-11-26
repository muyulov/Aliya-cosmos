"""浏览器侧 WebSocket 终端通道：协议、状态上报与安全边界。"""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import cast

import pytest
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from agent.cosmos.contracts import (
    InboundEnvelope,
    PresenceInput,
    PresenceSnapshot,
    TerminalMessage,
)
from agent.cosmos.services.terminal import TerminalService
from agent.cosmos.services.ws_channel import WebSocketChannelService, parse_message
from agent.cosmos.transport import MemoryUserChannel, UserChannelPort
from core.config import Settings


class FakeTerminal:
    """只覆盖通道用到的方法，避免拉起真实服务。"""

    def __init__(self) -> None:
        self.channel: UserChannelPort | None = None
        self.ingested: list[TerminalMessage] = []
        self.presence: list[PresenceInput] = []
        self.heartbeats: int = 0
        self.history_messages: list[TerminalMessage] = []

    def set_user_channel(self, channel: UserChannelPort) -> None:
        self.channel = channel

    async def ingest(self, message: TerminalMessage) -> InboundEnvelope | None:
        self.ingested.append(message)
        return None

    async def report_presence(self, signal: PresenceInput) -> PresenceSnapshot | None:
        self.presence.append(signal)
        return None

    async def note_link_heartbeat(self) -> None:
        self.heartbeats += 1

    async def history(self, session_id: str, limit: int = 20) -> list[TerminalMessage]:
        return [message for message in self.history_messages if message.session_id == session_id][
            -limit:
        ]


def as_terminal(fake: FakeTerminal) -> TerminalService:
    """测试替身只实现通道用到的方法，经 object 中转避免无效转换告警。"""
    return cast("TerminalService", cast("object", fake))


@pytest.fixture
def settings() -> Settings:
    resolved = Settings()
    resolved.agent.cosmos.terminal.ws.enabled = True
    resolved.agent.cosmos.terminal.ws.host = "127.0.0.1"
    resolved.agent.cosmos.terminal.ws.port = 0
    return resolved


@pytest.fixture
def fake_terminal() -> FakeTerminal:
    return FakeTerminal()


@pytest.fixture
async def channel_service(
    settings: Settings, fake_terminal: FakeTerminal
) -> AsyncIterator[WebSocketChannelService]:
    service = WebSocketChannelService(as_terminal(fake_terminal), settings.agent)
    await service.start()
    yield service
    await service.stop()


def ws_url(service: WebSocketChannelService, path: str = "/ws") -> str:
    address = service.address
    assert address is not None
    return f"ws://{address[0]}:{address[1]}{path}"


async def connect_client(service: WebSocketChannelService, path: str = "/ws") -> ClientConnection:
    return await connect(ws_url(service, path), proxy=None)


async def recv_json(connection: ClientConnection) -> dict[str, object]:
    parsed = parse_message(await asyncio.wait_for(connection.recv(), timeout=2))
    assert parsed is not None
    return parsed


async def hello(connection: ClientConnection, session_id: str = "web") -> dict[str, object]:
    await connection.send(json.dumps({"type": "hello", "session_id": session_id}))
    return await recv_json(connection)


async def test_message_round_trip(
    channel_service: WebSocketChannelService, fake_terminal: FakeTerminal
) -> None:
    connection = await connect_client(channel_service)
    try:
        ready = await hello(connection)
        assert ready["type"] == "ready"
        assert ready["session_id"] == "web"

        await connection.send(json.dumps({"type": "message", "text": "在吗"}))
        for _ in range(50):
            if fake_terminal.ingested:
                break
            await asyncio.sleep(0.01)
        assert len(fake_terminal.ingested) == 1
        message = fake_terminal.ingested[0]
        assert message.text == "在吗"
        assert message.session_id == "web"
        assert message.direction == "user_to_aliya"
        assert message.kind == "chat"
        assert message.meta.channel == "websocket"
        assert message.meta.device == "web"

        await channel_service.send(
            TerminalMessage(
                direction="aliya_to_user",
                kind="chat",
                text="在啊",
                session_id="web",
            )
        )
        frame = await recv_json(connection)
        assert frame["type"] == "message"
        pushed = cast("dict[str, object]", frame["message"])
        assert pushed["text"] == "在啊"
    finally:
        await connection.close()


async def test_typing_reports_presence(
    channel_service: WebSocketChannelService, fake_terminal: FakeTerminal
) -> None:
    connection = await connect_client(channel_service)
    try:
        _ = await hello(connection)
        await connection.send(json.dumps({"type": "typing", "typing": True}))
        for _ in range(50):
            if fake_terminal.presence:
                break
            await asyncio.sleep(0.01)
        assert len(fake_terminal.presence) == 1
        signal = fake_terminal.presence[0]
        assert signal.is_typing is True
        assert signal.device == "web"
        assert signal.online == "online"
    finally:
        await connection.close()


async def test_ping_gets_pong(
    channel_service: WebSocketChannelService, fake_terminal: FakeTerminal
) -> None:
    connection = await connect_client(channel_service)
    try:
        _ = await hello(connection)
        await connection.send(json.dumps({"type": "ping", "at": "2026-01-01T00:00:00Z"}))
        frame = await recv_json(connection)
        assert frame == {"type": "pong", "at": "2026-01-01T00:00:00Z"}
        assert fake_terminal.heartbeats >= 2
    finally:
        await connection.close()


async def test_history_is_replayed_after_ready(
    channel_service: WebSocketChannelService, fake_terminal: FakeTerminal
) -> None:
    fake_terminal.history_messages = [
        TerminalMessage(
            message_id="m1",
            direction="user_to_aliya",
            kind="chat",
            text="上一句",
            at=datetime.now(UTC),
            session_id="web",
        )
    ]
    connection = await connect_client(channel_service)
    try:
        ready = await hello(connection)
        assert ready["type"] == "ready"
        frame = await recv_json(connection)
        assert frame["type"] == "history"
        messages = cast("list[dict[str, object]]", frame["messages"])
        assert messages[0]["text"] == "上一句"
    finally:
        await connection.close()


async def test_test_page_is_served(channel_service: WebSocketChannelService) -> None:
    address = channel_service.address
    assert address is not None
    reader, writer = await asyncio.open_connection(*address)
    try:
        writer.write(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        await writer.drain()
        response = (await asyncio.wait_for(reader.read(), timeout=3)).decode("utf-8", "replace")
    finally:
        writer.close()
        await writer.wait_closed()
    head, _, body = response.partition("\r\n\r\n")
    assert head.startswith("HTTP/1.1 200")
    assert "text/html" in head
    assert "WebSocket 终端测试台" in body


async def test_query_string_is_ignored(channel_service: WebSocketChannelService) -> None:
    connection = await connect_client(channel_service, "/ws?from=browser")
    try:
        ready = await hello(connection)
        assert ready["type"] == "ready"
    finally:
        await connection.close()


async def test_unknown_path_is_closed(channel_service: WebSocketChannelService) -> None:
    connection = await connect_client(channel_service, "/wrong")
    try:
        with pytest.raises(ConnectionClosed) as error:
            _ = await asyncio.wait_for(connection.recv(), timeout=2)
        received = error.value.rcvd
        assert received is not None
        assert received.code == 1008
    finally:
        await connection.close()


async def test_token_is_required_when_configured(
    monkeypatch: pytest.MonkeyPatch, fake_terminal: FakeTerminal
) -> None:
    monkeypatch.setenv("WS_TEST_TOKEN", "s3cret")
    settings = Settings()
    settings.agent.cosmos.terminal.ws.enabled = True
    settings.agent.cosmos.terminal.ws.host = "127.0.0.1"
    settings.agent.cosmos.terminal.ws.port = 0
    settings.agent.cosmos.terminal.ws.token_env = "WS_TEST_TOKEN"
    service = WebSocketChannelService(as_terminal(fake_terminal), settings.agent)
    await service.start()
    try:
        connection = await connect_client(service)
        try:
            await connection.send(
                json.dumps({"type": "hello", "session_id": "web", "token": "wrong"})
            )
            frame = await recv_json(connection)
            assert frame["type"] == "error"
            assert frame["code"] == "unauthorized"
            with pytest.raises(ConnectionClosed):
                _ = await asyncio.wait_for(connection.recv(), timeout=2)
        finally:
            await connection.close()
    finally:
        await service.stop()


async def test_non_loopback_without_token_fails(fake_terminal: FakeTerminal) -> None:
    settings = Settings()
    settings.agent.cosmos.terminal.ws.enabled = True
    settings.agent.cosmos.terminal.ws.host = "0.0.0.0"
    settings.agent.cosmos.terminal.ws.port = 0
    service = WebSocketChannelService(as_terminal(fake_terminal), settings.agent)
    with pytest.raises(RuntimeError):
        await service.start()
    assert fake_terminal.channel is None


async def test_stop_restores_fallback_channel(
    settings: Settings, fake_terminal: FakeTerminal
) -> None:
    service = WebSocketChannelService(as_terminal(fake_terminal), settings.agent)
    await service.start()
    assert fake_terminal.channel is service
    await service.stop()
    assert isinstance(fake_terminal.channel, MemoryUserChannel)


async def test_message_without_hello_is_closed(channel_service: WebSocketChannelService) -> None:
    connection = await connect_client(channel_service)
    try:
        await connection.send(json.dumps({"type": "message", "text": "在吗"}))
        frame = await recv_json(connection)
        assert frame["type"] == "error"
        assert frame["code"] == "invalid_hello"
    finally:
        await connection.close()


async def test_invalid_text_returns_error(
    channel_service: WebSocketChannelService, fake_terminal: FakeTerminal
) -> None:
    connection = await connect_client(channel_service)
    try:
        _ = await hello(connection)
        await connection.send(json.dumps({"type": "message", "text": "   "}))
        frame = await recv_json(connection)
        assert frame["type"] == "error"
        assert frame["code"] == "invalid_text"
        assert fake_terminal.ingested == []
    finally:
        await connection.close()
