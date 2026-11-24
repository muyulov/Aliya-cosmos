"""世界推理契约（设计 07 章）。

世界推理 agent 是参谋不是执行者：输入是聚合信号（不含用户消息原文），
输出是带置信度与依据的可能性，不构成指令。
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

#: 时段
type DayPeriod = Literal["morning", "afternoon", "evening", "night", "unknown"]

#: 链路状态
type WorldLinkState = Literal["up", "unstable", "down"]

#: 世界推理给出的行动类型：只描述「做什么」，执行由 Aliya 决定
type ActionKind = Literal["wait", "observe", "check_in", "offer_help", "notify", "act"]


class TimeSignal(BaseModel):
    """时间信号：一律取 ClockService.now()。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    now: datetime
    timezone: str
    period: DayPeriod = "unknown"
    since_last_user_message_seconds: int | None = None
    since_last_aliya_message_seconds: int | None = None
    session_duration_seconds: int | None = None


class LinkSignal(BaseModel):
    """通讯链路信号。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    state: WorldLinkState = "up"
    since_minutes: int | None = None
    queued_user_messages: int = Field(default=0, ge=0)
    queued_aliya_messages: int = Field(default=0, ge=0)


class PresenceSignal(BaseModel):
    """扁平化后的用户状态信号。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    online_state: Literal["online", "away", "busy", "offline", "unknown"] = "unknown"
    online_since_minutes: int | None = None
    silence_minutes: int | None = None
    is_typing: bool = False
    typing_paused_seconds: int | None = None
    messages_last_15m: int = 0
    mood_valence: float | None = None
    mood_energy: float | None = None
    mood_confidence: float | None = None
    awaiting_result: bool = False
    pending_count: int = 0
    device: str | None = None
    #: 哪些字段拿不到，推理时要考虑不确定性
    unavailable_fields: list[str] = Field(default_factory=list)


class TaskSignal(BaseModel):
    """一条正在执行的任务信号（不含 task_id）。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    label: str
    agent_name: str
    elapsed_seconds: float = 0.0
    progress: float | None = None


class OutcomeSignal(BaseModel):
    """最近一次任务结果信号。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    status: Literal["ok", "partial", "failed", "cancelled"]
    finished_minutes_ago: int = Field(default=0, ge=0)
    elapsed_seconds: float = 0.0


class TaskSignals(BaseModel):
    """任务聚合信号。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    running: list[TaskSignal] = Field(default_factory=list)
    queued: int = Field(default=0, ge=0)
    recent_outcomes: list[OutcomeSignal] = Field(default_factory=list)


class AgentSignals(BaseModel):
    """工具聚合信号：只给数量与异常名单。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    total: int = 0
    idle: int = 0
    busy: int = 0
    abnormal: int = 0
    abnormal_agents: list[str] = Field(default_factory=list)


class HistoryPattern(BaseModel):
    """历史模式：样本天数不足时结论不可用。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    active_hours: list[int] = Field(default_factory=list)
    typical_session_minutes: int | None = None
    response_latency_p50_seconds: int | None = None
    proactive_sent: int = 0
    proactive_replied: int = 0
    last_proactive_minutes_ago: int | None = None
    sample_days: int = 0


class BeliefState(BaseModel):
    """对用户潜在状态的信念：只存概率，不存心理诊断标签。

    `uncertainty` 越高，行动选择应越偏向「先问清楚」而不是「直接推进」。
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    online_prob: float = Field(default=0.5, ge=0, le=1)
    engagement: float = Field(default=0.5, ge=0, le=1)
    readiness: float = Field(default=0.5, ge=0, le=1)
    uncertainty: float = Field(default=0.5, ge=0, le=1)
    evidence_count: int = Field(default=0, ge=0)
    updated_at: datetime | None = None


class WorldInferenceDigest(BaseModel):
    """上一轮推演的摘要：只给结论，不给全文，避免上下文无限膨胀。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    inference_id: str
    revision: int = 0
    generated_at: datetime
    snapshot: str
    confidence: float = Field(default=0.0, ge=0, le=1)
    signals_used: list[str] = Field(default_factory=list)


class AliyaContext(BaseModel):
    """Aliya 主动提供的上下文：cosmos 只存不解读。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    relation_stage: str | None = None
    open_topics: list[str] = Field(default_factory=list)
    unspoken: list[str] = Field(default_factory=list)
    phase: Literal["daily", "maintenance", "crisis", "rest", "unknown"] = "unknown"
    last_intent: str | None = None


