"""子 agent 契约（设计 02 / 08 章）。

字段即接口文档：卡片既是调度依据也是展示依据；`AgentRequest` 刻意不携带
用户身份与对话历史，`AgentResult` 只回给 Aliya，不直接面向用户。
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from datetime import datetime
from typing import ClassVar, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent.cosmos.contracts.presence import PresenceField, PresenceSnapshot

#: agent id：小写蛇形，2–32 位
AGENT_ID_PATTERN = r"^[a-z][a-z0-9_]{1,31}$"
#: 能力标签：小写蛇形，至少 1 位
CAPABILITY_PATTERN = r"^[a-z][a-z0-9_]*$"

#: 展示名上限（02 章 2.1）
NAME_MAX_CHARS = 24
#: 桌面副标题上限（02 章 2.1）
SUMMARY_MAX_CHARS = 20


class AgentExample(BaseModel):
    """一条典型请求，提高自然语言匹配准确率。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    input: str
    note: str | None = None


class DesktopMeta(BaseModel):
    """卡片在桌面上的位置与呈现。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    group: str = "general"
    order: int = 100
    visible: bool = True
    pinned: bool = False
    badge: str | None = None


class ExecutionPolicy(BaseModel):
    """执行策略：超时、并发、排队、重试与心跳。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    timeout_seconds: float = Field(default=120.0, gt=0)
    max_concurrent: int = Field(default=1, ge=1)
    queue: bool = False
    cancel_on_supersede: bool = False
    retryable: bool = False
    heartbeat_seconds: float = Field(default=30.0, gt=0)


class AgentPermissions(BaseModel):
    """权限：默认全拒，按需打开。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    read_presence: bool = False
    presence_fields: list[PresenceField] = Field(default_factory=list)
    notify_user: bool = False
    read_conversation: bool = False


class AgentCard(BaseModel):
    """子 agent 的注册元数据 + 桌面展示元数据。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    id: str = Field(pattern=AGENT_ID_PATTERN)
    name: str = Field(min_length=1, max_length=NAME_MAX_CHARS)
    version: str = "0.1.0"
    icon: str = "🔧"
    #: 桌面副标题：模型层不设上限，超长由注册服务截断并 WARNING
    summary: str = Field(min_length=1)
    #: 完整能力说明：模型层不设上限，超长由服务层处理
    description: str = Field(min_length=1)
    capabilities: list[str] = Field(min_length=1)
    params_schema: dict[str, object] = Field(default_factory=lambda: {"type": "object"})
    examples: list[AgentExample] = Field(default_factory=list)
    desktop: DesktopMeta = DesktopMeta()
    execution: ExecutionPolicy = ExecutionPolicy()
    permissions: AgentPermissions = AgentPermissions()

    @model_validator(mode="after")
    def _check_capabilities(self) -> AgentCard:
        """能力标签必须是合法的小写蛇形标识。"""
        pattern = re.compile(CAPABILITY_PATTERN)
        for capability in self.capabilities:
            if not pattern.match(capability):
                msg = f"能力标签非法：{capability!r}"
                raise ValueError(msg)
        return self


class AgentRequest(BaseModel):
    """子 agent 的输入：参数自包含，不含用户身份与对话历史。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    request_id: str
    agent_id: str
    params: dict[str, object] = Field(default_factory=dict)
    label: str
    created_at: datetime
    deadline_at: datetime | None = None
    correlation_id: str = ""


class Artifact(BaseModel):
    """中间产物：cosmos 只登记与转发，不解析内容。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    kind: str
    name: str
    uri: str
    mime: str | None = None
    size: int | None = None


class AgentErrorInfo(BaseModel):
    """机器可读的失败信息。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    code: str
    message: str
    retriable: bool = False
    user_hint: str | None = None


class AgentMetrics(BaseModel):
    """可选指标：拿不到就不填，不编造数据。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    steps: int | None = None
    tokens: int | None = None
    files_touched: int | None = None


