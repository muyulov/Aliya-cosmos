import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent.cosmos.contracts import (
    ConversationContext,
    CosmosCapabilities,
    CosmosEvent,
    InboundEnvelope,
    PresenceSnapshot,
    ReadablePresence,
    TaskHandle,
    TerminalMessage,
)
from agent.cosmos.contracts.rpc import (
    AckInbound,
    Hello,
    Ping,
    RpcRequest,
    Subscribe,
    parse_rpc_line,
)
from agent.cosmos.services.control_server import ControlServerService
from core.config import ClockSettings, Settings
from core.service import ClockService


class FakeControl:
    def __init__(self) -> None:
        self.inbound_queue: asyncio.Queue[InboundEnvelope] = asyncio.Queue()
        self.event_queue: asyncio.Queue[CosmosEvent] = asyncio.Queue()
        self.delivered: list[str] = []
        self.invoked: tuple[str, dict[str, object], str] | None = None
        self.subscription: dict[str, object] | None = None

    async def deliver(self, text: str, *, session_id: str, kind: str = "chat") -> None:
        self.delivered.append(text)

    async def handshake(self) -> CosmosCapabilities:
        return CosmosCapabilities(api_version="1.0", contract_version="1.0.0")

    def get_presence(self) -> PresenceSnapshot:
        return PresenceSnapshot(
            captured_at=datetime.now(UTC),
            readable=ReadablePresence(summary="在电脑前"),
        )

    async def invoke_agent(
        self, agent_id: str, params: dict[str, object], *, session_id: str
    ) -> TaskHandle:
        self.invoked = (agent_id, params, session_id)
        return TaskHandle(
            task_id="t1",
            agent_id=agent_id,
            label="测试任务",
            accepted_at=datetime.now(UTC),
        )

    async def inbound(self) -> AsyncIterator[InboundEnvelope]:
        while True:
            yield await self.inbound_queue.get()

    async def subscribe(
        self,
        topics: list[str],
        *,
        since_sequence: int | None = None,
        cursors: dict[str, int] | None = None,
    ) -> AsyncIterator[CosmosEvent]:
        self.subscription = {
            "topics": topics,
            "since": since_sequence,
            "cursors": cursors,
        }
        while True:
            yield await self.event_queue.get()


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("ALIYA_CONTROL_TOKEN", "test-token")
    resolved = Settings()
    resolved.agent.control.socket_path = str(tmp_path / "aliya.sock")
    return resolved


@pytest.fixture
async def clock() -> AsyncIterator[ClockService]:
    service = ClockService(ClockSettings(tz="Asia/Shanghai"))
    await service.start()
    yield service
    await service.stop()


@pytest.fixture
def fake_control() -> FakeControl:
    return FakeControl()


@pytest.fixture
async def control_server(
    settings: Settings, clock: ClockService, fake_control: FakeControl
) -> AsyncIterator[ControlServerService]:
    service = ControlServerService(fake_control, clock, settings.agent)  # type: ignore[arg-type]
    await service.start()
    yield service
    await service.stop()


@pytest.fixture
def socket_path(settings: Settings) -> Path:
    return Path(settings.agent.control.socket_path)


