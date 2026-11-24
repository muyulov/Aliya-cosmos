"""用户状态契约（设计 04 章）。

六类字段各自建模，`availability` 显式记录可用性：拿不到数据时必须显式 `unknown`
并给出 reason，禁止用默认值伪装完整。
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

#: 六类字段名；`availability` 与订阅过滤都用它
type PresenceField = Literal["online", "activity", "typing", "mood", "task_context", "env"]

#: 可解读表述的语气
type PresenceTone = Literal["calm", "engaged", "waiting", "away", "unknown"]

#: 字段不可用的原因
type UnavailableReason = Literal["channel_not_supported", "disabled_by_config", "no_evidence"]


class OnlineInfo(BaseModel):
    """在线状态（04 章 2.1）。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    state: Literal["online", "away", "busy", "offline", "unknown"] = "unknown"
    since: datetime | None = None
    last_seen_at: datetime | None = None


class ActivityInfo(BaseModel):
    """活跃度（04 章 2.2）：只统计数量与时间，不分析内容。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    last_message_at: datetime | None = None
    messages_last_15m: int = 0
    messages_today: int = 0
    session_duration_seconds: int | None = None
    silence_seconds: int | None = None


class TypingInfo(BaseModel):
    """输入状态（04 章 2.3）：依赖通道能力，不支持时整项不可用。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    is_typing: bool = False
    typing_started_at: datetime | None = None
    typing_paused_seconds: int | None = None


class MoodInfo(BaseModel):
    """情绪信号（04 章 2.4）：必须有证据与置信度。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    valence: float = Field(default=0.0, ge=-1, le=1)
    energy: float = Field(default=0.0, ge=0, le=1)
    confidence: float = Field(default=0.0, ge=0, le=1)
    source: Literal["decision", "llm", "explicit", "none"] = "none"
    evidence_count: int = Field(default=0, ge=0)
    updated_at: datetime | None = None


class TaskContextInfo(BaseModel):
    """任务上下文（04 章 2.5）：用户是否在等结果。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    awaiting_result: bool = False
    awaiting_task_id: str | None = None
    awaiting_since: datetime | None = None
    has_pending_tasks: bool = False
    pending_count: int = Field(default=0, ge=0)


class EnvInfo(BaseModel):
    """环境信号（04 章 2.6）：时区、时段、设备。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    timezone: str | None = None
    local_time: datetime | None = None
    period: Literal["morning", "afternoon", "evening", "night", "unknown"] = "unknown"
    device: Literal["desktop", "mobile", "web", "unknown"] = "unknown"


class PresenceFields(BaseModel):
    """六类字段的完整快照。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    online: OnlineInfo = OnlineInfo()
    activity: ActivityInfo = ActivityInfo()
    typing: TypingInfo = TypingInfo()
    mood: MoodInfo = MoodInfo()
    task_context: TaskContextInfo = TaskContextInfo()
    env: EnvInfo = EnvInfo()


class Availability(BaseModel):
    """单个字段是否可用及原因。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    available: bool = True
    reason: UnavailableReason | None = None


class ReadablePresence(BaseModel):
    """给 Aliya 看的可解读表述（04 章 3）。

    文案超长由 `PresenceService` 截断而非抛错，因此这里不设 max_length。
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    summary: str
    detail: str | None = None
    tone: PresenceTone = "unknown"
    hint: str | None = None
    confidence: float = Field(default=0.5, ge=0, le=1)
    stale: bool = False


class PresenceSnapshot(BaseModel):
    """状态快照：原始字段 + 可用性 + 可解读表述。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    captured_at: datetime
    ttl_seconds: int = Field(default=30, ge=0)
    fields: PresenceFields = PresenceFields()
    availability: dict[str, Availability] = Field(default_factory=dict)
    readable: ReadablePresence
    revision: int = 0


class PresenceChanged(BaseModel):
    """状态变化事件（04 章 4.2）：推全量快照而不是 diff。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    revision: int
    changed: list[str] = Field(default_factory=list)
    snapshot: PresenceSnapshot
    readable: ReadablePresence
    at: datetime


class PresenceInput(BaseModel):
    """通道上报的状态输入（文档未给字段，此处补齐）。

    所有字段可选：通道只能上报它支持的能力，缺失即「这次没说」，不是「值是默认」。
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    online: Literal["online", "away", "busy", "offline"] | None = None
    is_typing: bool | None = None
    device: Literal["desktop", "mobile", "web"] | None = None
    timezone: str | None = None
    busy: bool | None = None
