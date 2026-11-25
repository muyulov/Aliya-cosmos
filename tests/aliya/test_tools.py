import pytest

from agent.aliya.turn.tools import build_tools
from core.llm import ToolCall


class FakeRuntime:
    def __init__(self) -> None:
        self.delivered: list[str] = []
        self.remembered: list[tuple[str, str]] = []

    async def send(self, text: str) -> None:
        self.delivered.append(text)

    async def describe_desktop(self) -> str:
        return "桌面：翻译、检索"

    async def dispatch(
        self,
        task_text: str,
        *,
        agent_id: str | None = None,
        params: dict[str, object] | None = None,
        hint: str | None = None,
    ) -> str:
        return f"已派：{task_text}"

    async def task_status(self, task_id: str | None = None) -> str:
        return "进行中" if task_id is None else f"{task_id} 进行中"

    async def cancel(self, task_id: str) -> str:
        return f"已取消：{task_id}"

    async def retry(self, task_id: str) -> str:
        return f"已重试：{task_id}"

    async def refresh_world(self) -> str:
        return "夜里，海面很静"

    async def remember(self, note: str, kind: str) -> str:
        self.remembered.append((note, kind))
        return "记住了"


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime()


@pytest.mark.asyncio
async def test_send_message_maps_to_deliver(runtime: FakeRuntime) -> None:
    registry = build_tools(runtime)  # type: ignore[arg-type]
    result = await registry.execute(
        ToolCall(id="1", name="send_message", arguments='{"text":"在吗"}')
    )
    assert "已发送" in result
    assert runtime.delivered == ["在吗"]


def test_proactive_toolset_is_small(runtime: FakeRuntime) -> None:
    registry = build_tools(runtime, proactive=True)  # type: ignore[arg-type]
    assert set(registry.names) == {
        "send_message",
        "see_desktop",
        "task_status",
        "refresh_world",
        "remember",
    }


def test_normal_toolset_has_eight(runtime: FakeRuntime) -> None:
    registry = build_tools(runtime)  # type: ignore[arg-type]
    assert set(registry.names) == {
        "send_message",
        "see_desktop",
        "dispatch_task",
        "task_status",
        "cancel_task",
        "retry_task",
        "refresh_world",
        "remember",
    }


@pytest.mark.asyncio
async def test_remember_records_kind(runtime: FakeRuntime) -> None:
    registry = build_tools(runtime)  # type: ignore[arg-type]
    await registry.execute(
        ToolCall(
            id="2",
            name="remember",
            arguments='{"note":"她说风信子开了","kind":"anchor"}',
        )
    )
    assert runtime.remembered == [("她说风信子开了", "anchor")]
