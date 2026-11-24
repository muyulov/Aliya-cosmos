"""错误树的单元测试：继承关系、默认码、附加字段与 message 语义。"""

from __future__ import annotations

import pytest

from agent.cosmos import errors
from agent.cosmos.errors import (
    AgentBusyError,
    AgentInvokeError,
    AgentNotFoundError,
    AgentRegistrationError,
    ContractVersionError,
    CosmosError,
    CosmosPermissionError,
    DispatchError,
    MessageDeliveryError,
    PresenceError,
    PresencePrivacyError,
    WorldBridgeError,
)

#: 11 个子类及其默认码
SUBCLASSES: list[tuple[type[CosmosError], str]] = [
    (AgentRegistrationError, "agent_registration"),
    (AgentNotFoundError, "agent_not_found"),
    (AgentInvokeError, "agent_invoke"),
    (AgentBusyError, "agent_busy"),
    (DispatchError, "dispatch"),
    (PresenceError, "presence"),
    (PresencePrivacyError, "presence_privacy"),
    (WorldBridgeError, "world_bridge"),
    (MessageDeliveryError, "message_delivery"),
    (ContractVersionError, "contract_version"),
    (CosmosPermissionError, "permission"),
]


@pytest.mark.parametrize(("error_type", "code"), SUBCLASSES)
def test_子类继承与默认码(error_type: type[CosmosError], code: str) -> None:
    exc = error_type("出错了")
    assert isinstance(exc, CosmosError)
    assert exc.code == code
    assert exc.retriable is False


@pytest.mark.parametrize(("error_type", "_code"), SUBCLASSES)
def test_默认不可重试(error_type: type[CosmosError], _code: str) -> None:
    assert error_type("x").retriable is False


def test_可覆盖错误码与可重试() -> None:
    exc = CosmosError("x", code="timeout", retriable=True)
    assert exc.code == "timeout"
    assert exc.retriable is True


def test_未注册错误保留原因() -> None:
    exc = AgentNotFoundError("找不到编辑台", reason="disabled")
    assert exc.reason == "disabled"
    assert exc.code == "agent_not_found"


def test_未注册错误的原因可为空() -> None:
    assert AgentNotFoundError("找不到").reason is None


def test_任务错误保留_agent_id() -> None:
    assert AgentInvokeError("x", agent_id="edit").agent_id == "edit"
    assert AgentBusyError("x", agent_id="edit").agent_id == "edit"


def test_越权错误保留能力与调用方() -> None:
    exc = CosmosPermissionError("不允许", capability="register", principal="aliya")
    assert exc.capability == "register"
    assert exc.principal == "aliya"


def test_str_只返回消息() -> None:
    exc = AgentNotFoundError("找不到编辑台", reason="disabled")
    assert str(exc) == "找不到编辑台"


def test_未知码不影响_str() -> None:
    exc = CosmosError("随便什么", code="whatever")
    assert str(exc) == "随便什么"


def test_错误模块显式导出全部子类() -> None:
    exported = set(errors.__all__)
    assert {item[0].__name__ for item in SUBCLASSES} <= exported
