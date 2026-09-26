"""LLM 客户端工厂。

约定：这是全仓库唯一 new 出 SDK 客户端的地方，也是测试注入替身的唯一接缝。
超时与重试都交给 SDK，这里只负责把配置原样传进去。
"""

from __future__ import annotations

from openai import AsyncOpenAI

from core.config import LLMEndpointSettings


def build_client(config: LLMEndpointSettings) -> AsyncOpenAI:
    """按端点配置构造异步客户端。

    SDK 默认 timeout 是 10 分钟、max_retries 是 2，这里都显式给值，
    免得默认值随 SDK 版本漂移。
    """
    return AsyncOpenAI(
        api_key=config.api_key,
        base_url=config.base_url,
        timeout=config.timeout,
        max_retries=config.retries,
    )
