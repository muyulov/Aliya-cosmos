"""记忆层错误树。

约定：
- 全部继承 MemoryError，调用方可以一次捕获整个记忆层。
- 下层异常包进来时挂在 __cause__ 上，需要排查原始报错时顺着 cause 找。
- 对账信息收进属性而不是塞进消息：调用方要按字段分支时不必解析文本。
- 不在 ServiceError 树下：记忆读写失败是业务运行期错误，不该让进程 fail fast。
"""

from __future__ import annotations


class MemoryError(Exception):
    """记忆层错误基类。"""


class MemoryConfigError(MemoryError):
    """配置导致的能力不可用：依赖端点未就绪（embedding 无 key / llm 无 key / decision 未启用）。

    带上是哪一个依赖没就绪，便于对账。
    无 cause：这不是调用失败，是根本没有发起调用。
    """

    def __init__(self, message: str, *, dependency: str) -> None:
        self.dependency: str = dependency
        super().__init__(message)


class MemoryStoreError(MemoryError):
    """Kùzu 打开、建表、查询、事务失败（包装其 RuntimeError）。

    带上是在哪一步操作失败的，便于对账。
    __cause__ 挂原始的 Kùzu 异常。
    """

    def __init__(self, message: str, *, operation: str) -> None:
        self.operation: str = operation
        super().__init__(message)


class MemoryInputError(MemoryError):
    """observe 载荷非法：空文本、未知 kind。

    在本地就拦下来，不必白跑一趟后续处理。
    """


class MemoryExtractError(MemoryError):
    """抽取结果不符合 schema（包装 LLMSchemaError）。"""
