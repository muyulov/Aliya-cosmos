"""配置模块。

约定：
- data/config/cosmos.yaml 是配置的唯一骨架，字段缺失时用这里的默认值。
- 需要密钥等隐私值时不写明文，在 YAML 里写 ${VAR} 占位符，值由 .env 提供。
- 通过 get_settings() 获取单例；测试中可用 get_settings.cache_clear() 重置，
  或用 load_settings(config_file=..., env_file=...) 显式指定文件。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import ClassVar, Literal, cast

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from core.config.loader import CONFIG_FILE, ENV_FILE, interpolate, read_env, read_yaml

Environment = Literal["dev", "test", "prod"]

#: 日志级别的合法取值：与 formatters 里 LEVEL_TAGS / LEVEL_COLORS 认识的那几个一致。
#: 写错级别在配置加载期就被 pydantic 拦下（ValidationError），
#: 而不是等到 setup_logging 交给 loguru 时才抛裸 ValueError。
LogLevel = Literal["TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"]

#: 未找到 YAML 骨架文件时的来源描述前缀
SOURCE_DEFAULT = "内置默认值"
#: 直接构造实例（测试或调用方注入）时的来源描述
SOURCE_INJECTED = "外部注入"


class AppSettings(BaseModel):
    """应用层配置。"""

    app_name: str = "Aliya-cosmos"
    env: Environment = "dev"
    debug: bool = True


class LogSettings(BaseModel):
    """日志层配置。

    注意 json 字段用别名 json_output：json 与 pydantic 自带的序列化方法撞名，
    直接命名 json 会冲突。YAML 里写 json，代码里读 json_output。

    - `layout`：单行（默认，字段内联成 `键=值`，便于 grep 与按行采集）或树形
      （字段缩进成树，人读更清楚）。
    - `color`：仅控制台生效，文件永远不带 ANSI 转义；`auto` 仅在终端下着色。
    - `source`：是否在每条日志里带上调用点 `文件名:行号`。默认开启——定位问题时
      不必再从消息反推是哪一行打的；压日志体积或对接固定格式采集时关掉。
    - `file_name` / `error_file_name`：支持 loguru 的时间模板，启动时间戳在装配时
      求值，因此每次启动都写到独立文件；模板里的 `{}` 同时让 loguru 的 retention
      能跨启动识别同族文件（写死文件名则只能清理本进程自己轮转出的小文件）。
      两个名字的前缀**不能互为包含关系**：装配后要按模板反查目录定位文件，主日志的
      glob 会把「以它为前缀」的错误日志一起匹配进来（`cosmos-*` 命中 `cosmos-error-*`）。
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(populate_by_name=True)

    level: LogLevel = "INFO"
    json_output: bool = Field(default=False, alias="json")
    layout: Literal["tree", "line"] = "line"
    color: Literal["auto", "always", "never"] = "auto"
    source: bool = True
    dir: str = "logs"
    file_name: str = "cosmos-main-{time:%Y%m%d-%H%M%S}.log"
    error_file_name: str = "cosmos-error-{time:%Y%m%d-%H%M%S}.log"
    rotation: str = "00:00"
    retention: str = "7 days"
    compression: str = "zip"

    @field_validator("level", mode="before")
    @classmethod
    def _normalize_level(cls, value: object) -> object:
        """级别大小写不敏感：YAML 里写 info / Info 都先归一成 INFO 再校验。"""
        return value.upper() if isinstance(value, str) else value


class ServiceSettings(BaseModel):
    """服务生命周期配置。"""

    start_timeout: float | None = Field(default=30.0, gt=0)  # 秒；None 表示不限制
    stop_timeout: float | None = Field(default=30.0, gt=0)


class ClockSettings(BaseModel):
    """时钟服务配置。"""

    tz: str = "UTC"


class EmbeddingSettings(BaseModel):
    """文本向量化端点配置。

    与 llm 分开配：DeepSeek 没有 embedding 端点，两家通常不是同一个服务。
    `dimensions` 为 None 表示用模型原始维度，设值则请求截断（做不到的模型会报错）。
    """

    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    api_key: str = ""  # YAML 里写 ${DASHSCOPE_API_KEY:}，留空表示该端点未启用
    model: str = "text-embedding-v3"
    timeout: float = Field(default=60.0, gt=0)  # 单次请求超时（秒）
    retries: int = Field(default=2, ge=0)  # SDK 重试次数，0 关闭
    batch_size: int = Field(default=10, gt=0)  # 单次请求最多几条文本
    dimensions: int | None = Field(default=None, gt=0)  # None = 用模型原始维度；设值则请求截断


