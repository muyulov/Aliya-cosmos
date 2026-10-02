"""判断层错误树。

约定：
- 全部继承 DecisionError，调用方可以一次捕获整个判断层。
- 原始 httpx2 异常挂在 __cause__ 上，需要排查端点返回体时顺着 cause 找。
- 不在 ServiceError 树下：调用失败是业务运行期错误，不该让进程 fail fast。
"""

from __future__ import annotations


class DecisionError(Exception):
    """判断层错误基类。"""


class DecisionConfigError(DecisionError):
    """配置导致的能力不可用：端点没启用。

    无 cause：这不是调用失败，是根本没有发起调用。
    """


class DecisionRequestError(DecisionError):
    """端点返回 4xx / 5xx。

    带上对账需要的三个坐标（状态码、端点、模型）与端点给的 request_id。
    __cause__ 挂原始的 httpx2.HTTPStatusError，要读端点返回体时顺着它找。
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


class DecisionTimeoutError(DecisionError):
    """请求超时。

    __cause__ 挂原始的 httpx2.TimeoutException。
    """


class DecisionConnectionError(DecisionError):
    """连不上端点（DNS、TLS、连接被拒等），超时除外。

    __cause__ 挂原始的 httpx2.TransportError（非超时的那类）。
    """


class DecisionResponseError(DecisionError):
    """返回体不可用：不是合法 JSON、顶层不是对象、缺 answers、便捷方法要的字段不在。

    无 cause 或 cause 是 JSON 解析异常：问题出在返回体本身，端点这一趟是通的。
    """
