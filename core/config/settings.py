"""配置模块。

约定：
- data/config/app.yaml 是配置的唯一骨架，字段缺失时用这里的默认值。
- 需要密钥等隐私值时不写明文，在 YAML 里写 ${VAR} 占位符，值由 .env 提供。
- 通过 get_settings() 获取单例；测试中可用 get_settings.cache_clear() 重置，
  或用 load_settings(config_file=..., env_file=...) 显式指定文件。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from core.config.loader import CONFIG_FILE, ENV_FILE, interpolate, read_env, read_yaml

Environment = Literal["dev", "test", "prod"]

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
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(populate_by_name=True)

    level: str = "INFO"
    json_output: bool = Field(default=False, alias="json")
    dir: str = "logs"
    file_name: str = "app.log"
    error_file_name: str = "error.log"
    rotation: str = "00:00"
    retention: str = "7 days"
    compression: str = "zip"


class ServiceSettings(BaseModel):
    """服务生命周期配置。"""

    start_timeout: float | None = Field(default=30.0, gt=0)  # 秒；None 表示不限制
    stop_timeout: float | None = Field(default=30.0, gt=0)


class ClockSettings(BaseModel):
    """时钟服务配置。"""

    tz: str = "UTC"


class Settings(BaseModel):
    """顶层配置，按子模块分组，避免一个扁平大类。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", populate_by_name=True)

    app: AppSettings = AppSettings()
    log: LogSettings = LogSettings()
    service: ServiceSettings = ServiceSettings()
    clock: ClockSettings = ClockSettings()

    #: 本次配置的来源；由 load_settings() 写入，直接构造实例时保持 None
    _source: str | None = PrivateAttr(default=None)

    @property
    def config_source(self) -> str:
        """本次配置的来源，供启动日志展示。"""
        return self._source or SOURCE_INJECTED

    def mark_source(self, source: str) -> None:
        """记录配置来源。由 load_settings() 在装配完成后调用。"""
        self._source = source


def load_settings(
    config_file: Path = CONFIG_FILE,
    env_file: Path = ENV_FILE,
) -> Settings:
    """加载配置：读 YAML → 占位符插值 → 校验，并记录配置来源。

    文件缺失不报错（全部走默认值），占位符取不到值则抛 ConfigError。
    """
    raw, found = read_yaml(config_file)
    data = interpolate(raw, read_env(env_file))
    settings = Settings.model_validate(data)
    settings.mark_source(str(config_file) if found else f"{SOURCE_DEFAULT}（未找到 {config_file}）")
    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取配置单例。"""
    return load_settings()
