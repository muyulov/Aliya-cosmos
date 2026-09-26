"""LLM 层错误树。

约定：
- 全部继承 LLMError，调用方可以一次捕获整个 LLM 层。
- SDK 的原始异常挂在 __cause__ 上，需要排查端点返回体时顺着 cause 找。
- 不在 ServiceError 树下：LLM 调用失败是业务运行期错误，不该让进程 fail fast。
"""

from __future__ import annotations


class LLMError(Exception):
    """LLM 层错误基类。"""


class LLMConfigError(LLMError):
    """配置缺失导致的能力不可用：端点未配 api_key。

    无 cause：这不是调用失败，是根本没有发起调用。
    """


class LLMRequestError(LLMError):
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


class LLMTimeoutError(LLMError):
    """请求超时。"""


class LLMConnectionError(LLMError):
    """连不上端点（DNS、TLS、连接被拒等），超时除外。"""


class LLMSchemaError(LLMError):
    """结构化输出不符合给定 schema。"""


class LLMResponseError(LLMError):
    """返回体缺内容：choices 为空、content 为 None 等。"""
