"""向量化内核：协议、结果结构、远程实现与工厂。

换内核 = 子类覆盖 EmbeddingService._make_encoder()（容器的构造器契约只认
Service 与配置节点，协议对象注不进去）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, cast

from openai import AsyncOpenAI, Omit, omit
from openai.types.create_embedding_response import Usage

from core.config import EmbeddingSettings


@dataclass(frozen=True, slots=True)
class EncodedVector:
    """一条向量，index 是它在本次请求入参中的位置。

    归位与 index 校验在服务层（EmbeddingService._reorder）：内核只把位置原样
    报上来，不替服务层猜顺序，也不自行重排或丢弃。
    """

    index: int
    vector: list[float]


@dataclass(frozen=True, slots=True)
class EncodeResult:
    """一次 encode() 的结果。

    prompt_tokens 为 None 表示端点没返回用量（兼容端点常见），由调用方决定记不记。
    """

    items: tuple[EncodedVector, ...]
    prompt_tokens: int | None = None


class Encoder(Protocol):
    """把一批文本换成一批向量。

    约定（实现者必须遵守）：
    - 一次 encode() = 一次请求一批文本，返回的 items 条数必须与入参相等。
    - 每条向量带 index（入参位置），归位由调用方负责——内核不得自行重排。
    - dimensions 非 None 时必须兑现：拿不到该维度就抛错，禁止静默忽略。本地 bge
      这类没有截断能力的实现遇到该参数应当显式报错，而不是返回原始维度——
      否则调用方以为拿到 512 维、实际是 1024 维，错得很晚。
    - aclose() 是资源回收入口；没有资源的本地实现写空实现即可。
    """

    async def encode(
        self, texts: Sequence[str], *, dimensions: int | None = None
    ) -> EncodeResult: ...

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

    async def encode(self, texts: Sequence[str], *, dimensions: int | None = None) -> EncodeResult:
        """一次请求带一批文本，把向量与它们在入参中的位置原样报上去。"""
        response = await self._client.embeddings.create(
            model=self._model,
            input=list(texts),
            dimensions=_or_omit(dimensions),
        )
        # usage 在 SDK 类型里是必填，但兼容端点不返回时 construct_type 会把它填成
        # None（实测），直接取属性会炸。类型上表达不出来，只能绕过类型系统
        # （先转 object 再转目标类型，双重 cast 才不会撞 reportInvalidCast）。
        usage = cast("Usage | None", cast("object", response.usage))
        return EncodeResult(
            items=tuple(
                EncodedVector(index=item.index, vector=item.embedding) for item in response.data
            ),
            prompt_tokens=None if usage is None else usage.prompt_tokens,
        )

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
