"""装配入口：15 个服务、启停、两个进程入口。"""

from __future__ import annotations

import asyncio
import signal
import subprocess
import sys
from pathlib import Path

from agent.cosmos.assembly import build_cosmos_manager
from agent.cosmos.services import ControlService
from agent.cosmos.transport import BusService
from core.config import Settings
from core.service import ServiceState
from core.service.registry import build_manager

ROOT = Path(__file__).resolve().parents[2]


def test_装配出十五个服务() -> None:
    manager = build_cosmos_manager(Settings())
    names = manager.names
    assert len(names) == 15
    assert "bus" in names
    assert "cosmos.registry" in names
    assert "cosmos.runtime" in names
    assert "cosmos.desktop" in names
    assert "cosmos.presence" in names
    assert "cosmos.conversation" in names
    assert "cosmos.dispatch" in names
    assert "cosmos.world" in names
    assert "cosmos.terminal" in names
    assert "cosmos.control" in names
    assert "cosmos.control_server" in names


async def test_全部服务可启动与停止() -> None:
    manager = build_cosmos_manager(Settings())
    async with manager.lifespan():
        assert all(service.running for service in manager.services)
        assert manager.get(BusService).running is True
        assert manager.get(ControlService).running is True

    assert all(service.state is not ServiceState.RUNNING for service in manager.services)


async def test_缺世界端口且关闭时能正常启动() -> None:
    settings = Settings()
    assert settings.agent.cosmos.world.enabled is False
    manager = build_cosmos_manager(settings)
    async with manager.lifespan():
        assert all(service.running for service in manager.services)


def test_core_入口行为不变() -> None:
    manager = build_manager(Settings())
    assert len(manager.names) == 4
    assert set(manager.names) == {"clock", "embedding", "llm", "decision"}


async def test_cosmos_进程可优雅退出() -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "agent.cosmos.app",
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        await asyncio.sleep(3)
        process.send_signal(signal.SIGINT)
        code = await asyncio.wait_for(process.wait(), timeout=10)
    finally:
        if process.returncode is None:
            process.kill()
            _ = await process.wait()
    assert code == 0
