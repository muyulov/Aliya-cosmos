"""调度契约（设计 05 章）。

`DispatchPlan` 是「候选 → 计划」的产物：只描述要做什么，不包含执行结果。
`depends_on` 用同批次任务的 0 基序号表达先后关系；模型层负责拦下越界、
自依赖与环，避免运行期才发现。
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent.cosmos.contracts.agent import AgentResult

#: 计划决策
type DispatchDecision = Literal["reply_only", "single", "parallel", "clarify", "rejected"]

#: 意图来源
type DispatchSource = Literal["desktop", "natural_language", "explicit"]


class DispatchIntent(BaseModel):
    """调度入口：桌面点击、自然语言、显式调用归一成这一种输入。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    source: DispatchSource
    session_id: str
    user_text: str | None = None
    agent_id: str | None = None
    params: dict[str, object] | None = None
    hint: str | None = None


class PlannedTask(BaseModel):
    """计划里的单个任务。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    agent_id: str
    params: dict[str, object] = Field(default_factory=dict)
    label: str
    #: 依赖同批次其它任务的序号（0 基）
    depends_on: list[int] = Field(default_factory=list)


class DispatchPlan(BaseModel):
    """调度计划：只生成，不产生副作用。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    decision: DispatchDecision
    tasks: list[PlannedTask] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0, le=1)
    rationale: str
    missing: list[str] = Field(default_factory=list)
    #: 生成计划时的会话；execute() 用它做幂等去重（文档模型未给，此处补齐）
    session_id: str = ""

    def _check_dependencies(self) -> None:
        """校验序号不越界、无自依赖、无环。"""
        total = len(self.tasks)
        for index, task in enumerate(self.tasks):
            for dependency in task.depends_on:
                if dependency < 0 or dependency >= total:
                    msg = f"任务 {index} 依赖序号越界：{dependency}"
                    raise ValueError(msg)
                if dependency == index:
                    msg = f"任务 {index} 依赖了自己"
                    raise ValueError(msg)

        # DFS 三色标记找环
        visiting: set[int] = set()
        done: set[int] = set()

        def visit(node: int) -> None:
            if node in done:
                return
            if node in visiting:
                msg = f"调度计划存在依赖环，任务 {node}"
                raise ValueError(msg)
            visiting.add(node)
            for dependency in self.tasks[node].depends_on:
                visit(dependency)
            visiting.discard(node)
            done.add(node)

        for index in range(total):
            visit(index)

    @model_validator(mode="after")
    def _validate_dependencies(self) -> DispatchPlan:
        self._check_dependencies()
        return self


class TaskHandle(BaseModel):
    """提交成功后返回的任务句柄。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    task_id: str
    agent_id: str
    label: str
    accepted_at: datetime
    queued: bool = False


class RejectedTask(BaseModel):
    """被拒绝的任务及原因。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    agent_id: str
    reason: str


class DispatchHandle(BaseModel):
    """整批提交的回执：提交是异步的，结果等 `dispatch.finished`。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    dispatch_id: str
    task_ids: list[str] = Field(default_factory=list)
    accepted_at: datetime
    status: Literal["accepted", "partial"] = "accepted"
    rejected: list[RejectedTask] = Field(default_factory=list)


class DispatchOutcome(BaseModel):
    """整批结果：只汇总状态与各自 summary，不合并内容结论。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    dispatch_id: str
    status: Literal["ok", "partial", "failed", "cancelled"]
    results: list[AgentResult] = Field(default_factory=list)
    elapsed_seconds: float = Field(default=0.0, ge=0)
    summary: str
