"""向量层对外出口。

用法：
    from core.embedding import EmbeddingService

    vector = await svc.embed("要向量化的文本")
"""

from core.embedding.encoder import (
    EncodedVector,
    EncodeResult,
    RemoteEncoder,
    build_encoder,
)
from core.embedding.errors import (
    EmbeddingConfigError,
    EmbeddingConnectionError,
    EmbeddingError,
    EmbeddingInputError,
    EmbeddingRequestError,
    EmbeddingResponseError,
    EmbeddingTimeoutError,
)
from core.embedding.service import EmbeddingService

__all__ = [
    "EmbeddingConfigError",
    "EmbeddingConnectionError",
    "EmbeddingError",
    "EmbeddingInputError",
    "EmbeddingRequestError",
    "EmbeddingResponseError",
    "EmbeddingService",
    "EmbeddingTimeoutError",
    "EncodeResult",
    "EncodedVector",
    "RemoteEncoder",
    "build_encoder",
]