class AgentResult(BaseModel):
    """子 agent 的输出：只回给 Aliya，不直接面向用户。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    request_id: str = ""
    status: Literal["ok", "partial", "failed", "cancelled"]
    summary: str
    content: str | None = None
    artifacts: list[Artifact] = Field(default_factory=list)
    error: AgentErrorInfo | None = None
    metrics: AgentMetrics | None = None
    truncated: bool = False

    @model_validator(mode="after")
    def _check_consistency(self) -> AgentResult:
        """08 章 4.2 的四条约束。"""
        if not self.summary.strip():
            msg = "AgentResult.summary 不能为空"
            raise ValueError(msg)
        if self.status == "ok" and self.error is not None:
            msg = "status=ok 时不能带 error"
            raise ValueError(msg)
        if self.status in {"partial", "failed"} and self.error is None:
            msg = f"status={self.status} 时必须给 error"
            raise ValueError(msg)
        if self.status == "cancelled" and (self.error is None or self.error.code != "cancelled"):
            msg = "status=cancelled 时 error.code 必须是 cancelled"
            raise ValueError(msg)
        return self

    @classmethod
    def ok(
        cls,
        *,
        summary: str,
        content: str | None = None,
        artifacts: list[Artifact] | None = None,
        metrics: AgentMetrics | None = None,
        request_id: str = "",
    ) -> AgentResult:
        """成功结果。"""
        return cls(
            request_id=request_id,
            status="ok",
            summary=summary,
            content=content,
            artifacts=list(artifacts or []),
            metrics=metrics,
        )

    @classmethod
    def partial(
        cls,
        *,
        summary: str,
        error: AgentErrorInfo,
        content: str | None = None,
        artifacts: list[Artifact] | None = None,
        request_id: str = "",
    ) -> AgentResult:
        """部分完成：必须说明哪部分没成。"""
        return cls(
            request_id=request_id,
            status="partial",
            summary=summary,
            content=content,
            artifacts=list(artifacts or []),
            error=error,
        )

    @classmethod
    def failed(
        cls,
        *,
        code: str,
        message: str,
        retriable: bool = False,
        user_hint: str | None = None,
        summary: str | None = None,
        content: str | None = None,
        request_id: str = "",
    ) -> AgentResult:
        """失败结果：用返回值表达业务失败，不抛业务异常。"""
        return cls(
            request_id=request_id,
            status="failed",
            summary=summary or message,
            content=content,
            error=AgentErrorInfo(
                code=code,
                message=message,
                retriable=retriable,
                user_hint=user_hint,
            ),
        )

    @classmethod
    def cancelled(
        cls,
        *,
        summary: str = "任务已取消",
        request_id: str = "",
    ) -> AgentResult:
        """取消结果。"""
        return cls(
            request_id=request_id,
            status="cancelled",
            summary=summary,
            error=AgentErrorInfo(code="cancelled", message=summary, retriable=False),
        )


@runtime_checkable
class AgentContext(Protocol):
    """运行时能力：进度上报、心跳、中间产物与日志子步骤。"""

    @property
    def request_id(self) -> str: ...

    @property
    def cancelled(self) -> bool: ...

    @property
    def remaining_seconds(self) -> float | None: ...

    async def report(
        self,
        *,
        progress: float | None = None,
        step: str | None = None,
        message: str | None = None,
    ) -> None: ...

    async def heartbeat(self) -> None: ...

    async def emit(self, artifact: Artifact) -> None: ...

    async def read_presence(self) -> PresenceSnapshot | None: ...

    def child_scope(self, name: str) -> AbstractContextManager[None]: ...


#: handler 签名唯一：同步实现不被接受（无法响应取消）
type AgentHandler = Callable[[AgentRequest, AgentContext], Awaitable[AgentResult]]


class AgentMatch(BaseModel):
    """检索候选：score 仅用于排序，不是概率。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    agent_id: str
    score: float = Field(ge=0, le=1)
    reason: str