class LLMEndpointSettings(BaseModel):
    """一个 LLM 端点的配置：连接参数与采样参数都归端点自己。

    `temperature` / `max_tokens` 为 None 表示「不传该参数」，由服务端决定取值；
    传给 SDK 前会在调用边界换成 omit（显式 None 会被序列化成 JSON null）。
    """

    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""  # YAML 里写 ${XXX_API_KEY:}，留空表示该端点未启用
    model: str = "gpt-4o-mini"
    timeout: float = Field(default=60.0, gt=0)  # 单次请求超时（秒）
    retries: int = Field(default=2, ge=0)  # SDK 重试次数，0 关闭
    structured_mode: Literal["json_schema", "json_object"] = "json_schema"
    temperature: float | None = None
    max_tokens: int | None = None


class LLMSettings(BaseModel):
    """LLM 层配置：chat 与 vision 各是一个端点。

    两个头可以指向同一个服务（多数兼容端点如此），也可以分开接不同供应商。
    走哪个端点由调用时的 `endpoint=` 显式决定（不给走 chat），配置本身不参与路由。
    """

    chat: LLMEndpointSettings = LLMEndpointSettings()
    vision: LLMEndpointSettings = LLMEndpointSettings()


#: 判断层的两个端点名。laya 是本地 laya-serve，jev 是 TypeSafe 云端。
type Backend = Literal["jev", "laya"]


class DecisionEndpointSettings(BaseModel):
    """一个判断端点的配置。

    `model` 为空表示「请求体里不带 model 字段」：Laya 靠这个走 Router 自动路由，
    Jev 则会 422（它的 model 是必填）。传空串或 null 都不等于「不传」。

    `base_url` / `model` 的默认值由 `DecisionSettings` 按端点给（jev 与 laya 不同），
    别拿裸的 `DecisionEndpointSettings()` 去建客户端。
    """

    enabled: bool = False
    base_url: str = ""
    api_key: str = ""  # 留空则不发 Authorization 头（laya-serve 默认不要求认证）
    model: str = ""
    timeout: float = Field(default=60.0, gt=0)  # 单次请求超时（秒）
    retries: int = Field(default=2, ge=0)  # 429/502/503/504/529 退避重试次数，0 关闭
    #: 连接池上限：并发 predict / predict_many 时压住对端点的连接数；None 用 httpx2 默认
    max_connections: int | None = Field(default=None, gt=0)


class DecisionSettings(BaseModel):
    """判断层配置：jev 云端与 laya 本地是两个端点，字段相同、协议相同。"""

    jev: DecisionEndpointSettings = DecisionEndpointSettings(
        base_url="https://api.typesafe.ai/v1", model="jev-latest"
    )
    laya: DecisionEndpointSettings = DecisionEndpointSettings(
        base_url="http://127.0.0.1:8000/v1", model=""
    )


def _warn_unknown_keys(model: type[BaseModel], data: dict[str, object]) -> None:
    """把输入里模型不认识的键打成 WARNING。

    只在 schema 层对账：值本身交给 pydantic 校验。
    刻意用 loguru 而不是 `core.logger` 的门面：后者依赖本模块，走它成环。
    """
    known: set[str] = set(model.model_fields)
    unknown: list[str] = sorted(key for key in data if key not in known)
    if not unknown:
        return
    logger.warning(
        "配置含未知键，已忽略 | 配置节点={} 未知键={}",
        model.__name__,
        "、".join(unknown),
    )


