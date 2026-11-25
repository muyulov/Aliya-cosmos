import pytest

from agent.aliya.prompt.builder import PromptBuilder
from agent.aliya.turn.context import TurnContext
from agent.aliya.turn.runner import TurnRunner
from agent.aliya.turn.tools import build_tools
from core.config.settings import AgentAliyaSettings
from core.llm import AssistantReply, ToolCall


class FakeRuntime:
    def __init__(self) -> None:
        self.delivered: list[str] = []

    async def send(self, text: str) -> None:
        self.delivered.append(text)

    async def describe_desktop(self) -> str:
        return "桌面"

    async def dispatch(self, task_text: str, **kwargs: object) -> str:
        return f"已派：{task_text}"

    async def task_status(self, task_id: str | None = None) -> str:
        return "进行中"

    async def cancel(self, task_id: str) -> str:
        return f"已取消：{task_id}"

    async def retry(self, task_id: str) -> str:
        return f"已重试：{task_id}"

    async def refresh_world(self) -> str:
        return "夜里"

    async def remember(self, note: str, kind: str) -> str:
        return "记住了"


class FakeLLM:
    def __init__(self) -> None:
        self.replies: list[AssistantReply] = []
        self.calls = 0

    async def chat_tools(self, messages: object, tools: object) -> AssistantReply:
        self.calls += 1
        return self.replies.pop(0)


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime()


def make_runner(llm: FakeLLM, runtime: FakeRuntime) -> TurnRunner:
    return TurnRunner(
        llm=llm,  # type: ignore[arg-type]
        tools=build_tools(runtime),  # type: ignore[arg-type]
        builder=PromptBuilder(persona="人格", charter="准则", tools=[]),
        config=AgentAliyaSettings(),
    )


@pytest.mark.asyncio
async def test_tool_loop_then_send(runtime: FakeRuntime) -> None:
    llm = FakeLLM()
    llm.replies = [
        AssistantReply(
            content=None,
            tool_calls=(
                ToolCall(id="1", name="send_message", arguments='{"text":"在吗"}'),
            ),
        ),
        AssistantReply(content=None, tool_calls=()),
    ]
    runner = make_runner(llm, runtime)
    await runner.run(turn=TurnContext(user_text="在吗", sender=runtime.send))
    assert runtime.delivered == ["在吗"]


@pytest.mark.asyncio
async def test_max_rounds_fallback(runtime: FakeRuntime) -> None:
    llm = FakeLLM()
    llm.replies = [
        AssistantReply(
            content=None,
            tool_calls=(
                ToolCall(id=str(i), name="see_desktop", arguments="{}"),
            ),
        )
        for i in range(10)
    ]
    runner = make_runner(llm, runtime)
    await runner.run(turn=TurnContext(user_text="在吗", sender=runtime.send))
    assert runtime.delivered == [AgentAliyaSettings().reply.fallback_text]
    assert llm.calls == AgentAliyaSettings().reply.max_rounds
