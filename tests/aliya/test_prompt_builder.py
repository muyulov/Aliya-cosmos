from datetime import UTC, datetime
from typing import cast

from openai.types.chat import ChatCompletionMessageParam

from agent.aliya.prompt.builder import PromptBuilder
from agent.aliya.turn.context import TurnContext
from core.agent import tool
from core.llm import user


@tool
def get_weather(city: str) -> str:
    """查指定城市的天气。

    Args:
        city: 城市名。
    """
    return f"{city} 晴"


def text_at(messages: list[ChatCompletionMessageParam], index: int) -> str:
    """取出某条消息的正文。

    `content` 在部分 TypedDict 里是 NotRequired 且可能是多模态数组，
    测试只关心纯文本，这里统一收窄一次，避免每处断言都写 cast。
    """
    data = cast("dict[str, object]", cast("object", messages[index]))
    content = data.get("content")
    assert isinstance(content, str)
    return content


def test_system_keeps_persona_charter_and_skill_index() -> None:
    builder = PromptBuilder(
        persona="人格",
        charter="准则",
        tools=[],
        extra_index="可用 skill：workspace-tasks",
    )
    messages = builder.build(turn=TurnContext(user_text="在吗"))
    system = text_at(messages, 0)
    assert "人格" in system
    assert "准则" in system
    assert "可用 skill" in system
    assert system.index("人格") < system.index("可用 skill")


def test_动态上下文进消息不进系统() -> None:
    builder = PromptBuilder(persona="人格", charter="准则", tools=[])
    turn = TurnContext(
        user_text="在吗",
        presence="对方在线",
        tasks="没有待办",
        world="夜里",
    )
    messages = builder.build(turn=turn)

    system = text_at(messages, 0)
    assert "对方在线" not in system
    assert "夜里" not in system

    context = text_at(messages, 1)
    assert context.startswith("[当前上下文]")
    assert "对方在线" in context
    assert "夜里" in context


def test_当前时间排在上下文最前() -> None:
    builder = PromptBuilder(persona="人格", charter="准则", tools=[])
    turn = TurnContext(
        user_text="在吗",
        now=datetime(2026, 11, 26, 14, 26, tzinfo=UTC),
        presence="对方在线",
    )
    context = text_at(builder.build(turn=turn), 1)
    assert context.startswith("[当前上下文]\n现在：2026-11-26 14:26（星期四）")


def test_工具索引进上下文不进系统() -> None:
    builder = PromptBuilder(persona="人格", charter="准则", tools=[get_weather])
    messages = builder.build(turn=TurnContext(user_text="在吗"))

    system = text_at(messages, 0)
    assert "get_weather" not in system
    assert "get_weather" in text_at(messages, 1)


def test_历史完整保留且不截断内容() -> None:
    builder = PromptBuilder(persona="人格", charter="准则", tools=[])
    long_text = "长" * 10_000
    history: list[ChatCompletionMessageParam] = [user(f"旧{i}") for i in range(30)]
    history.append(user(long_text))

    messages = builder.build(turn=TurnContext(user_text="在吗", history=history))

    # 历史窗口由上游控制，PromptBuilder 不再二次裁剪
    assert len(messages) == 1 + len(history) + 1
    assert text_at(messages, -1) == "在吗"
    kept = [text_at(messages, index) for index in range(1, len(messages) - 1)]
    assert kept[0] == "旧0"
    assert long_text in kept


def test_上下文排在历史之前() -> None:
    builder = PromptBuilder(persona="人格", charter="准则", tools=[])
    turn = TurnContext(
        user_text="在吗",
        presence="对方在线",
        history=[user("上一句")],
    )
    messages = builder.build(turn=turn)

    assert "对方在线" in text_at(messages, 1)
    assert text_at(messages, 2) == "上一句"
    assert text_at(messages, -1) == "在吗"
