from datetime import UTC, datetime

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
        #: 模拟 AliyaRuntime 的 _current_turn：工具发送后同步回合的 sent 状态
        self.turn: TurnContext | None = None

    def now(self) -> datetime:
        return datetime(2026, 11, 26, 21, 5, tzinfo=UTC)

    async def send(self, text: str) -> None:
        self.delivered.append(text)
        if self.turn is not None:
            self.turn.sent = True
            self.turn.replies.append(text)

    async def describe_desktop(self) -> str:
        return "桌面"

    async def dispatch(self, task_text: str, **_kwargs: object) -> str:
        return f"已派：{task_text}"

    async def task_status(self, task_id: str | None = None) -> str:
        _ = task_id  # 协议参数名必须保留，用占位赋值消除未使用告警
        return "进行中"

    async def cancel(self, task_id: str) -> str:
        return f"已取消：{task_id}"

    async def retry(self, task_id: str) -> str:
        return f"已重试：{task_id}"

    async def refresh_world(self) -> str:
        return "夜里"

    async def remember(self, note: str, kind: str) -> str:
        _ = note, kind  # 协议参数名必须保留，用占位赋值消除未使用告警
        return "记住了"

    async def check_memory(self, scope: str | None = None) -> str:
        _ = scope
        return "锚点：风信子"

    async def resolve_topic(self, topic: str) -> str:
        return f"已经收尾了：{topic}"

    async def plan_followup(self, text: str, after_minutes: int) -> str:
        return f"记下了：{text}（{after_minutes} 分钟后）"

    async def cancel_followup(self, text: str) -> str:
        return f"已经取消了：{text}"

    async def defer(self, minutes: int, reason: str = "") -> str:
        _ = reason
        return f"延后 {minutes} 分钟"


class FakeLLM:
    def __init__(self) -> None:
        self.replies: list[AssistantReply] = []
        self.calls: int = 0

    async def chat_tools(self, _messages: object, _tools: object) -> AssistantReply:
        self.calls += 1
        return self.replies.pop(0)


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime()


def make_runner(llm: FakeLLM, runtime: FakeRuntime) -> TurnRunner:
    return TurnRunner(
        llm=llm,  # pyright: ignore[reportArgumentType]
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
            tool_calls=(ToolCall(id="1", name="send_message", arguments='{"text":"在吗"}'),),
        ),
        AssistantReply(content=None, tool_calls=()),
    ]
    runner = make_runner(llm, runtime)
    turn = TurnContext(user_text="在吗", sender=runtime.send)
    runtime.turn = turn
    await runner.run(turn=turn)
    assert runtime.delivered == ["在吗"]


@pytest.mark.asyncio
async def test_max_rounds_fallback(runtime: FakeRuntime) -> None:
    llm = FakeLLM()
    llm.replies = [
        AssistantReply(
            content=None,
            tool_calls=(ToolCall(id=str(i), name="see_desktop", arguments="{}"),),
        )
        for i in range(10)
    ]
    runner = make_runner(llm, runtime)
    await runner.run(turn=TurnContext(user_text="在吗", sender=runtime.send))
    assert runtime.delivered == [AgentAliyaSettings().reply.fallback_text]
    assert llm.calls == AgentAliyaSettings().reply.max_rounds


@pytest.mark.asyncio
async def test_empty_reply_falls_back(runtime: FakeRuntime) -> None:
    """模型既没调工具也没给正文时，必须发兜底回复而不是静默结束。"""
    llm = FakeLLM()
    llm.replies = [AssistantReply(content=None, tool_calls=())]
    runner = make_runner(llm, runtime)
    turn = TurnContext(user_text="在吗", sender=runtime.send)
    runtime.turn = turn
    await runner.run(turn=turn)
    assert runtime.delivered == [AgentAliyaSettings().reply.fallback_text]


@pytest.mark.asyncio
async def test_silent_turn_skips_fallback(runtime: FakeRuntime) -> None:
    """主动回合已决定不开口时，空回复不能再补一条兜底消息。"""
    llm = FakeLLM()
    llm.replies = [AssistantReply(content=None, tool_calls=())]
    runner = make_runner(llm, runtime)
    turn = TurnContext(
        user_text="",
        sender=runtime.send,
        proactive=True,
        origin="proactive",
        silent=True,
    )
    runtime.turn = turn
    await runner.run(turn=turn)
    assert runtime.delivered == []
