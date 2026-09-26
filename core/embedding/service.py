"""向量服务：把「文本转向量」收成一个服务，业务代码不直接接触 SDK。

约定：
- 两项能力：embed（单条）与 embed_many（批量，按 batch_size 串行切片）。
- 输出原样透传：不做 L2 归一化，模长信息该不该丢由调用方决定。
- 缺 api_key 时只警告、不建内核：脚手架不该因为没密钥就起不来。
- dimensions 三级优先：调用方覆盖 > 配置 > 不传（用模型原始维度）。
- 日志不记正文：正文该不该记由调用方自己决定并自己打。
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from typing import ClassVar, override

from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError

from core.config import EmbeddingSettings
from core.embedding.encoder import EncodedVector, RemoteEncoder, build_encoder
from core.embedding.errors import (
    EmbeddingConfigError,
    EmbeddingConnectionError,
    EmbeddingError,
    EmbeddingInputError,
    EmbeddingRequestError,
    EmbeddingResponseError,
    EmbeddingTimeoutError,
)
from core.service.base import HealthStatus, Service

#: 没配密钥时的统一措辞：start 的警告、health 的详情、调用时的报错共用
NO_API_KEY = "未配置 api_key，向量功能不可用"


def _pick(override: int | None, fallback: int | None) -> int | None:
    """调用方的覆盖优先，其次配置，都没有则不传（用模型原始维度）。"""
    return override if override is not None else fallback


def _elapsed_ms(begin: float) -> float:
    """从 begin（`time.perf_counter()` 的取值）到现在经过的毫秒数。"""
    return round((time.perf_counter() - begin) * 1000, 1)


@asynccontextmanager
async def _wrap_errors(endpoint: str, model: str) -> AsyncGenerator[None, None]:
    """把 SDK 异常换成向量层错误。

    except 顺序是硬要求：APITimeoutError 是 APIConnectionError 的子类，
    先捕子类才不会把超时一律误判成连接错误。
    """
    try:
        yield
    except APITimeoutError as exc:
        raise EmbeddingTimeoutError(f"请求模型 {model} 超时：{exc}") from exc
    except APIStatusError as exc:
        raise EmbeddingRequestError(
            f"模型 {model} 在 {endpoint} 返回 {exc.status_code}",
            status_code=exc.status_code,
            endpoint=endpoint,
            model=model,
            request_id=exc.request_id,
        ) from exc
    except APIConnectionError as exc:
        raise EmbeddingConnectionError(f"连接 {endpoint} 失败：{exc}") from exc
    except APIError as exc:
        raise EmbeddingError(f"调用模型 {model} 失败：{exc}") from exc


class EmbeddingService(Service):
    """向量服务：持有内核，暴露单条与批量两项能力。"""

    name: ClassVar[str] = "embedding"

    def __init__(self, config: EmbeddingSettings) -> None:
        super().__init__()
        self._config: EmbeddingSettings = config
        self._encoder: RemoteEncoder | None = None

    # ---------- 模型 ----------

    @property
    def model(self) -> str:
        """向量化模型名。"""
        return self._config.model

    # ---------- 生命周期 ----------

    @override
    async def start(self) -> None:
        """建内核。没配 api_key 只警告：脚手架不该因为没密钥就起不来。"""
        if self._encoder is not None:
            return
        if not self._config.api_key:
            self.log.warning(NO_API_KEY, 端点=self._config.base_url)
            return
        self._encoder = build_encoder(self._config)

    @override
    async def stop(self) -> None:
        """关内核。必须幂等：回滚时它也会作用在没建过内核的实例上。"""
        encoder, self._encoder = self._encoder, None
        if encoder is not None:
            await encoder.aclose()

    @override
    async def health(self) -> HealthStatus:
        """只看有没有内核：健康检查不该发请求（有副作用、花钱、受网络抖动影响）。"""
        healthy = self._encoder is not None
        detail = "" if healthy else NO_API_KEY
        return HealthStatus(name=self.label, healthy=healthy, state=self.state, detail=detail)

    # ---------- 调用能力 ----------

    async def embed(self, text: str, *, dimensions: int | None = None) -> list[float]:
        """单条文本向量化。"""
        self._check_texts((text,))
        resolved = _pick(dimensions, self._config.dimensions)
        encoder = self._require_encoder()
        begin = time.perf_counter()
        async with _wrap_errors(self._config.base_url, self._config.model):
            result = await encoder.encode([text], dimensions=resolved)
        vectors = self._reorder(result.items, 1)
        self._check_vectors(vectors, resolved)
        self._log_batch(begin, 1, len(vectors[0]), result.prompt_tokens)
        return vectors[0]

    async def embed_many(
        self, texts: Sequence[str], *, dimensions: int | None = None
    ) -> list[list[float]]:
        """批量文本向量化：按 batch_size 切片、逐批串行请求。

        空序列直接返回 []，不发请求、不查密钥（空输入不需要服务）。
        先全量校验再发第一批：否则传到第 5 条才发现空串，前几批白花钱。
        """
        if not texts:
            return []
        self._check_texts(texts)
        resolved = _pick(dimensions, self._config.dimensions)
        encoder = self._require_encoder()
        size = self._config.batch_size
        batches = [texts[start : start + size] for start in range(0, len(texts), size)]
        begin = time.perf_counter()
        vectors: list[list[float]] = []
        for batch in batches:
            batch_begin = time.perf_counter()
            async with _wrap_errors(self._config.base_url, self._config.model):
                result = await encoder.encode(batch, dimensions=resolved)
            batch_vectors = self._reorder(result.items, len(batch))
            self._check_vectors(batch_vectors, resolved)
            vectors.extend(batch_vectors)
            self._log_batch(batch_begin, len(batch), len(batch_vectors[0]), result.prompt_tokens)
        if len(batches) > 1:
            self.log.info(
                "批量向量化完成",
                批数=len(batches),
                文本数=len(texts),
                耗时毫秒=_elapsed_ms(begin),
            )
        return vectors

    # ---------- 内部 ----------

    def _require_encoder(self) -> RemoteEncoder:
        """取内核；没配 api_key 时到这里才报错（配置缺失不该让进程起不来）。"""
        if self._encoder is None:
            raise EmbeddingConfigError(NO_API_KEY)
        return self._encoder

    @staticmethod
    def _check_texts(texts: Sequence[str]) -> None:
        """空串与全空白串在本地拦下：换不来任何信息，只是白花一次必然 400 的请求。"""
        for text in texts:
            if not text.strip():
                raise EmbeddingInputError("待向量化的文本不能为空或全空白")

    @staticmethod
    def _reorder(items: Sequence[EncodedVector], expected: int) -> list[list[float]]:
        """按 index 归位（兼容端点可能乱序返回，不依赖端点顺序）。

        index 越界、重复或条数不符一律抛错：不猜顺序，也不拿坏数据凑数。
        """
        order = sorted(item.index for item in items)
        if order != list(range(expected)):
            raise EmbeddingResponseError(
                f"端点返回的 index 与请求对不上：期望 0..{expected - 1}，实际 {order}"
            )
        return [item.vector for item in sorted(items, key=lambda item: item.index)]

    @staticmethod
    def _check_vectors(vectors: list[list[float]], dimensions: int | None) -> None:
        """校验声明的维度被兑现：端点静默忽略 dimensions 时，只有这里能发现。"""
        for vector in vectors:
            if not vector:
                raise EmbeddingResponseError("端点返回了空向量")
            if dimensions is not None and len(vector) != dimensions:
                raise EmbeddingResponseError(
                    f"端点返回 {len(vector)} 维向量，与声明的 {dimensions} 维不符"
                )

    def _log_batch(
        self, begin: float, count: int, dimension: int, prompt_tokens: int | None
    ) -> None:
        """每批一条成功日志：只记模型 / 端点 / 条数 / 维度 / 耗时，不记正文。

        维度记的是实际返回的维数（未声明 dimensions 时它就是模型原始维度）；
        端点没返回用量时省略 输入token，不因为没得记而报错。
        """
        fields: dict[str, object] = {
            "模型": self._config.model,
            "端点": self._config.base_url,
            "文本数": count,
            "维度": dimension,
            "耗时毫秒": _elapsed_ms(begin),
        }
        if prompt_tokens is not None:
            fields["输入token"] = prompt_tokens
        # 显式传 face：fields 的值是 object，**fields 展开时会被逐个形参对账
        self.log.info("向量化完成", face=None, **fields)
