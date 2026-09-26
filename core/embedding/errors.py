"""向量层错误树。

约定：
- 全部继承 EmbeddingError，调用方可以一次捕获整个向量层。
- SDK 的原始异常挂在 __cause__ 上，需要排查端点返回体时顺着 cause 找。
- 不在 ServiceError 树下：向量化失败是业务运行期错误，不该让进程 fail fast。
- 比 llm 多一个 EmbeddingInputError：入参错与端点错要能分开处理。
"""

from __future__ import annotations


class EmbeddingError(Exception):
    """向量层错误基类。"""


class EmbeddingConfigError(EmbeddingError):
    """配置缺失导致的能力不可用：端点未配 api_key。

    无 cause：这不是调用失败，是根本没有发起调用。
    """


class EmbeddingInputError(EmbeddingError):
    """入参不合法：空串或全空白串。

    在本地就拦下来，不必白花一次必然 400 的请求。
    """


class EmbeddingRequestError(EmbeddingError):
    """端点返回 4xx / 5xx。

    带上对账需要的三个坐标：状态码、端点、模型，以及端点给的 request_id。
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        endpoint: str,
        model: str,
        request_id: str | None,
    ) -> None:
        self.status_code: int = status_code
        self.endpoint: str = endpoint
        self.model: str = model
        self.request_id: str | None = request_id
        super().__init__(message)


class EmbeddingTimeoutError(EmbeddingError):
    """请求超时。"""


class EmbeddingConnectionError(EmbeddingError):
    """连不上端点（DNS、TLS、连接被拒等），超时除外。"""


class EmbeddingResponseError(EmbeddingError):
    """返回体与请求对不上：条数不符、index 越界或重复、向量为空、声明维度不符。"""
