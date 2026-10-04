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
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator

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

    app_name: str = "aliya-cosmos"
    env: Environment = "dev"
    debug: bool = True


class LogSettings(BaseModel):
    """日志层配置。

    注意 json 字段用别名 json_output：json 与 pydantic 自带的序列化方法撞名，
    直接命名 json 会冲突。YAML 里写 json，代码里读 json_output。

    - `layout`：单行（默认，字段内联成 `键=值`，便于 grep 与按行采集）或树形
      （字段缩进成树，人读更清楚）。
    - `color`：仅控制台生效，文件永远不带 ANSI 转义；`auto` 仅在终端下着色。
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
    """LLM 层配置：chat、vision 与 memory 各是一个端点。

    三个头可以指向同一个服务（多数兼容端点如此），也可以分开接不同供应商。

    memory 端点有特殊约定：它只接受显式 `endpoint="memory"` 选择，不参与模型名匹配。
    因此即使它的 model 与 chat 同名，也不会把 chat 的调用抢走——记忆层的抽取与记忆块
    渲染走它，普通对话仍走 chat。
    """

    chat: LLMEndpointSettings = LLMEndpointSettings()
    vision: LLMEndpointSettings = LLMEndpointSettings()
    memory: LLMEndpointSettings = LLMEndpointSettings()


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
    retries: int = Field(default=2, ge=0)  # 429/529 退避重试次数，0 关闭


class DecisionSettings(BaseModel):
    """判断层配置：jev 云端与 laya 本地是两个端点，字段相同、协议相同。"""

    jev: DecisionEndpointSettings = DecisionEndpointSettings(
        base_url="https://api.typesafe.ai/v1", model="jev-latest"
    )
    laya: DecisionEndpointSettings = DecisionEndpointSettings(
        base_url="http://127.0.0.1:8000/v1", model=""
    )


class MemoryGateSettings(BaseModel):
    """写入门控：双重判定（laya 与 LLM 各判一次，每次判重要度与新颖度）。"""

    backend: Backend = "laya"  # 第一层判定走哪个判断端点
    importance_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    novelty_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    skip_laya: bool = False  # True = 跳过第一层，直接进 LLM（laya 不可用时的应急开关）


class MemoryRecallSettings(BaseModel):
    """召回策略。"""

    top_k: int = Field(default=8, gt=0)  # 每张表各取多少条
    hops: int = Field(default=2, ge=1, le=3)  # 图多跳的跳数
    budget: int = Field(default=2000, gt=0)  # 记忆块的字符预算
    block_by_llm: bool = True  # False = 恒走确定性模板


class MemoryDecaySettings(BaseModel):
    """遗忘衰减：score = importance * exp(-Δt/τ) * (1 + log(1 + access_count))。"""

    tau: float = Field(default=30.0, gt=0)  # 衰减常数（世界时间单位）
    floor: float = Field(default=0.05, gt=0.0, le=1.0)  # 跌破此值即删除


class MemoryConsolidateSettings(BaseModel):
    """定时循环。"""

    enabled: bool = True
    interval: float = Field(default=3600.0, gt=0)  # 周期（秒）
    min_events: int = Field(default=20, gt=0)  # 累积够多少条事件才触发一轮


class MemoryRetrySettings(BaseModel):
    """失败重试与死信。"""

    max_attempts: int = Field(default=3, ge=1)  # 单条最多重试次数（首次不计）
    backoff: float = Field(default=1.0, gt=0)  # 退避基数（秒），按 backoff * 2^n 递增
    deadletter_path: str = "data/memory/deadletter.jsonl"  # append-only，刻意独立于 Kùzu


class MemorySettings(BaseModel):
    """记忆层配置。"""

    db_path: str = "data/memory"  # Kùzu 数据库目录（一库一世界）
    dimensions: int = Field(default=1024, gt=0)  # 向量列维度，必须与嵌入端点实际返回一致
    queue_size: int = Field(default=256, gt=0)
    gate: MemoryGateSettings = MemoryGateSettings()
    recall: MemoryRecallSettings = MemoryRecallSettings()
    consolidate: MemoryConsolidateSettings = MemoryConsolidateSettings()
    decay: MemoryDecaySettings = MemoryDecaySettings()
    retry: MemoryRetrySettings = MemoryRetrySettings()


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
    memory: MemorySettings = MemorySettings()

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
