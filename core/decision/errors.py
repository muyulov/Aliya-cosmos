"""判断层错误树。

约定：
- 全部继承 DecisionError，调用方可以一次捕获整个判断层。
- 原始 httpx2 异常挂在 __cause__ 上的只有三类：超时、连接、返回体解码失败。
  其余（配置错、状态码错、返回体内容错）没有 cause，对账信息已收进属性，
  别顺着 __cause__ 找——那里是 None。
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

    带上对账需要的四个坐标：状态码、端点、模型、端点给的 request_id。
    无 __cause__：端点返回体的对账信息已收敛成 status_code / endpoint / model /
    request_id 四个属性，这里不保留原始异常（也未调用 raise_for_status，
    压根不会有 httpx2.HTTPStatusError）。
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

    __cause__ 挂原始的 httpx2.TransportError（非超时的那类），或兜底的
    httpx2.RequestError（非 TransportError 的其余请求错误）。
    """


class DecisionResponseError(DecisionError):
    """返回体不可用。

    覆盖：不是合法 JSON（含非 UTF-8 字节）、Content-Encoding 声明的压缩体解不开、
    顶层不是对象、缺 answers、便捷方法要的字段不在。

    cause 视来源而定：坏 JSON 挂 json.JSONDecodeError / UnicodeDecodeError，
    解不开压缩体挂 httpx2.DecodingError，顶层不是对象与缺 answers 无 cause。
    共同点是问题都出在返回体本身，端点这一趟是通的——因此都归这个类，而不是
    连接错误。
    """
