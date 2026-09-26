"""向量化内核：协议、远程实现与工厂。

约定（实现者必须遵守）：
- 一次 encode() = 一次请求一批文本，返回顺序与入参一一对应，条数必须相等。
- dimensions 非 None 时必须兑现：拿不到该维度就抛错，禁止静默忽略。本地 bge
  这类没有截断能力的实现遇到该参数应当显式报错，而不是返回原始维度——
  否则调用方以为拿到 512 维、实际是 1024 维，错得很晚。
- aclose() 是资源回收入口；没有资源的本地实现写空实现即可。

换内核 = 子类覆盖 EmbeddingService._make_encoder()（容器的构造器契约只认
Service 与配置节点，协议对象注不进去）。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from openai import AsyncOpenAI, Omit, omit

from core.config import EmbeddingSettings
from core.embedding.errors import EmbeddingResponseError


class Encoder(Protocol):
    """把一批文本换成一批向量。"""

    async def encode(
        self, texts: Sequence[str], *, dimensions: int | None = None
    ) -> list[list[float]]: ...

    async def aclose(self) -> None: ...


def _or_omit(value: int | None) -> int | Omit:
    """None 换成 omit（不传该参数）。

    SDK 3.x 的请求体只剔除 NotGiven 与 Omit，显式 None 会被原样序列化成
    JSON null（端点多半 400），所以「不传」必须走 omit。
    """
    return omit if value is None else value


class RemoteEncoder:
    """OpenAI 兼容端点的内核实现。"""

    def __init__(self, client: AsyncOpenAI, model: str) -> None:
        self._client: AsyncOpenAI = client
        self._model: str = model

    async def encode(
        self, texts: Sequence[str], *, dimensions: int | None = None
    ) -> list[list[float]]:
        """一次请求带一批文本，按 index 归位后返回（不依赖端点返回顺序）。

        index 只有内核拿得到（协议出口是纯向量），因此归位与 index 校验都在这里做。
        index 缺失、越界或重复时抛错，不猜顺序。
        """
        response = await self._client.embeddings.create(
            model=self._model,
            input=list(texts),
            dimensions=_or_omit(dimensions),
        )
        indices = sorted(item.index for item in response.data)
        if indices != list(range(len(response.data))):
            raise EmbeddingResponseError(f"端点返回的 index 不合法：{indices}")
        return [item.embedding for item in sorted(response.data, key=lambda item: item.index)]

    async def aclose(self) -> None:
        await self._client.close()


def build_encoder(config: EmbeddingSettings) -> RemoteEncoder:
    """按配置构造远程内核。

    这是全仓库除 core/llm/client.py 外唯一 new 出 SDK 客户端的地方。
    SDK 默认 timeout 是 10 分钟、max_retries 是 2，这里都显式给值，
    免得默认值随 SDK 版本漂移。
    """
    client = AsyncOpenAI(
        api_key=config.api_key,
        base_url=config.base_url,
        timeout=config.timeout,
        max_retries=config.retries,
    )
    return RemoteEncoder(client, config.model)
