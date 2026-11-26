from core.config.settings import AgentAliyaSettings, AgentControlSettings, load_settings


def test_defaults() -> None:
    control = AgentControlSettings()
    aliya = AgentAliyaSettings()
    assert control.socket_path == "data/state/aliya.sock"
    assert control.max_clients == 1
    assert aliya.reply.max_rounds == 6
    assert aliya.memory.dir == "data/Aliya"
    assert aliya.memory.anchors == []
    assert aliya.initiative.default_action == "wait"


def test_parse_nested() -> None:
    aliya = AgentAliyaSettings.model_validate(
        {
            "persona": {"source": "p.md", "charter": "c.md"},
            "memory": {"anchors": ["风信子"], "max_turns": 50},
            "reply": {"post_checks": ["narration"]},
        }
    )
    assert aliya.persona.source == "p.md"
    assert aliya.memory.max_turns == 50
    assert aliya.reply.post_checks == ["narration"]


def test_repo_yaml_has_control_and_aliya() -> None:
    settings = load_settings()
    assert settings.agent.control.socket_path == "data/state/aliya.sock"
    assert settings.agent.aliya.persona.source == "data/prompt/Aliya.md"
    assert settings.agent.aliya.memory.dir == "data/Aliya"
    assert settings.agent.aliya.memory.anchors == ["风信子", "拉姆石", "高马尾", "兔子狗", "海獭号"]
