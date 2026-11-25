"""Aliya 运行时：串行回合、事件缓存与自主 tick。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

type Phase = Literal["daily", "maintenance", "crisis", "rest"]


def phase_for(*, now: datetime, tasks: int, crisis: bool) -> Phase:
    """回合结束后回传的处境：有任务优先，其次危机，再按时段。"""
    if tasks > 0:
        return "maintenance"
    if crisis:
        return "crisis"
    if now.hour >= 23 or now.hour < 5:
        return "rest"
    return "daily"
