"""cosmos 测试的公共夹具。

约定：
- `clock`：真实 `ClockService`，时区固定 Asia/Shanghai；测试需要推进时间时，
  自己写一个返回固定时间的替身，不改这里。
- `settings`：直接构造 `Settings()`，不读 YAML，保证用例与本地配置无关。
- `bus`：`BusService`（依赖 clock），已 `start()`；实现落地于 Task 6，随后补入。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from agent.cosmos.transport import BusService
from core.config import ClockSettings, Settings
from core.service import ClockService


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture(autouse=True)
def _isolate_state_dir(settings: Settings, tmp_path: Path) -> None:
    """所有用例的 state_dir 都指向临时目录，避免把桌面/轨迹写进仓库。"""
    settings.agent.cosmos.contract.state_dir = str(tmp_path)


@pytest.fixture
async def clock() -> AsyncIterator[ClockService]:
    service = ClockService(ClockSettings(tz="Asia/Shanghai"))
    await service.start()
    yield service
    await service.stop()


@pytest.fixture
async def bus(settings: Settings, clock: ClockService) -> AsyncIterator[BusService]:
    service = BusService(settings.agent, clock)
    await service.start()
    yield service
    await service.stop()