async def hello(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    writer.write(
        Hello(token="test-token", contract_version="1.0", client_id="aliya")
        .model_dump_json()
        .encode()
        + b"\n"
    )
    await writer.drain()
    response = parse_rpc_line((await reader.readline()).decode())
    assert response.type == "hello_ok"


@pytest.mark.asyncio
async def test_hello_and_now(control_server: ControlServerService, socket_path: Path) -> None:
    reader, writer = await asyncio.open_unix_connection(socket_path)
    await hello(reader, writer)

    writer.write(RpcRequest(id="1", method="now").model_dump_json().encode() + b"\n")
    await writer.drain()
    response = parse_rpc_line((await reader.readline()).decode())
    assert response.type == "result"
    assert "server_time" in response.result
    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio
async def test_second_client_is_rejected(
    control_server: ControlServerService, socket_path: Path
) -> None:
    first_reader, first_writer = await asyncio.open_unix_connection(socket_path)
    await hello(first_reader, first_writer)

    second_reader, second_writer = await asyncio.open_unix_connection(socket_path)
    response = parse_rpc_line((await second_reader.readline()).decode())
    assert response.type == "error"
    assert response.error is not None
    assert response.error.code == "busy"

    first_writer.close()
    second_writer.close()
    await first_writer.wait_closed()
    await second_writer.wait_closed()


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
async def test_inbound_is_pushed_and_acked(
    control_server: ControlServerService,
    socket_path: Path,
    fake_control: FakeControl,
) -> None:
    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        await hello(reader, writer)

        fake_control.inbound_queue.put_nowait(make_envelope("m1"))
        message = parse_rpc_line((await reader.readline()).decode())
        assert message.type == "inbound"
        assert message.envelope["message"]["message_id"] == "m1"

        writer.write(AckInbound(message_id="m1").model_dump_json().encode() + b"\n")
        await writer.drain()
        for _ in range(20):
            if control_server.pending_count == 0:
                break
            await asyncio.sleep(0.01)
        assert control_server.pending_count == 0
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_event_is_pushed(
    control_server: ControlServerService,
    socket_path: Path,
    fake_control: FakeControl,
) -> None:
    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        await hello(reader, writer)
        writer.write(Subscribe(topics={"agent.result": 0}).model_dump_json().encode() + b"\n")
        await writer.drain()
        assert parse_rpc_line((await reader.readline()).decode()).type == "subscribed"

        fake_control.event_queue.put_nowait(
            CosmosEvent(
                event_id="e1",
                topic="agent.result",
                sequence=1,
                at=datetime.now(UTC),
                payload={"summary": "好了"},
            )
        )
        message = parse_rpc_line((await reader.readline()).decode())
        assert message.type == "event"
        assert message.event["topic"] == "agent.result"
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_deliver_is_deduplicated(
    control_server: ControlServerService,
    socket_path: Path,
    fake_control: FakeControl,
) -> None:
    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        await hello(reader, writer)
        request = RpcRequest(
            id="1",
            method="deliver",
            params={"text": "在吗", "session_id": "s1", "client_msg_id": "c1"},
        )
        writer.write(request.model_dump_json().encode() + b"\n")
        await writer.drain()
        assert parse_rpc_line((await reader.readline()).decode()).type == "result"

        writer.write(request.model_copy(update={"id": "2"}).model_dump_json().encode() + b"\n")
        await writer.drain()
        assert parse_rpc_line((await reader.readline()).decode()).type == "result"
        assert fake_control.delivered == ["在吗"]
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_subscribe_passes_per_topic_cursors(
    control_server: ControlServerService,
    socket_path: Path,
    fake_control: FakeControl,
) -> None:
    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        await hello(reader, writer)
        writer.write(
            Subscribe(topics={"agent.result": 3, "presence.changed": 1}).model_dump_json().encode()
            + b"\n"
        )
        await writer.drain()
        assert parse_rpc_line((await reader.readline()).decode()).type == "subscribed"
        for _ in range(50):
            if fake_control.subscription is not None:
                break
            await asyncio.sleep(0.01)
        assert fake_control.subscription == {
            "topics": ["agent.result", "presence.changed"],
            "since": 1,
            "cursors": {"agent.result": 3, "presence.changed": 1},
        }
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_get_presence_rpc_handles_sync_method(
    control_server: ControlServerService,
    socket_path: Path,
) -> None:
    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        await hello(reader, writer)
        writer.write(RpcRequest(id="1", method="get_presence").model_dump_json().encode() + b"\n")
        await writer.drain()
        response = parse_rpc_line((await reader.readline()).decode())
        assert response.type == "result"
        assert response.result["readable"]["summary"] == "在电脑前"
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_invoke_agent_rpc_forwards_session_id(
    control_server: ControlServerService,
    socket_path: Path,
    fake_control: FakeControl,
) -> None:
    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        await hello(reader, writer)
        request = RpcRequest(
            id="1",
            method="invoke_agent",
            params={
                "agent_id": "echo",
                "params": {"text": "hi"},
                "session_id": "s1",
            },
        )
        writer.write(request.model_dump_json().encode() + b"\n")
        await writer.drain()
        response = parse_rpc_line((await reader.readline()).decode())
        assert response.type == "result"
        assert response.result["task_id"] == "t1"
        assert fake_control.invoked == ("echo", {"text": "hi"}, "s1")
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_ping_gets_pong(
    control_server: ControlServerService,
    socket_path: Path,
) -> None:
    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        await hello(reader, writer)
        now = datetime.now(UTC)
        writer.write(Ping(at=now).model_dump_json().encode() + b"\n")
        await writer.drain()
        response = parse_rpc_line((await reader.readline()).decode())
        assert response.type == "pong"
        assert response.at == now
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_unacked_inbound_is_replayed_on_reconnect(
    control_server: ControlServerService,
    socket_path: Path,
    fake_control: FakeControl,
) -> None:
    reader, writer = await asyncio.open_unix_connection(socket_path)
    await hello(reader, writer)
    fake_control.inbound_queue.put_nowait(make_envelope("m1"))
    message = parse_rpc_line((await reader.readline()).decode())
    assert message.type == "inbound"
    assert message.envelope["message"]["message_id"] == "m1"

    writer.close()
    await writer.wait_closed()
    await asyncio.sleep(0.05)

    reader2, writer2 = await asyncio.open_unix_connection(socket_path)
    try:
        await hello(reader2, writer2)
        replay = parse_rpc_line((await asyncio.wait_for(reader2.readline(), 1)).decode())
        assert replay.type == "inbound"
        assert replay.envelope["message"]["message_id"] == "m1"

        writer2.write(AckInbound(message_id="m1").model_dump_json().encode() + b"\n")
        await writer2.drain()
        for _ in range(50):
            if control_server.pending_count == 0:
                break
            await asyncio.sleep(0.01)
        assert control_server.pending_count == 0
    finally:
        writer2.close()
        await writer2.wait_closed()


@pytest.mark.asyncio
async def test_empty_token_is_rejected(
    control_server: ControlServerService, socket_path: Path
) -> None:
    """服务端配了令牌时，空令牌客户端必须被拒绝（不能 fail-open）。"""
    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        writer.write(
            Hello(token="", contract_version="1.0", client_id="attacker").model_dump_json().encode()
            + b"\n"
        )
        await writer.drain()
        response = parse_rpc_line((await reader.readline()).decode())
        assert response.type == "error"
        assert response.error is not None
        assert response.error.code == "unauthorized"
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_start_without_token_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未配置令牌时服务端 fail fast，而不是以无认证状态启动。"""
    monkeypatch.delenv("ALIYA_CONTROL_TOKEN", raising=False)
    settings = Settings()
    settings.agent.control.socket_path = str(tmp_path / "aliya.sock")
    service = ControlServerService(
        FakeControl(),  # type: ignore[arg-type]
        ClockService(ClockSettings()),
        settings.agent,
    )
    with pytest.raises(RuntimeError):
        await service.start()
    assert not Path(settings.agent.control.socket_path).exists()
