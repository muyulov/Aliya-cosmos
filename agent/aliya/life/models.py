"""生活层数据契约；跨回合存活的状态只在这里定义。"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

ActivityKind = Literal["repair", "observe", "rest", "wander", "social", "organize", "read"]
Interruptibility = Literal["high", "medium", "low"]


class Activity(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    #: 必须能直接说出口；这段话会进 prompt 与 AliyaContext
    title: str
    place: str
    kind: ActivityKind
    started_at: datetime
    expected_minutes: int = Field(default=30, gt=0)
    interruptibility: Interruptibility = "medium"
    output_hint: str = ""
    turn_done: bool = False
    interrupted_at: datetime | None = None

    def minutes_left(self, now: datetime) -> int:
        """按叙事占用的预期时长算剩余分钟；已超时返回 0。"""
        used = (now - self.started_at).total_seconds() / 60
        return max(0, round(self.expected_minutes - used))


class Move(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", populate_by_name=True)

    from_: str = Field(alias="from")
    to: str
    departed_at: datetime
    arrive_at: datetime


class PendingActivity(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    title: str
    place: str
    kind: ActivityKind
    expected_minutes: int = Field(default=30, gt=0)
    interruptibility: Interruptibility = "medium"
    output_hint: str = ""


class ActivityState(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    version: int = 1
    #: 当前所在舱室；移动中不改，到达后才更新
    place: str = "cabin"
    energy: float = Field(default=0.8, ge=0, le=1)
    current: Activity | None = None
    moving: Move | None = None
    pending: list[PendingActivity] = Field(default_factory=list)
    #: 最近一次日终反思的日期（含失败尝试），跨进程持久化
    last_reflection_date: str | None = None
    updated_at: datetime