class WorldInput(BaseModel):
    """喂给世界推理的全部信号。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    generated_at: datetime
    time: TimeSignal
    link: LinkSignal = LinkSignal()
    #: 采集关闭时为 None，不是空对象
    presence: PresenceSignal | None = None
    tasks: TaskSignals = TaskSignals()
    agents: AgentSignals = AgentSignals()
    history: HistoryPattern | None = None
    aliya_context: AliyaContext | None = None
    #: 上一轮维护的信念；冷启动为 None
    belief: BeliefState | None = None
    #: 上一轮推演摘要，供「基于旧结论更新」而不是每次从零推断
    last_inference: WorldInferenceDigest | None = None
    #: 供推理侧锚定的实体短语（任务名、工具名、最近话题），不含消息原文
    anchors: list[str] = Field(default_factory=list)


class Hypothesis(BaseModel):
    """一条带依据的可能性。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    statement: str
    confidence: float = Field(ge=0, le=1)
    basis: str
    #: 这条假设用到了哪些输入信号；cosmos 侧会校验它必须落在 signals_used 内
    basis_signals: list[str] = Field(default_factory=list)


class ActionWindow(BaseModel):
    """行动窗口：给时机与方向，不下指令。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    window: str
    suggestion: str
    rationale: str
    #: 行动类型；端口不填时由 cosmos 按 suggestion 关键词推断
    kind: ActionKind = "wait"
    #: 期望价值：置信度 × 紧迫度 × 可打扰程度，由 cosmos 侧计算
    expected_value: float | None = Field(default=None, ge=0, le=1)


class ActionChoice(BaseModel):
    """一步 EFE 选择结果：给 Aliya 一个「现在最值得做什么」的排序依据。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    action: ActionKind
    window: str | None = None
    expected_value: float = Field(default=0.0, ge=0, le=1)
    #: 实用价值：推进目标 / 满足当前需求
    pragmatic: float = Field(default=0.0, ge=0, le=1)
    #: 认识价值：降低不确定性的信息增益
    epistemic: float = Field(default=0.0, ge=0, le=1)
    #: 打扰 / 误判成本
    cost: float = Field(default=0.0, ge=0, le=1)
    #: 一步 expected free energy：实用 + 认识 - 成本
    efe: float = Field(default=0.0, ge=-1, le=1)
    #: 有限时域前瞻的折扣后价值
    lookahead: float = Field(default=0.0, ge=-1, le=1)
    #: 选择依据：`efe + discount × lookahead`
    total_efe: float = Field(default=0.0, ge=-1, le=1)
    rationale: str = ""


class WorldInference(BaseModel):
    """世界推理结果。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    inference_id: str
    generated_at: datetime
    ttl_seconds: int = Field(default=600, ge=0)
    snapshot: str
    #: 1–3 条；不足由推理侧补「无明显信号」，超过截断并 WARNING
    hypotheses: list[Hypothesis] = Field(min_length=1, max_length=3)
    action_windows: list[ActionWindow] = Field(default_factory=list, max_length=3)
    signals_used: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0, le=1)
    #: cosmos 侧标记，不由推理侧写
    stale: bool = False
    #: 单调递增：快路与慢路、并发推演都靠它排序
    revision: int = Field(default=0, ge=0)
    #: 快路（规则）还是慢路（深度端口）
    producer: Literal["heuristic", "deep"] = "deep"
    #: 置信度分解：time / presence / tasks / history / mood / model
    confidence_breakdown: dict[str, float] = Field(default_factory=dict)
    #: 本轮更新后的信念
    belief: BeliefState | None = None
    #: 信念各维度的变化量
    belief_delta: dict[str, float] = Field(default_factory=dict)
    #: 上一轮预测与本轮观测的误差（0–1）
    prediction_error: float | None = Field(default=None, ge=0, le=1)
    #: 紧迫度与可打扰程度：给终端做打扰预算
    urgency: float = Field(default=0.0, ge=0, le=1)
    interruptibility: float | None = Field(default=None, ge=0, le=1)
    expected_value: float | None = Field(default=None, ge=0, le=1)
    #: 一步 EFE 选出的行动；没有可用候选时为 None
    action_choice: ActionChoice | None = None


class WorldInferenceEvent(BaseModel):
    """`world.inference` 事件载荷。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    inference: WorldInference
    stale: bool = False
    #: 距生成时间
    age_seconds: float = Field(default=0.0, ge=0)
    trigger: Literal[
        "tick", "presence", "dispatch", "silence", "manual", "user_message", "mood", "link"
    ] = "tick"
