"""通信终端契约（设计 06 章）。

`TerminalMessage` 是唯一跨用户与 Aliya 边界的消息模型；`InboundEnvelope` 是
发给 Aliya 的信封，只带 `ReadablePresence` 与 `TaskBrief`，不带原始状态字段与
内部标识。
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent.cosmos.contracts.presence import ReadablePresence

#: 消息类型
type MessageKind = Literal["chat", "task", "query", "mixed", "progress", "result", "system"]

#: 通讯链路状态（01 章 3.1）
type LinkState = Literal["up", "unstable", "down"]


class Attachment(BaseModel):
    """消息附件（文档未给字段，此处补齐）。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    kind: Literal["image", "file", "audio"]
    name: str
    uri: str
    mime: str | None = None
    size: int | None = None


class MessageMeta(BaseModel):
    """消息元信息：通道侧字段与内部分段信息。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    channel: str | None = None
    device: str | None = None
    locale: str | None = None
    #: 是否 Aliya 主动发起
    proactive: bool = False
    #: 与调度批次的关联，仅内部使用
    dispatch_id: str | None = None
    #: 分段信息：`part_index` 从 1 开始
    part_index: int | None = None
    part_total: int | None = None


class TerminalMessage(BaseModel):
    """用户与 Aliya 之间的单条消息。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    #: 全局唯一；入口侧为空时由 TerminalService 生成
    message_id: str = ""
    direction: Literal["user_to_aliya", "aliya_to_user"]
    kind: MessageKind
    text: str
    #: 取 ClockService.now()；入口侧为空时由 TerminalService 填
    at: datetime | None = None
    session_id: str
    #: 客户端幂等键
    client_msg_id: str | None = None
    reply_to: str | None = None
    attachments: list[Attachment] = Field(default_factory=list)
    meta: MessageMeta = MessageMeta()


class ConversationTurn(BaseModel):
    """一轮对话（文档未给字段，此处补齐）。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    message_id: str
    role: Literal["user", "aliya"]
    text: str
    at: datetime


class PinnedFact(BaseModel):
    """跨轮次保留的稳定事实：偏好、约束、决定、未收尾任务。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    key: str
    value: str
    source_message_id: str = ""
    confidence: float = Field(default=0.8, ge=0, le=1)
    expires_at: datetime | None = None


class ConversationContext(BaseModel):
    """发给 Aliya 的对话上下文。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    session_id: str = ""
    turns: list[ConversationTurn] = Field(default_factory=list)
    summary: str | None = None
    last_user_message_at: datetime | None = None
    last_aliya_message_at: datetime | None = None
    #: 用户连续发了几条而 Aliya 还没回
    unanswered_count: int = Field(default=0, ge=0)
    topic_hint: str | None = None
    #: 预算裁剪后仍保留的稳定事实
    pinned_facts: list[PinnedFact] = Field(default_factory=list)
    #: 估算 token，供调用方判断是否接近预算
    token_estimate: int = Field(default=0, ge=0)
    #: 是否因预算裁剪了部分上下文
    truncated: bool = False
    #: 摘要是否降级为截断
    summary_degraded: bool = False


class TaskBrief(BaseModel):
    """任务摘要：只有标签、状态、进度，不含正文与内部标识。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    task_id: str
    #: 展示名，不是 id
    agent_name: str
    label: str
    status: str
    progress: float | None = None
    elapsed_seconds: float = 0.0


class InboundEnvelope(BaseModel):
    """发给 Aliya 的用户消息信封。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    message: TerminalMessage
    conversation: ConversationContext
    #: 采集关闭时为 None
    presence: ReadablePresence | None = None
    tasks: list[TaskBrief] = Field(default_factory=list)


class ProactiveReceipt(BaseModel):
    """主动消息回执：被拒也不静默丢弃。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    accepted: bool
    reason: (
        Literal["ok", "too_frequent", "daily_limit", "hourly_limit", "quiet_hours", "user_active"]
        | None
    ) = None
    #: 延迟投递时间
    deliver_after: datetime | None = None


class RedactionSpan(BaseModel):
    """一处命中：只记位置与动作，不记原文。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    rule: str
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    action: Literal["mask", "flag"] = "mask"
    replacement: str = ""


class RedactionResult(BaseModel):
    """脱敏扫描结果：返回可发送文本与命中位置，不记录原文。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    hit: bool = False
    rules: list[str] = Field(default_factory=list)
    #: 已按规则遮蔽后的文本；无命中时等于原文
    text: str = ""
    #: 命中位置基于原始文本，回调方按需做偏移换算
    spans: list[RedactionSpan] = Field(default_factory=list)


class CosmosEvent(BaseModel):
    """事件总线上的统一事件。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    event_id: str
    topic: str
    sequence: int
    at: datetime
    payload: dict[str, object] = Field(default_factory=dict)
