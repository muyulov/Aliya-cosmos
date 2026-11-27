"""cosmos 错误树。

约定（见 `docs/wiki/architecture/boundaries.md` 错误分层一节）：

- 统一继承 `CosmosError`，调用方一次 `except CosmosError` 就能兜住全部业务错误。
- 三个属性：`code`（机器可读）、`message`（人可读）、`retriable`（是否值得重试）。
- 输入问题 → 抛错且 `retriable=False`；运行期失败 → 事件 + 降级，不杀进程；
  配置 / 编程错误 → 启动期 fail fast。

`code` 与 `retriable` 用类属性作默认值，实例可通过构造参数覆盖；
`str(exc)` 只返回 message，日志侧用 `Service.log_error("…", exc)` 自动拆出
「错误类型 / 错误消息」两个结构化字段。
"""

from __future__ import annotations

from typing import override

__all__ = [
    "AgentBusyError",
    "AgentInvokeError",
    "AgentNotFoundError",
    "AgentRegistrationError",
    "ContractVersionError",
    "CosmosError",
    "CosmosPermissionError",
    "DispatchError",
    "MessageDeliveryError",
    "PresenceError",
    "PresencePrivacyError",
]


class CosmosError(Exception):
    """cosmos 业务错误基类。"""

    #: 机器可读错误码；子类用类属性固定默认值
    code: str = "internal"
    #: 是否值得重试；输入类错误一律 False
    retriable: bool = False

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        retriable: bool | None = None,
    ) -> None:
        self.message: str = message
        if code is not None:
            self.code = code
        if retriable is not None:
            self.retriable = retriable
        super().__init__(message)

    @override
    def __repr__(self) -> str:
        return f"<{type(self).__name__} code={self.code!r} retriable={self.retriable}>"


class AgentRegistrationError(CosmosError):
    """卡片字段非法、重名、能力声明冲突。"""

    code: str = "agent_registration"


class AgentNotFoundError(CosmosError):
    """引用了未注册或已禁用的 agent。"""

    code: str = "agent_not_found"

    def __init__(
        self,
        message: str,
        *,
        reason: str | None = None,
        code: str | None = None,
        retriable: bool | None = None,
    ) -> None:
        self.reason: str | None = reason
        super().__init__(message, code=code, retriable=retriable)


class AgentInvokeError(CosmosError):
    """提交任务失败（handler 不可用、参数不符合 schema）。"""

    code: str = "agent_invoke"

    def __init__(
        self,
        message: str,
        *,
        agent_id: str | None = None,
        code: str | None = None,
        retriable: bool | None = None,
    ) -> None:
        self.agent_id: str | None = agent_id
        super().__init__(message, code=code, retriable=retriable)


class AgentBusyError(CosmosError):
    """超过并发上限或该 agent 不允许并发。"""

    code: str = "agent_busy"

    def __init__(
        self,
        message: str,
        *,
        agent_id: str | None = None,
        code: str | None = None,
        retriable: bool | None = None,
    ) -> None:
        self.agent_id: str | None = agent_id
        super().__init__(message, code=code, retriable=retriable)


class DispatchError(CosmosError):
    """无法生成调度计划（无候选、置信度过低且未指定目标）。"""

    code: str = "dispatch"


class PresenceError(CosmosError):
    """状态字段非法、采集开关冲突。"""

    code: str = "presence"


class PresencePrivacyError(CosmosError):
    """请求了未授权的状态字段。"""

    code: str = "presence_privacy"


class MessageDeliveryError(CosmosError):
    """投递失败且重试耗尽。"""

    code: str = "message_delivery"


class ContractVersionError(CosmosError):
    """契约主版本不一致，不做兼容猜测。"""

    code: str = "contract_version"


class CosmosPermissionError(CosmosError):
    """越权调用。"""

    code: str = "permission"

    def __init__(
        self,
        message: str,
        *,
        capability: str = "",
        principal: str = "",
        code: str | None = None,
        retriable: bool | None = None,
    ) -> None:
        self.capability: str = capability
        self.principal: str = principal
        super().__init__(message, code=code, retriable=retriable)
