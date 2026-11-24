"""控制面契约（设计 09 章）。"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field


class CosmosCapabilities(BaseModel):
    """`handshake()` 的返回：版本、可订阅主题、启用的能力与限额。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    api_version: str
    contract_version: str
    event_topics: list[str] = Field(default_factory=list)
    #: "world" / "presence.mood" / "dispatch.auto" ...
    features: list[str] = Field(default_factory=list)
    #: max_parallel / max_content_chars / history_limit
    limits: dict[str, int] = Field(default_factory=dict)
