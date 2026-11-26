import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from agent.aliya.transport import RemoteControl, RemoteError
from agent.cosmos.contracts import (
    ConversationContext,
    CosmosEvent,
    InboundEnvelope,
    PresenceSnapshot,
    ReadablePresence,
    TerminalMessage,
)
from agent.cosmos.contracts.rpc import (
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


class FakeServer:
    def __init__(self, socket_path: Path, *, timezone: str = "UTC") -> None:
        self.socket_path = socket_path
        self.timezone = timezone
        self.delivered: list[str] = []
        self.subscriptions: list[dict[str, int]] = []
        #: 置位后下一次 deliver 返回业务错误，用于验证 outbox 不无限重放
        self.reject_deliver: bool = False
        self._seen: set[str] = set()
        self._server: asyncio.AbstractServer | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        self.socket_path.unlink(missing_ok=True)
        self._server = await asyncio.start_unix_server(self._handle, path=str(self.socket_path))

    async def stop(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.close()
        for writer in list(self._writers):
            writer.close()
            with contextlib.suppress(ConnectionResetError, TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
        if server is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(server.wait_closed(), timeout=2.0)
        self.socket_path.unlink(missing_ok=True)

    async def push_inbound(self, envelope: InboundEnvelope) -> None:
        for writer in list(self._writers):
            await self._send(
                writer,
                Inbound(envelope=envelope.model_dump(mode="json")),
            )

    async def push_event(self, event: CosmosEvent) -> None:
        for writer in list(self._writers):
            await self._send(writer, Event(event=event.model_dump(mode="json")))

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        try:
            hello = parse_rpc_line((await reader.readline()).decode())
            assert isinstance(hello, Hello)
            await self._send(
                writer,
                HelloOk(
                    capabilities={"api_version": "1.0", "contract_version": "1.0.0"},
                    server_time=datetime.now(ZoneInfo(self.timezone)),
                    timezone=self.timezone,
                ),
            )
            while True:
                line = await reader.readline()
                if not line:
                    return
                message = parse_rpc_line(line.decode())
                if isinstance(message, Subscribe):
                    self.subscriptions.append(dict(message.topics))
                    await self._send(writer, Subscribed(topics=message.topics))
                elif isinstance(message, RpcRequest):
                    await self._handle_request(writer, message)
        finally:
            self._writers.discard(writer)
            writer.close()
            with contextlib.suppress(ConnectionResetError, TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)

    async def _handle_request(self, writer: asyncio.StreamWriter, message: RpcRequest) -> None:
        if message.method == "deliver":
            if self.reject_deliver:
                self.reject_deliver = False
                await self._send(
                    writer,
                    RpcResponse(
                        id=message.id,
                        ok=False,
                        type="error",
                        error=RpcError(code="invalid_params", message="参数不足"),
                    ),
                )
                return
            client_msg_id = str(message.params.get("client_msg_id", ""))
            if client_msg_id and client_msg_id in self._seen:
                await self._send(writer, RpcResponse(id=message.id, result=None))
                return
            if client_msg_id:
                self._seen.add(client_msg_id)
            self.delivered.append(str(message.params["text"]))
            await self._send(writer, RpcResponse(id=message.id, result=None))
        elif message.method == "now":
            await self._send(
                writer,
                RpcResponse(
                    id=message.id,
                    result={"server_time": datetime.now(UTC).isoformat()},
                ),
            )
        elif message.method == "get_presence":
            snapshot = PresenceSnapshot(
                captured_at=datetime.now(UTC),
                readable=ReadablePresence(summary="在电脑前"),
            )
            await self._send(
                writer,
                RpcResponse(id=message.id, result=snapshot.model_dump(mode="json")),
            )
        elif message.method == "list_tasks":
            await self._send(writer, RpcResponse(id=message.id, result=[]))
        else:
            await self._send(writer, RpcResponse(id=message.id, result=None))

    async def _send(self, writer: asyncio.StreamWriter, message: object) -> None:
        writer.write(message.model_dump_json().encode() + b"\n")  # type: ignore[attr-defined]
        await writer.drain()


@pytest.fixture
async def fake_server(tmp_path: Path) -> AsyncIterator[FakeServer]:
    server = FakeServer(tmp_path / "aliya.sock")
    await server.start()
    yield server
    await server.stop()


def make_envelope(message_id: str) -> InboundEnvelope:
    return InboundEnvelope(
        message=TerminalMessage(
            message_id=message_id,
            direction="user_to_aliya",
            kind="chat",
            text="在吗",
            session_id="s1",
        ),
        conversation=ConversationContext(session_id="s1"),
    )


@pytest.mark.asyncio
async def test_call_round_trip(fake_server: FakeServer) -> None:
    client = RemoteControl(str(fake_server.socket_path), token="t")
    await client.connect()
    caps = await client.handshake()
    assert caps.contract_version == "1.0.0"
    result = await client.call("now", {})
    assert isinstance(result, dict)
    assert "server_time" in result
    await client.close()


@pytest.mark.asyncio
async def test_now_uses_server_timezone(tmp_path: Path) -> None:
    """握手拿到的是服务端时区，now() 不能把东八区下午还原成 UTC 早上。"""
    server = FakeServer(tmp_path / "aliya.sock", timezone="Asia/Shanghai")
    await server.start()
    client = RemoteControl(str(server.socket_path), token="t")
    try:
        await client.connect()
        now = client.now()
        assert now.utcoffset() == timedelta(hours=8)
        assert now.hour == datetime.now(ZoneInfo("Asia/Shanghai")).hour
    finally:
        await client.close()
        await server.stop()


@pytest.mark.asyncio
async def test_fetch_presence_uses_rpc(fake_server: FakeServer) -> None:
    client = RemoteControl(str(fake_server.socket_path), token="t")
    await client.connect()
    snapshot = await client.fetch_presence()
    assert snapshot.readable.summary == "在电脑前"
    assert client.get_presence().readable.summary == "在电脑前"
    await client.close()


@pytest.mark.asyncio
async def test_deliver(fake_server: FakeServer) -> None:
    client = RemoteControl(str(fake_server.socket_path), token="t")
    await client.connect()
    await client.deliver("在吗", session_id="s1")
    assert fake_server.delivered == ["在吗"]
    await client.close()


@pytest.mark.asyncio
async def test_inbound(fake_server: FakeServer) -> None:
    client = RemoteControl(str(fake_server.socket_path), token="t")
    await client.connect()
    stream = client.inbound()
    pending = asyncio.create_task(stream.__anext__())
    await asyncio.sleep(0)
    await fake_server.push_inbound(make_envelope("m1"))
    envelope = await asyncio.wait_for(pending, timeout=2)
    assert envelope.message.message_id == "m1"
    await client.ack_inbound("m1")
    await client.close()


@pytest.mark.asyncio
async def test_reconnect_replays_outbox(fake_server: FakeServer) -> None:
    client = RemoteControl(str(fake_server.socket_path), token="t", request_timeout=2)
    await asyncio.wait_for(client.connect(), timeout=3)
    await asyncio.wait_for(fake_server.stop(), timeout=3)

    task = asyncio.create_task(client.deliver("在吗", session_id="s1"))
    await asyncio.sleep(0.2)
    await asyncio.wait_for(fake_server.start(), timeout=3)
    with contextlib.suppress(ConnectionError, TimeoutError):
        await asyncio.wait_for(task, timeout=5)

    for _ in range(50):
        if fake_server.delivered:
            break
        await asyncio.sleep(0.1)
    assert fake_server.delivered == ["在吗"]
    await asyncio.wait_for(client.close(), timeout=3)


@pytest.mark.asyncio
async def test_subscribe_resumes_from_latest_topic_cursor(fake_server: FakeServer) -> None:
    client = RemoteControl(str(fake_server.socket_path), token="t")
    await client.connect()
    stream = client.subscribe(["agent.result"], since_sequence=2)
    pending = asyncio.create_task(stream.__anext__())
    for _ in range(50):
        if fake_server.subscriptions:
            break
        await asyncio.sleep(0.01)
    assert fake_server.subscriptions[-1] == {"agent.result": 2}

    await fake_server.push_event(
        CosmosEvent(
            event_id="e5",
            topic="agent.result",
            sequence=5,
            at=datetime.now(UTC),
            payload={},
        )
    )
    event = await asyncio.wait_for(pending, timeout=2)
    assert event.sequence == 5

    await client._resubscribe()
    for _ in range(50):
        if len(fake_server.subscriptions) >= 2:
            break
        await asyncio.sleep(0.01)
    assert fake_server.subscriptions[-1] == {"agent.result": 5}

    await stream.aclose()
    await client.close()


@pytest.mark.asyncio
async def test_rejected_deliver_is_not_replayed(fake_server: FakeServer) -> None:
    """业务拒绝是终态：消息必须移出 outbox，否则重连会无限补发。"""
    client = RemoteControl(str(fake_server.socket_path), token="t")
    await client.connect()
    fake_server.reject_deliver = True
    with pytest.raises(RemoteError):
        await client.deliver("在吗", session_id="s1")
    assert len(client._outbox) == 0
    assert fake_server.delivered == []
    await client.close()
