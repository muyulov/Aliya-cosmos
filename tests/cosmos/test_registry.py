"""注册表：校验、查询、检索与配置加载。"""

from __future__ import annotations

from typing import cast

import pytest
from loguru import logger

from agent.cosmos.contracts import (
    AgentCard,
    AgentContext,
    AgentHandler,
    AgentRequest,
    AgentResult,
    DesktopMeta,
    ExecutionPolicy,
)
from agent.cosmos.errors import AgentNotFoundError, AgentRegistrationError
from agent.cosmos.registry import load_agents
from agent.cosmos.services import AgentRegistryService
from core.config import (
    AgentCardConfig,
    AgentCosmosSettings,
    AgentRegistrationSettings,
    Settings,
)


async def handle(_request: AgentRequest, _ctx: AgentContext) -> AgentResult:
    return AgentResult.ok(summary="完成")


def make_card(**overrides: object) -> AgentCard:
    payload: dict[str, object] = {
        "id": "edit",
        "name": "编辑台",
        "icon": "✏️",
        "summary": "编辑代码与文件",
        "description": "按指令修改仓库内文件：新建、改写、局部替换、批量重命名。",
        "capabilities": ["code_edit", "file_write"],
        "params_schema": {"type": "object"},
        "desktop": DesktopMeta(group="dev", order=10),
        "execution": ExecutionPolicy(timeout_seconds=300),
    }
    payload.update(overrides)
    return AgentCard.model_validate(payload)


def make_registry(settings: Settings) -> AgentRegistryService:
    return AgentRegistryService(settings.agent)


def test_注册后可查询(settings: Settings) -> None:
    registry = make_registry(settings)
    card = make_card()
    registry.register(card, handle)

    assert registry.get("edit") == card
    assert registry.handler("edit") is handle
    assert registry.enabled("edit") is True
    assert [item.id for item in registry.list()] == ["edit"]


def test_重复注册报错(settings: Settings) -> None:
    registry = make_registry(settings)
    registry.register(make_card(), handle)
    with pytest.raises(AgentRegistrationError):
        registry.register(make_card(name="另一个"), handle)


def test_非法_id_报错(settings: Settings) -> None:
    registry = make_registry(settings)
    card = make_card().model_copy(update={"id": "Edit"})
    with pytest.raises(AgentRegistrationError):
        registry.register(card, handle)


def test_空能力报错(settings: Settings) -> None:
    registry = make_registry(settings)
    card = make_card().model_copy(update={"capabilities": []})
    with pytest.raises(AgentRegistrationError):
        registry.register(card, handle)


def test_非_object_schema_报错(settings: Settings) -> None:
    registry = make_registry(settings)
    card = make_card().model_copy(update={"params_schema": {"type": "array"}})
    with pytest.raises(AgentRegistrationError):
        registry.register(card, handle)


def test_超时必须为正(settings: Settings) -> None:
    registry = make_registry(settings)
    execution = make_card().execution.model_copy(update={"timeout_seconds": 0})
    card = make_card().model_copy(update={"execution": execution})
    with pytest.raises(AgentRegistrationError):
        registry.register(card, handle)


def test_同步_handler_报错(settings: Settings) -> None:
    registry = make_registry(settings)

    def sync_handler(_request: AgentRequest, _ctx: AgentContext) -> AgentResult:
        return AgentResult.ok(summary="sync")

    with pytest.raises(AgentRegistrationError):
        registry.register(make_card(), cast("AgentHandler", sync_handler))


def test_摘要超长被截断并告警(settings: Settings) -> None:
    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    try:
        registry = make_registry(settings)
        registry.register(
            make_card(summary="这是一个非常非常长的摘要，长到超过二十个字的上限"), handle
        )
    finally:
        logger.remove(sink_id)

    card = registry.get("edit")
    assert len(card.summary) == 20
    assert any("摘要超长" in message for message in messages)
    # 原文不进日志
    assert not any("非常非常长" in message for message in messages)


def test_未声明分组回落_general(settings: Settings) -> None:
    registry = make_registry(settings)
    registry.register(make_card(desktop=DesktopMeta(group="nope")), handle)
    assert registry.get("edit").desktop.group == "general"


def test_检索命中能力(settings: Settings) -> None:
    registry = make_registry(settings)
    registry.register(make_card(), handle)

    matches = registry.match("改一下这个函数")
    assert matches
    assert matches[0].agent_id == "edit"
    assert "capabilities" in matches[0].reason
    assert matches[0].score == 0.9


def test_检索按分数与_id_排序(settings: Settings) -> None:
    registry = make_registry(settings)
    registry.register(make_card(), handle)
    registry.register(
        make_card(id="echo", name="回声台", capabilities=["echo"], summary="原样返回指令"),
        handle,
    )
    matches = registry.match("帮我回显一句话")
    assert matches[0].agent_id == "echo"


def test_禁用后仍可查询但不再可调度(settings: Settings) -> None:
    registry = make_registry(settings)
    registry.register(make_card(), handle)

    registry.disable("edit")
    assert registry.enabled("edit") is False
    assert registry.get("edit").id == "edit"

    registry.enable("edit")
    assert registry.enabled("edit") is True


def test_注销后查不到(settings: Settings) -> None:
    registry = make_registry(settings)
    registry.register(make_card(), handle)
    registry.unregister("edit")

    with pytest.raises(AgentNotFoundError):
        _ = registry.get("edit")
    assert registry.list() == []


def test_配置加载成功(settings: Settings) -> None:
    registry = make_registry(settings)
    config = AgentCosmosSettings(
        agents=[
            AgentRegistrationSettings(
                id="echo",
                module="tests.cosmos.fake_agent",
                handler="handle",
                card=AgentCardConfig(
                    name="回声台",
                    summary="原样返回",
                    description="联调用的最小工具，把收到的文本原样返回。",
                    capabilities=["echo"],
                ),
            )
        ]
    )
    load_agents(registry, config)

    assert registry.get("echo").name == "回声台"
    assert registry.enabled("echo") is True


def test_模块不存在时报错带路径(settings: Settings) -> None:
    registry = make_registry(settings)
    config = AgentCosmosSettings(
        agents=[
            AgentRegistrationSettings(
                id="ghost",
                module="tests.cosmos.not_exists",
                card=AgentCardConfig(
                    name="幽灵",
                    summary="不存在",
                    description="用来验证导入失败时的报错信息。",
                    capabilities=["ghost"],
                ),
            )
        ]
    )
    with pytest.raises(AgentRegistrationError) as info:
        load_agents(registry, config)
    assert "tests.cosmos.not_exists" in str(info.value)


def test_同步_handler_在配置加载时被拒(settings: Settings) -> None:
    registry = make_registry(settings)
    config = AgentCosmosSettings(
        agents=[
            AgentRegistrationSettings(
                id="sync",
                module="tests.cosmos.fake_agent",
                handler="sync_handle",
                card=AgentCardConfig(
                    name="同步",
                    summary="同步实现",
                    description="同步 handler 无法响应取消，注册期必须拒绝。",
                    capabilities=["sync"],
                ),
            )
        ]
    )
    with pytest.raises(AgentRegistrationError):
        load_agents(registry, config)


def test_运行期注册默认被拒(settings: Settings) -> None:
    registry = make_registry(settings)
    with pytest.raises(AgentRegistrationError):
        registry.register(make_card(), handle, source="runtime")


def test_禁用后不参与检索(settings: Settings) -> None:
    registry = make_registry(settings)
    registry.register(make_card(), handle)
    registry.disable("edit")
    assert registry.match("改一下这个函数") == []
