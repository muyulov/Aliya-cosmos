"""配置模块。

约定：
- 所有字段都有默认值，克隆仓库后不做任何配置即可启动。
- 环境变量前缀为 APP_，嵌套层级用双下划线，例如 APP_APP__ENV=prod。
- 通过 get_settings() 获取单例；测试中可用 get_settings.cache_clear() 重置。
"""

from __future__ import annotations

from functools import lru_cache
from typing import ClassVar, Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["dev", "test", "prod"]


class AppSettings(BaseSettings):
    """应用层配置。"""

    app_name: str = "aliya-cosmos"
    env: Environment = "dev"
    debug: bool = True

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"


class LogSettings(BaseSettings):
    """日志层配置。

    注意 json 字段用别名 json_output：pydantic 在序列化时用 json 作为方法名，
    直接命名 json 会与父类属性冲突。

    支持 json（默认分组，读 APP_LOG__LEVEL 等）。
    """

    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        extra="ignore", populate_by_name=True
    )

    level: str = "INFO"
    json_output: bool = Field(default=False, alias="json")
    dir: str = "logs"
    file_name: str = "app.log"
    error_file_name: str = "error.log"
    rotation: str = "00:00"
    retention: str = "7 days"
    compression: str = "zip"


class Settings(BaseSettings):
    """顶层配置，按子模块分组，避免一个扁平大类。"""

    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="APP_",
        env_nested_delimiter="__",
        extra="ignore",
        populate_by_name=True,
    )

    app: AppSettings = AppSettings()
    log: LogSettings = LogSettings()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取配置单例。"""
    return Settings()
