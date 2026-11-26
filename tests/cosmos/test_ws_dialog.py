"""WS 对话集成测试：浏览器发言 → 真实服务 → 控制面回执 → 浏览器收到回复。"""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import pytest
from websockets.asyncio.client import connect

from agent.aliya.transport import RemoteControl
from agent.cosmos.assembly import build_cosmos_manager
from agent.cosmos.services import WebSocketChannelService
from agent.cosmos.services.ws_channel import parse_message
from core.config import Settings


def frame(raw: str | bytes) -> dict[str, object]:
    parsed = parse_message(raw)
    assert parsed is not None
    return parsed


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("ALIYA_CONTROL_TOKEN", "test-token")
    resolved = Settings()
    resolved.agent.control.socket_path = str(tmp_path / "aliya.sock")
    resolved.agent.cosmos.contract.state_dir = str(tmp_path / "state")
    resolved.agent.cosmos.presence.mood.provider = "off"
    ws = resolved.agent.cosmos.terminal.ws
    ws.enabled = True
    ws.host = "127.0.0.1"
    ws.port = 0
    ws.path = "/ws"
    ws.token_env = ""
    ws.history_limit = 0
    return resolved


@pytest.fixture
async def ws_url(settings: Settings) -> AsyncIterator[str]:
    manager = build_cosmos_manager(settings)
    async with manager.lifespan():
        address = manager.get(WebSocketChannelService).address
        assert address is not None
        yield f"ws://{address[0]}:{address[1]}/ws"


async def test_system_notice_is_delivered_with_system_kind(settings: Settings, ws_url: str) -> None:
    """失败提示走 kind=system；客户端据此渲染系统提示而不是 Aliya 台词。"""
    control = RemoteControl(settings.agent.control.socket_path, token="test-token")
    await control.connect()
    try:
        async with connect(ws_url, proxy=None) as connection:
            await connection.send(json.dumps({"type": "hello", "session_id": "web"}))
            _ = frame(await asyncio.wait_for(connection.recv(), timeout=3))

            await control.deliver(
                "回复失败，请稍后再试。",
                session_id="web",
                kind="system",
            )
            reply = frame(await asyncio.wait_for(connection.recv(), timeout=5))
            message = cast("dict[str, object]", reply["message"])
            assert message["kind"] == "system"
            assert message["text"] == "回复失败，请稍后再试。"
    finally:
        await control.close()


async def test_ws_dialog_round_trip(settings: Settings, ws_url: str) -> None:
    """不接 LLM：用控制面客户端扮演 Aliya，验证 WS 进、回复出。"""
    control = RemoteControl(settings.agent.control.socket_path, token="test-token")
    await control.connect()

    async def aliya_loop() -> None:
        async for envelope in control.inbound():
            await control.ack_inbound(envelope.message.message_id)
            await control.deliver(
                f"收到：{envelope.message.text}",
                session_id=envelope.message.session_id,
            )

    aliya = asyncio.create_task(aliya_loop())
    await asyncio.sleep(0.2)  # 等订阅就绪
    try:
        async with connect(ws_url, proxy=None) as connection:
            await connection.send(json.dumps({"type": "hello", "session_id": "web"}))
            ready = frame(await asyncio.wait_for(connection.recv(), timeout=3))
            assert ready == {"type": "ready", "protocol": "1.0", "session_id": "web"}

            await connection.send(json.dumps({"type": "message", "text": "你好呀"}))
            reply = frame(await asyncio.wait_for(connection.recv(), timeout=10))
            assert reply["type"] == "message"
            message = cast("dict[str, object]", reply["message"])
            assert message["text"] == "收到：你好呀"
            assert message["session_id"] == "web"
            assert message["direction"] == "aliya_to_user"
    finally:
        _ = aliya.cancel()
        _ = await asyncio.gather(aliya, return_exceptions=True)
        await control.close()
