from agent.aliya.prompt.builder import PromptBuilder
from agent.aliya.turn.context import TurnContext


def test_layers_in_order() -> None:
    builder = PromptBuilder(persona="人格", charter="准则", tools=[])
    messages = builder.build(turn=TurnContext(user_text="在吗"))
    assert messages[0]["role"] == "system"
    assert "人格" in messages[0]["content"]
    assert "准则" in messages[0]["content"]
    assert messages[-1]["role"] == "user"


def test_context_and_history_are_included() -> None:
    builder = PromptBuilder(persona="人格", charter="准则", tools=[])
    turn = TurnContext(
        user_text="在吗",
        presence="对方在线",
        tasks="没有待办",
        world="夜里",
        history=[{"role": "user", "content": "上一句"}],
    )
    messages = builder.build(turn=turn)
    assert "对方在线" in messages[0]["content"]
    assert "夜里" in messages[0]["content"]
    assert messages[1]["content"] == "上一句"
    assert messages[-1]["content"] == "在吗"