class _AgentConfigNode(BaseModel):
    """agent 配置节点基类：未知键 WARNING 后忽略，不阻断加载。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _check_unknown(cls, data: object) -> object:
        if isinstance(data, dict):
            _warn_unknown_keys(cls, cast("dict[str, object]", data))
        # 显式 cast：`isinstance` 收窄后剩余分支的类型对检查器不透明
        return cast("object", data)


class AgentContractSettings(_AgentConfigNode):
    """契约版本与运行数据目录。"""

    api_version: str = "1.0"
    contract_version: str = "1.0.0"
    state_dir: str = "data/state"


class AgentLinkSettings(_AgentConfigNode):
    """通讯链路（01 章 3.1）：心跳、断链判定与中断期队列。"""

    heartbeat_seconds: int = Field(default=30, gt=0)
    down_after_seconds: int = Field(default=90, gt=0)
    queue_limit: int = Field(default=1000, ge=1)
    replay_on_recover: bool = True
    suppress_proactive_when_down: bool = True


class AgentDispatchSettings(_AgentConfigNode):
    """调度（05 章）：检索阈值、精排、并发与幂等窗口。"""

    mode: Literal["suggest", "auto"] = "suggest"
    auto_threshold: float = Field(default=0.75, ge=0, le=1)
    min_confidence: float = Field(default=0.6, ge=0, le=1)
    high_threshold: float = Field(default=0.9, ge=0, le=1)
    max_candidates: int = Field(default=5, ge=1)
    max_parallel: int = Field(default=3, ge=1)
    dedupe_window_seconds: int = Field(default=60, ge=0)
    plan_timeout_seconds: float = Field(default=5.0, gt=0)
    match_fallback_on_error: bool = True


class AgentDesktopGroupSettings(_AgentConfigNode):
    """桌面分组声明。"""

    key: str
    title: str
    order: int = 100


class AgentDesktopSettings(_AgentConfigNode):
    """桌面（03 章）：分组、节流、对账与文本上限。"""

    groups: list[AgentDesktopGroupSettings] = Field(
        default_factory=lambda: [
            AgentDesktopGroupSettings(key="dev", title="开发工具", order=10),
            AgentDesktopGroupSettings(key="media", title="内容处理", order=20),
            AgentDesktopGroupSettings(key="general", title="通用", order=90),
        ]
    )
    progress_throttle_ms: int = Field(default=1000, ge=0)
    reconcile_seconds: int = Field(default=30, gt=0)
    max_content_chars: int = Field(default=2000, gt=0)
    history_limit: int = Field(default=5, ge=1)


class AgentPresenceFieldSettings(_AgentConfigNode):
    """六类用户状态字段的采集开关。"""

    online: bool = True
    activity: bool = True
    typing: bool = True
    mood: bool = True
    task_context: bool = True
    env: bool = True


class AgentMoodSettings(_AgentConfigNode):
    """情绪信号（04 章 2.4）。"""

    provider: Literal["decision", "llm", "off"] = "decision"
    backend: Backend = "laya"
    min_confidence: float = Field(default=0.5, ge=0, le=1)
    stale_after_seconds: int = Field(default=1800, gt=0)
    delta_threshold: float = Field(default=0.25, ge=0, le=1)
    window_messages: int = Field(default=5, ge=1)


class AgentPresencePushSettings(_AgentConfigNode):
    """状态变化推送的去抖间隔。"""

    min_interval_seconds: float = Field(default=5.0, ge=0)
    typing_settle_seconds: float = Field(default=3.0, ge=0)
    mood_min_interval_seconds: float = Field(default=300.0, ge=0)


class AgentPresencePanelSettings(_AgentConfigNode):
    """常驻面板的展示边界（04 章 3.4）。"""

    show_mood: bool = False
    show_device: bool = False
    aliya_can_hide: bool = True
    collapsed_fields: list[str] = Field(default_factory=lambda: ["mood", "env.device"])


class AgentPresencePrivacySettings(_AgentConfigNode):
    """隐私边界（04 章 5）。"""

    allow_timezone: bool = True
    allow_device: bool = True
    retain_raw_hours: int = Field(default=24, ge=0)
    retain_aggregate_days: int = Field(default=30, ge=0)
    expose_to_subagents: bool = False
    never_echo_to_user: bool = True
    address_as: str = "你"

    @field_validator("address_as")
    @classmethod
    def _only_you(cls, value: str) -> str:
        """对 Aliya 的呈现里对端只能叫「你」，写别的值回落并告警。"""
        if value == "你":
            return value
        logger.warning("对端称呼只允许「你」，已回落 | 配置值={}", value)
        return "你"


class AgentPresenceWordingSettings(_AgentConfigNode):
    """可解读表述的文案覆盖；None 用内置默认。"""

    online_idle_template: str | None = None
    offline_night_template: str | None = None
    unknown_summary: str = "用户状态未知"


class AgentPresenceSettings(_AgentConfigNode):
    """用户状态线索（04 章）。"""

    enabled: bool = True
    ttl_seconds: int = Field(default=30, ge=0)
    fields: AgentPresenceFieldSettings = AgentPresenceFieldSettings()
    idle_after_seconds: int = Field(default=300, gt=0)
    offline_after_seconds: int = Field(default=900, gt=0)
    mood: AgentMoodSettings = AgentMoodSettings()
    push: AgentPresencePushSettings = AgentPresencePushSettings()
    panel: AgentPresencePanelSettings = AgentPresencePanelSettings()
    privacy: AgentPresencePrivacySettings = AgentPresencePrivacySettings()
    wording: AgentPresenceWordingSettings = AgentPresenceWordingSettings()


class AgentRedactionSettings(_AgentConfigNode):
    """脱敏清单（06 章 6.3）。"""

    mask_agent_ids: bool = True
    mask_task_ids: bool = True
    mask_dispatch_ids: bool = True
    mask_error_types: bool = True
    mask_stack: bool = True
    mask_secrets: bool = True
    mask_pii: bool = True
    warn_on_hit: bool = True


class AgentProactiveSettings(_AgentConfigNode):
    """主动消息节流（06 章 7.3）。"""

    enabled: bool = True
    min_interval_seconds: int = Field(default=3600, ge=0)
    daily_limit: int = Field(default=6, ge=0)
    quiet_hours: list[str] = Field(default_factory=list)
    unanswered_backoff: bool = True
    max_backoff_multiplier: int = Field(default=4, ge=1)
    cancel_window_seconds: float = Field(default=5.0, ge=0)
    input_delay_seconds: float = Field(default=10.0, ge=0)
    #: 每小时打扰预算；urgency 高时仍受每日上限约束
    hourly_limit: int = Field(default=2, ge=0)
    #: 历史画像窗口与主动消息回复判定
    history_window_days: int = Field(default=14, ge=1)
    reply_window_minutes: int = Field(default=30, ge=1)


class AgentDeliverySettings(_AgentConfigNode):
    """出站投递失败的重试与死信。"""

    retry: int = Field(default=3, ge=0)
    retry_backoff_seconds: float = Field(default=2.0, ge=0)
    dead_letter: bool = True


class AgentWebSocketChannelSettings(_AgentConfigNode):
    """浏览器/用户侧 WebSocket 终端通道（06 章）。"""

    enabled: bool = False
    #: 默认只监听回环地址；绑定其它地址时必须配置令牌
    host: str = "127.0.0.1"
    #: 0 表示由系统分配端口（测试用）；生产环境请显式指定
    port: int = Field(default=8780, ge=0, le=65535)
    path: str = "/ws"
    #: 访问令牌所在环境变量；留空表示不校验
    token_env: str = ""
    #: 单客户端出站队列上限；满了丢最旧并告警
    queue_limit: int = Field(default=200, ge=1)
    #: 客户端未声明 session_id 时使用的会话标识
    default_session_id: str = "web"
    #: 握手后补发的历史条数；0 关闭
    history_limit: int = Field(default=20, ge=0, le=200)


class AgentTerminalSettings(_AgentConfigNode):
    """通信终端（06 章）。"""

    conversation_turns: int = Field(default=40, ge=1)
    summary_chars: int = Field(default=800, ge=0)
    #: 上下文预算与压缩策略
    context_max_tokens: int = Field(default=16000, ge=256)
    summary_batch_turns: int = Field(default=4, ge=1)
    session_ttl_hours: int = Field(default=72, ge=1)
    max_sessions: int = Field(default=200, ge=1)
    pinned_fact_ttl_days: int = Field(default=30, ge=1)
    #: 单条回复的建议分段上限；超出只告警，仍完整投递
    max_parts: int = Field(default=24, ge=1)
    #: 代码块/URL 的硬上限；超过才允许继续切
    segment_hard_max_chars: int = Field(default=8000, ge=100)
    #: 是否向 Aliya 暴露 unanswered_count；关闭时传 0
    unanswered_notice: bool = True
    delivery: AgentDeliverySettings = AgentDeliverySettings()
    redaction: AgentRedactionSettings = AgentRedactionSettings()
    proactive: AgentProactiveSettings = AgentProactiveSettings()
    ws: AgentWebSocketChannelSettings = AgentWebSocketChannelSettings()


class AgentWorldHistorySettings(_AgentConfigNode):
    """历史模式统计窗口（07 章 3.5）。"""

    window_days: int = Field(default=14, ge=1)
    min_sample_days: int = Field(default=3, ge=0)


class AgentWorldTriggerSettings(_AgentConfigNode):
    """世界推理的触发条件（07 章 5）。"""

    presence_change: bool = True
    dispatch_finished: bool = True
    silence_thresholds_minutes: list[int] = Field(default_factory=lambda: [30, 120, 360])
    user_message: bool = False
    #: 显著性门控：低于阈值的普通事件不触发推演
    min_salience: float = Field(default=0.35, ge=0, le=1)
    #: 高显著事件用短窗口，低显著事件用长窗口
    high_salience_window_seconds: float = Field(default=30.0, ge=0)
    low_salience_window_seconds: float = Field(default=300.0, ge=0)
    #: 定时对账周期：只兜底，不承担主触发
    reconcile_seconds: int = Field(default=600, gt=0)
    #: 轻量触发分类器权重（JSON）；为空时用规则打分
    model_path: str | None = None


class AgentWorldBeliefSettings(_AgentConfigNode):
    """信念状态更新（07 章 6）。"""

    enabled: bool = True
    #: 新观测与旧信念的混合权重：越大越相信本轮观测
    observation_weight: float = Field(default=0.6, ge=0, le=1)


class AgentWorldPolicySettings(_AgentConfigNode):
    """有限时域 EFE 的前瞻深度与折扣。"""

    horizon: int = Field(default=2, ge=1, le=3)
    discount: float = Field(default=0.8, ge=0, le=1)


class AgentWorldCalibrationSettings(_AgentConfigNode):
    """置信度校准与证据接地（07 章 6.2）。"""

    temperature: float = Field(default=1.5, gt=0)
    model_weight: float = Field(default=0.45, ge=0, le=1)
    evidence_weight: float = Field(default=0.55, ge=0, le=1)
    #: 低于该值的假设不进入 Aliya 的决策输入
    min_hypothesis_confidence: float = Field(default=0.2, ge=0, le=1)
    #: 校准报告路径；设置后启动时自动回填 temperature / min_salience
    report_path: str | None = None


class AgentWorldLLMSettings(_AgentConfigNode):
    """世界推理的模型参数；端点与密钥复用全局 `llm.chat`。"""

    #: None 表示沿用 `llm.chat.model`
    model: str | None = None
    #: None 表示跟随端点默认值
    temperature: float | None = Field(default=0.4, ge=0, le=2)
    max_tokens: int | None = Field(default=1200, ge=1)


class AgentWorldSettings(_AgentConfigNode):
    """世界推理桥（07 章）。"""

    enabled: bool = False
    #: 需要覆盖「首次调用 + 一次修复重试」；太短会让修复永远跑不完
    infer_timeout_seconds: float = Field(default=30.0, gt=0)
    ttl_seconds: int = Field(default=600, ge=0)
    failure_backoff_seconds: int = Field(default=300, ge=0)
    max_hypotheses: int = Field(default=3, ge=1, le=3)
    llm: AgentWorldLLMSettings = AgentWorldLLMSettings()
    history: AgentWorldHistorySettings = AgentWorldHistorySettings()
    triggers: AgentWorldTriggerSettings = AgentWorldTriggerSettings()
    belief: AgentWorldBeliefSettings = AgentWorldBeliefSettings()
    policy: AgentWorldPolicySettings = AgentWorldPolicySettings()
    calibration: AgentWorldCalibrationSettings = AgentWorldCalibrationSettings()
    #: 推演轨迹：输入指纹、触发原因、结论与置信度，供离线回放/调参
    trace_enabled: bool = True
    trace_max_bytes: int = Field(default=5_000_000, ge=0)


class AgentExampleConfig(_AgentConfigNode):
    """卡片示例请求的 YAML 形态。"""

    input: str
    note: str | None = None


class AgentDesktopMetaConfig(_AgentConfigNode):
    """卡片桌面元数据的 YAML 形态。"""

    group: str = "general"
    order: int = 100
    visible: bool = True
    pinned: bool = False
    badge: str | None = None


class AgentExecutionConfig(_AgentConfigNode):
    """卡片执行策略的 YAML 形态。"""

    timeout_seconds: float = Field(default=120.0, gt=0)
    max_concurrent: int = Field(default=1, ge=1)
    queue: bool = False
    cancel_on_supersede: bool = False
    retryable: bool = False
    heartbeat_seconds: float = Field(default=30.0, gt=0)


class AgentPermissionsConfig(_AgentConfigNode):
    """卡片权限的 YAML 形态。"""

    read_presence: bool = False
    presence_fields: list[str] = Field(default_factory=list)
    notify_user: bool = False
    read_conversation: bool = False


class AgentCardConfig(_AgentConfigNode):
    """`AgentCard` 的 YAML 形态。

    刻意不引用 `agent.cosmos.contracts`：`core` 不能反向依赖 `agent`。
    字段与契约模型一一对应，由 `agent.cosmos.registry.bootstrap` 转成 `AgentCard`。
    """

    name: str = ""
    version: str = "0.1.0"
    icon: str = "🔧"
    summary: str = ""
    description: str = ""
    capabilities: list[str] = Field(default_factory=list)
    params_schema: dict[str, object] = Field(default_factory=lambda: {"type": "object"})
    examples: list[AgentExampleConfig] = Field(default_factory=list)
    desktop: AgentDesktopMetaConfig = AgentDesktopMetaConfig()
    execution: AgentExecutionConfig = AgentExecutionConfig()
    permissions: AgentPermissionsConfig = AgentPermissionsConfig()


class AgentRegistrationSettings(_AgentConfigNode):
    """一个待注册子 agent：模块路径 + handler 名 + 卡片。"""

    id: str
    module: str
    handler: str = "handle"
    card: AgentCardConfig = AgentCardConfig()


class AgentCosmosSettings(_AgentConfigNode):
    """cosmos 主 agent 的完整配置。"""

    contract: AgentContractSettings = AgentContractSettings()
    link: AgentLinkSettings = AgentLinkSettings()
    dispatch: AgentDispatchSettings = AgentDispatchSettings()
    desktop: AgentDesktopSettings = AgentDesktopSettings()
    presence: AgentPresenceSettings = AgentPresenceSettings()
    terminal: AgentTerminalSettings = AgentTerminalSettings()
    world: AgentWorldSettings = AgentWorldSettings()
    allow_runtime_registration: bool = False
    agents: list[AgentRegistrationSettings] = Field(default_factory=list)


class AgentControlSettings(_AgentConfigNode):
    """控制面共享配置：服务端与客户端读同一份。"""

    socket_path: str = "data/state/aliya.sock"
    #: 存放控制面令牌的环境变量名；令牌缺失时服务端拒绝启动
    token_env: str = "ALIYA_CONTROL_TOKEN"
    #: 当前实现按单客户端设计：大于 1 时入站消息会广播给所有会话
    max_clients: int = Field(default=1, ge=1)
    request_timeout: float = Field(default=15, gt=0)
    outbox_limit: int = Field(default=200, ge=1)
    max_line_bytes: int = Field(default=1_048_576, ge=1024)


class AgentAliyaPersonaSettings(_AgentConfigNode):
    source: str = "data/prompt/Aliya.md"
    charter: str = "data/prompt/Aliya.charter.md"
    includes: list[str] = Field(default_factory=list)


class AgentAliyaMemorySettings(_AgentConfigNode):
    enabled: bool = True
    dir: str = "data/Aliya"
    max_turns: int = Field(default=200, ge=1)
    summary_chars: int = Field(default=600, ge=100)
    open_topics_limit: int = Field(default=5, ge=1)
    anchors: list[str] = Field(default_factory=list)
    private_threads: bool = True


class AgentAliyaInitiativeSettings(_AgentConfigNode):
    enabled: bool = True
    tick_seconds: float = Field(default=60, gt=0)
    min_gap_seconds: float = Field(default=3600, ge=0)
    daily_soft_limit: int = Field(default=4, ge=0)
    prefer_when: list[str] = Field(default_factory=list)
    respect_presence_hint: bool = True
    respect_link_state: bool = True
    default_action: Literal["wait", "speak"] = "wait"


class AgentAliyaReplySettings(_AgentConfigNode):
    max_rounds: int = Field(default=6, ge=1)
    turn_timeout_seconds: float = Field(default=120, gt=0)
    #: 单条出站消息的字符上限；0 表示不截断（默认），由通道分段处理
    max_chars_per_message: int = Field(default=0, ge=0)
    #: 用户回合失败时的系统提示；以 system 消息投递，不算 Aliya 的回复
    fallback_text: str = "回复失败，请稍后再试。"
    progress_forwarding: Literal["always", "on_wait", "never"] = "on_wait"
    post_checks: list[str] = Field(
        default_factory=lambda: [
            "narration",
            "engineering_terms",
            "internal_ids",
            "presence_leak",
        ]
    )


class AgentAliyaToolsSettings(_AgentConfigNode):
    prefer_agents: list[str] = Field(default_factory=list)
    ask_before_write: bool = True
    max_parallel_dispatch: int = Field(default=2, ge=1)
    #: 接入 data/mcp.json 的外部 MCP 工具；空配置不连任何 server
    mcp_enabled: bool = True
    #: 加载 data/skills 下的技能索引；正文由 load_skill 按需读取
    skills_enabled: bool = True


class AgentAliyaWorldSettings(_AgentConfigNode):
    consume: bool = True
    max_hypotheses_used: int = Field(default=2, ge=0)
    trust_threshold: float = Field(default=0.5, ge=0, le=1)
    provide_context: bool = True


class AgentAliyaSettings(_AgentConfigNode):
    persona: AgentAliyaPersonaSettings = AgentAliyaPersonaSettings()
    memory: AgentAliyaMemorySettings = AgentAliyaMemorySettings()
    initiative: AgentAliyaInitiativeSettings = AgentAliyaInitiativeSettings()
    reply: AgentAliyaReplySettings = AgentAliyaReplySettings()
    tools: AgentAliyaToolsSettings = AgentAliyaToolsSettings()
    world: AgentAliyaWorldSettings = AgentAliyaWorldSettings()


class AgentSettings(_AgentConfigNode):
    """agent 层配置的顶层节点（挂在 Settings.agent 上）。"""

    control: AgentControlSettings = AgentControlSettings()
    aliya: AgentAliyaSettings = AgentAliyaSettings()
    cosmos: AgentCosmosSettings = AgentCosmosSettings()


class Settings(BaseModel):
    """顶层配置，按子模块分组，避免一个扁平大类。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", populate_by_name=True)

    app: AppSettings = AppSettings()
    log: LogSettings = LogSettings()
    service: ServiceSettings = ServiceSettings()
    clock: ClockSettings = ClockSettings()
    embedding: EmbeddingSettings = EmbeddingSettings()
    llm: LLMSettings = LLMSettings()
    decision: DecisionSettings = DecisionSettings()
    agent: AgentSettings = AgentSettings()

    #: 本次配置的来源；由 load_settings() 写入，直接构造实例时保持 None
    _source: str | None = PrivateAttr(default=None)

    @property
    def config_source(self) -> str:
        """本次配置的来源，供启动日志展示。"""
        return self._source or SOURCE_INJECTED

    def mark_source(self, source: str) -> None:
        """记录配置来源。由 load_settings() 在装配完成后调用。"""
        self._source = source


def _describe_source(config_file: Path, raw: dict[str, object] | None) -> str:
    """把「文件是否存在、是否为空」翻译成启动日志里的一句话。"""
    if raw is None:
        return f"{SOURCE_DEFAULT}（未找到 {config_file}）"
    if not raw:
        return f"{config_file}（空文件，全部走默认值）"
    return str(config_file)


def load_settings(
    config_file: Path = CONFIG_FILE,
    env_file: Path = ENV_FILE,
    *,
    expand_rounds: int = 1,
) -> Settings:
    """加载配置：读 YAML → 占位符插值 → 校验，并记录配置来源。

    文件缺失不报错（全部走默认值），占位符取不到值则抛 ConfigError。
    `expand_rounds` 透传给 interpolate：默认单趟，需要链式占位符时传 2 及以上。
    """
    raw = read_yaml(config_file)
    data = interpolate(raw if raw is not None else {}, read_env(env_file), rounds=expand_rounds)
    settings = Settings.model_validate(data)
    settings.mark_source(_describe_source(config_file, raw))
    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取配置单例。"""
    return load_settings()
