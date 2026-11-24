"""配置驱动的子 agent 装配。

`core` 不能反向依赖 `agent`，所以卡片的 YAML 形态（`AgentCardConfig`）定义在
`core.config` 里；这里负责把它转成契约模型并按 `module + handler` 动态加载。
"""

from __future__ import annotations

import importlib
from typing import Protocol, cast

from pydantic import ValidationError

from agent.cosmos.contracts import (
    AgentCard,
    AgentExample,
    AgentHandler,
    AgentPermissions,
    DesktopMeta,
    ExecutionPolicy,
    PresenceField,
)
from agent.cosmos.errors import AgentRegistrationError
from core.config import AgentCardConfig, AgentCosmosSettings


class RegistrySink(Protocol):
    """注册表的最小接口：只依赖 `register`，避免 bootstrap 反向 import 服务实现。"""

    def register(
        self, card: AgentCard, handler: AgentHandler, *, source: str = "config"
    ) -> None: ...


def to_card(agent_id: str, config: AgentCardConfig) -> AgentCard:
    """把 YAML 形态的卡片转成契约模型。"""
    return AgentCard(
        id=agent_id,
        name=config.name,
        version=config.version,
        icon=config.icon,
        summary=config.summary,
        description=config.description,
        capabilities=list(config.capabilities),
        params_schema=dict(config.params_schema),
        examples=[AgentExample(input=item.input, note=item.note) for item in config.examples],
        desktop=DesktopMeta(
            group=config.desktop.group,
            order=config.desktop.order,
            visible=config.desktop.visible,
            pinned=config.desktop.pinned,
            badge=config.desktop.badge,
        ),
        execution=ExecutionPolicy(
            timeout_seconds=config.execution.timeout_seconds,
            max_concurrent=config.execution.max_concurrent,
            queue=config.execution.queue,
            cancel_on_supersede=config.execution.cancel_on_supersede,
            retryable=config.execution.retryable,
            heartbeat_seconds=config.execution.heartbeat_seconds,
        ),
        permissions=AgentPermissions(
            read_presence=config.permissions.read_presence,
            presence_fields=cast("list[PresenceField]", list(config.permissions.presence_fields)),
            notify_user=config.permissions.notify_user,
            read_conversation=config.permissions.read_conversation,
        ),
    )


def load_agents(registry: RegistrySink, config: AgentCosmosSettings) -> None:
    """按配置逐个导入并注册；任何一步失败都带模块路径抛错。"""
    for entry in config.agents:
        try:
            module = importlib.import_module(entry.module)
        except Exception as exc:
            msg = f"导入子 agent 模块失败：{entry.module}：{type(exc).__name__}: {exc}"
            raise AgentRegistrationError(msg) from exc

        handler: object = cast("object", getattr(module, entry.handler, None))
        if handler is None:
            msg = f"模块 {entry.module} 里没有 handler {entry.handler!r}"
            raise AgentRegistrationError(msg)
        if not callable(handler):
            msg = f"模块 {entry.module} 的 {entry.handler!r} 不是可调用对象"
            raise AgentRegistrationError(msg)

        try:
            card = to_card(entry.id, entry.card)
        except ValidationError as exc:
            msg = f"卡片配置非法：{entry.id}：{exc}"
            raise AgentRegistrationError(msg) from exc

        registry.register(card, cast("AgentHandler", handler), source="config")
