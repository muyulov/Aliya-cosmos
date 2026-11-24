"""桌面契约（设计 03 / 09 章）。

桌面是给 Aliya 看的面板：每张卡片固定呈现图标、名称、状态点与可解读摘要。
`ReadableState` 由 `DesktopService` 集中派生，渲染层不再自己拼文案。
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent.cosmos.contracts.agent import AgentCard
from agent.cosmos.contracts.terminal import TaskBrief

#: agent 运行态
type AgentStatus = Literal["idle", "queued", "running", "stalled", "abnormal", "disabled"]

#: 可解读状态的语气，用于配色与排序
type ReadableTone = Literal["calm", "busy", "strained", "alarming", "muted"]


class AgentTaskView(BaseModel):
    """当前或最近一次任务的视图。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    task_id: str
    label: str
    #: 0–100；None 表示无法量化（心跳型任务）
    progress: float | None = None
    step: str | None = None
    started_at: datetime
    elapsed_seconds: float = Field(default=0.0, ge=0)
    queue_depth: int = Field(default=0, ge=0)


class ReadableState(BaseModel):
    """一句话结论 + 数值补充 + 建议动作。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    summary: str
    meta: str | None = None
    tone: ReadableTone = "calm"
    hint: str | None = None


class DesktopEntry(BaseModel):
    """桌面上的一张卡片。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    card: AgentCard
    status: AgentStatus
    task: AgentTaskView | None = None
    readable: ReadableState
    badge: str | None = None


class DesktopGroup(BaseModel):
    """桌面分组。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    key: str
    title: str
    order: int = 100
    collapsed: bool = False


class DesktopSnapshot(BaseModel):
    """桌面全量快照。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    revision: int = 0
    generated_at: datetime
    groups: list[DesktopGroup] = Field(default_factory=list)
    #: 扁平列表，按展示顺序排列
    entries: list[DesktopEntry] = Field(default_factory=list)
    summary: str


class DesktopLayoutPatch(BaseModel):
    """布局补丁：幂等，只调顺序、分组、置顶、折叠。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    reorder: list[str] | None = None
    move: dict[str, str] | None = None
    pin: dict[str, bool] | None = None
    collapse: dict[str, bool] | None = None


class AgentDetail(BaseModel):
    """单卡详情。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    card: AgentCard
    status: AgentStatus
    task: AgentTaskView | None = None
    readable: ReadableState
    recent_tasks: list[TaskBrief] = Field(default_factory=list)
