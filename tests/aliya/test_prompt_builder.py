from typing import cast

from openai.types.chat import ChatCompletionMessageParam

from agent.aliya.prompt.builder import PromptBuilder
from agent.aliya.turn.context import TurnContext
from core.llm import user


def text_at(messages: list[ChatCompletionMessageParam], index: int) -> str:
    """取出某条消息的正文。

    `content` 在部分 TypedDict 里是 NotRequired 且可能是多模态数组，
    测试只关心纯文本，这里统一收窄一次，避免每处断言都写 cast。
    """
    data = cast("dict[str, object]", cast("object", messages[index]))
    content = data.get("content")
    assert isinstance(content, str)
    return content


def test_layers_in_order() -> None:
    builder = PromptBuilder(persona="人格", charter="准则", tools=[])
    messages = builder.build(turn=TurnContext(user_text="在吗"))
    assert messages[0]["role"] == "system"
    system = text_at(messages, 0)
    assert "人格" in system
    assert "准则" in system
    assert messages[-1]["role"] == "user"


def test_context_and_history_are_included() -> None:
    builder = PromptBuilder(persona="人格", charter="准则", tools=[])
    turn = TurnContext(
        user_text="在吗",
        presence="对方在线",
        tasks="没有待办",
        world="夜里",
        history=[user("上一句")],
    )
    messages = builder.build(turn=turn)
    system = text_at(messages, 0)
    assert "对方在线" in system
    assert "夜里" in system
    assert text_at(messages, 1) == "上一句"
    assert text_at(messages, -1) == "在吗"


def test_budget_keeps_persona_and_charter() -> None:
    builder = PromptBuilder(persona="人格" * 1000, charter="准则" * 1000, tools=[])
    messages = builder.build(turn=TurnContext(user_text="在吗", history=[]))
    system = text_at(messages, 0)
    assert "人格" in system
    assert "准则" in system


def test_budget_trims_old_history() -> None:
    builder = PromptBuilder(persona="人格", charter="准则", tools=[])
    history: list[ChatCompletionMessageParam] = [user(f"旧{i}") for i in range(30)]
    messages = builder.build(turn=TurnContext(user_text="在吗", history=history))
    assert len(messages) <= 14
    assert text_at(messages, -2) == "旧29"
    assert text_at(messages, -1) == "在吗"


def test_extra_index_enters_system_after_persona() -> None:
    """技能索引必须真正进 system，否则 load_skill 没有名字清单可用。"""
    builder = PromptBuilder(
        persona="人格",
        charter="准则",
        tools=[],
        extra_index="可用 skill：workspace-tasks",
    )
    messages = builder.build(turn=TurnContext(user_text="在吗"))
    system = text_at(messages, 0)
    assert "可用 skill" in system
    assert system.index("人格") < system.index("可用 skill")
