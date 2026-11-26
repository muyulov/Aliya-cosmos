"""装配入口：15 个服务、启停、两个进程入口。"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from agent.cosmos.assembly import build_cosmos_manager
from agent.cosmos.services import ControlService
from agent.cosmos.transport import BusService
from core.config import Settings
from core.service import ServiceState
from core.service.registry import build_manager

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOCKET = ROOT / "data/state/aliya.sock"


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """隔离控制面 socket 与运行数据，避免与本机已运行的 cosmos 冲突。"""
    monkeypatch.setenv("ALIYA_CONTROL_TOKEN", "test-token")
    resolved = Settings()
    resolved.agent.control.socket_path = str(tmp_path / "aliya.sock")
    resolved.agent.cosmos.contract.state_dir = str(tmp_path / "state")
    return resolved


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


async def test_全部服务可启动与停止(settings: Settings) -> None:
    manager = build_cosmos_manager(settings)
    async with manager.lifespan():
        assert all(service.running for service in manager.services)
        assert manager.get(BusService).running is True
        assert manager.get(ControlService).running is True

    assert all(service.state is not ServiceState.RUNNING for service in manager.services)


async def test_缺世界端口且关闭时能正常启动(settings: Settings) -> None:
    assert settings.agent.cosmos.world.enabled is False
    manager = build_cosmos_manager(settings)
    async with manager.lifespan():
        assert all(service.running for service in manager.services)


def test_core_入口行为不变() -> None:
    manager = build_manager(Settings())
    assert len(manager.names) == 4
    assert set(manager.names) == {"clock", "embedding", "llm", "decision"}


async def _socket_busy(path: Path) -> bool:
    """判断 socket 是否已被监听；残留文件直接清理。"""
    if not path.exists():
        return False
    try:
        _reader, writer = await asyncio.open_unix_connection(path)
    except OSError:
        path.unlink(missing_ok=True)
        return False
    writer.close()
    with contextlib.suppress(ConnectionResetError, TimeoutError):
        await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
    return True


async def test_cosmos_进程可优雅退出() -> None:
    if await _socket_busy(DEFAULT_SOCKET):
        pytest.skip("默认控制面 socket 已被占用，跳过进程级测试")
    env = {**os.environ, "ALIYA_CONTROL_TOKEN": "test-token"}
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "agent.cosmos.app",
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # 轮询 socket 出现代替固定 sleep，避免慢机器上误判。
        for _ in range(100):
            if DEFAULT_SOCKET.exists() or process.returncode is not None:
                break
            await asyncio.sleep(0.1)
        assert process.returncode is None, f"cosmos 提前退出：{process.returncode}"
        assert DEFAULT_SOCKET.exists(), "cosmos 未在 10 秒内启动控制面"
        # socket 已监听后再留一点时间让信号处理器挂好。
        await asyncio.sleep(0.3)
        process.send_signal(signal.SIGINT)
        code = await asyncio.wait_for(process.wait(), timeout=15)
    finally:
        if process.returncode is None:
            process.kill()
            _ = await process.wait()
    assert code == 0
