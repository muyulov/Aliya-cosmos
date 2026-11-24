"""任务运行时契约（文档未给模型，此处补齐）。

这些模型只用于事件载荷与桌面读取：不含 `params` 与结果正文，避免内部细节
顺着面板或事件流泄露到面向用户的方向。
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent.cosmos.contracts.agent import AgentErrorInfo
from agent.cosmos.contracts.desktop import AgentStatus
from agent.cosmos.contracts.dispatch import DispatchOutcome


class AgentRuntimeState(BaseModel):
    """单个 agent 的运行态快照。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    agent_id: str
    status: AgentStatus = "idle"
    running: int = Field(default=0, ge=0)
    queued: int = Field(default=0, ge=0)
    current_task_id: str | None = None
    last_heartbeat_at: datetime | None = None
    last_error: AgentErrorInfo | None = None
    error_streak: int = Field(default=0, ge=0)
    last_used_at: datetime | None = None


class AgentProgress(BaseModel):
    """一次进度上报。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    task_id: str
    agent_id: str
    label: str
    progress: float | None = None
    step: str | None = None
    message: str | None = None
    at: datetime


class AgentAbnormal(BaseModel):
    """异常事件：超时、心跳缺失、执行出错。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    agent_id: str
    reason: Literal["timeout", "stalled", "error", "internal"]
    error: AgentErrorInfo | None = None
    at: datetime


class DispatchFinished(BaseModel):
    """整批结束事件载荷。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    outcome: DispatchOutcome


class RuntimeStats(BaseModel):
    """运行统计：耗时中位数与连续失败次数。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    #: 历史耗时 P50；样本不足时为 None（没有基线就不下结论）
    p50_seconds: float | None = None
    error_streak: int = Field(default=0, ge=0)
