import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent.aliya.transport import RemoteControl
from agent.cosmos.contracts import (
    ConversationContext,
    InboundEnvelope,
    TerminalMessage,
)
from agent.cosmos.contracts.rpc import (
    Hello,
    HelloOk,
    Inbound,
    RpcRequest,
    RpcResponse,
    Subscribe,
    Subscribed,
    parse_rpc_line,
)


class FakeServer:
    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path
        self.delivered: list[str] = []
        self._server: asyncio.AbstractServer | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        self._server = await asyncio.start_unix_server(
            self._handle, path=str(self.socket_path)
        )

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for writer in list(self._writers):
            writer.close()
            with contextlib.suppress(ConnectionResetError):
                await writer.wait_closed()

    async def push_inbound(self, envelope: InboundEnvelope) -> None:
        for writer in list(self._writers):
            await self._send(
                writer,
                Inbound(envelope=envelope.model_dump(mode="json")),
            )

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._writers.add(writer)
        try:
            hello = parse_rpc_line((await reader.readline()).decode())
            assert isinstance(hello, Hello)
            await self._send(
                writer,
                HelloOk(
                    capabilities={"api_version": "1.0", "contract_version": "1.0.0"},
                    server_time=datetime.now(UTC),
                    timezone="UTC",
                ),
            )
            while True:
                line = await reader.readline()
                if not line:
                    return
                message = parse_rpc_line(line.decode())
                if isinstance(message, Subscribe):
                    await self._send(writer, Subscribed(topics=message.topics))
                elif isinstance(message, RpcRequest):
                    await self._handle_request(writer, message)
        finally:
            self._writers.discard(writer)
            writer.close()
            with contextlib.suppress(ConnectionResetError):
                await writer.wait_closed()

    async def _handle_request(
        self, writer: asyncio.StreamWriter, message: RpcRequest
    ) -> None:
        if message.method == "deliver":
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
