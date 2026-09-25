"""时钟服务：为其他服务提供可替换的时间来源。

时区由 ClockSettings 决定，默认 UTC。
now() 刻意保持**同步**：它不涉及 I/O，服务方法不必一律 async。
这样依赖它的服务无需为了取时间把纯计算逻辑改成异步。
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar, override
from zoneinfo import ZoneInfo

from core.config import ClockSettings
from core.service.base import Service, ServiceState


class ClockService(Service):
    """约定：时间一律从这里取，时区由配置决定，测试可注入固定时间的替身。"""

    name: ClassVar[str] = "clock"

    def __init__(self, config: ClockSettings) -> None:
        super().__init__()
        self._config: ClockSettings = config

    @override
    async def start(self) -> None:
        """校验时区可解析：非法时区在启动期 fail fast，而不是等第一次取值。

        只校验不缓存：zoneinfo 内部有缓存，now() 每次取时区开销可忽略，
        这样 now() 也就不依赖 start() 是否跑过。
        """
        if self.state is ServiceState.RUNNING:
            return
        _ = ZoneInfo(self._config.tz)

    def now(self) -> datetime:
        """当前时间（配置时区）。"""
        return datetime.now(ZoneInfo(self._config.tz))
