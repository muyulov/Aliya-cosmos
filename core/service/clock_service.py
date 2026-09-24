"""时钟服务：为其他服务提供可替换的时间来源。

now() 刻意保持**同步**：它不涉及 I/O，服务方法不必一律 async。
这样依赖它的服务无需为了取时间把纯计算逻辑改成异步。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

from core.service.base import Service


class ClockService(Service):
    """约定：时间一律从这里取，测试时可注入固定时间的替身。"""

    name: ClassVar[str] = "clock"

    def now(self) -> datetime:
        """当前时间（UTC）。"""
        return datetime.now(UTC)
