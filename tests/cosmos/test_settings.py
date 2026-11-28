"""agent 配置模型：默认值、约束、未知键告警与样例 YAML 对齐。"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
import yaml
from loguru import logger
from pydantic import ValidationError

from core.config import AgentCosmosSettings, AgentSettings, Settings, load_settings

#: 配置样例：与代码字段对齐的契约文件
SAMPLE = Path("agent/cosmos/config/agent-settings.example.yaml")


def test_可直接构造且默认值正确() -> None:
    settings = Settings()
    cosmos = settings.agent.cosmos
    assert cosmos.dispatch.mode == "suggest"
    assert cosmos.dispatch.min_confidence == 0.6
    assert cosmos.world.enabled is False
    assert cosmos.presence.enabled is True
    assert cosmos.presence.privacy.address_as == "你"
    assert cosmos.terminal.proactive.min_interval_seconds == 3600
    assert cosmos.agents == []


def test_agent_settings_可独立构造() -> None:
    assert AgentSettings().cosmos.contract.api_version == "1.0"
    assert AgentSettings().cosmos.desktop.history_limit == 5


def test_完整样例_yaml_可校验() -> None:
    raw = cast("object", yaml.safe_load(SAMPLE.read_text(encoding="utf-8")))
    assert isinstance(raw, dict)
    root = cast("dict[str, object]", raw)
    data = root["agent"]
    assert isinstance(data, dict)
    cosmos_data = cast("dict[str, object]", data)["cosmos"]
    assert isinstance(cosmos_data, dict)
    cosmos = AgentCosmosSettings.model_validate(cast("dict[str, object]", cosmos_data))
    assert cosmos.agents[0].id == "edit"
    assert cosmos.agents[0].card.capabilities == ["file_write", "file_edit", "code_edit"]
    assert cosmos.world.llm.temperature == 0.4
    assert cosmos.contract.contract_version == "1.0.0"


def test_仓库配置骨架可加载() -> None:
    settings = load_settings()
    assert settings.agent.cosmos.agents[0].id == "echo"
    assert settings.agent.cosmos.world.enabled is True
    assert settings.agent.cosmos.world.llm.max_tokens == 1200


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"dispatch": {"plan_timeout_seconds": 0}}, "超时必须为正"),
        ({"dispatch": {"max_parallel": 0}}, "并发至少 1"),
        ({"dispatch": {"auto_threshold": 1.5}}, "阈值在 0 到 1"),
        ({"dispatch": {"mode": "yolo"}}, "模式只有两种"),
        ({"link": {"queue_limit": 0}}, "队列至少 1"),
        ({"terminal": {"proactive": {"daily_limit": -1}}}, "每日上限非负"),
        ({"presence": {"mood": {"provider": "vibes"}}}, "情绪来源闭集"),
    ],
)
def test_非法值报校验错误(payload: dict[str, object], reason: str) -> None:
    with pytest.raises(ValidationError):
        _ = AgentCosmosSettings.model_validate(payload)
    assert reason  # 用例自带说明，便于失败时定位


def test_未知键只告警不阻断() -> None:
    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    try:
        cosmos = AgentCosmosSettings.model_validate({"dispatch": {"nope": 1}})
    finally:
        logger.remove(sink_id)

    assert cosmos.dispatch.mode == "suggest"
    assert any("未知键" in message for message in messages)


def test_对端称呼写别的值回落为你() -> None:
    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    try:
        cosmos = AgentCosmosSettings.model_validate(
            {"presence": {"privacy": {"address_as": "用户"}}}
        )
    finally:
        logger.remove(sink_id)

    assert cosmos.presence.privacy.address_as == "你"
    assert any("称呼" in message for message in messages)


def test_生活配置默认值() -> None:
    settings = Settings()
    life = settings.agent.aliya.life
    assert life.enabled is True
    assert life.tick_seconds == 60
    assert life.max_turns_per_day == 40
    assert life.rest_threshold == 0.25
    assert life.workspace_writes is True


def test_仓库骨架里的生活配置可加载() -> None:
    settings = load_settings()
    assert settings.agent.aliya.life.plan_minutes == 40
