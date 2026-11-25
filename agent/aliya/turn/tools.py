"""Aliya 的工具面：8 个工具，主动回合只给 5 个无副作用的。

用闭包把 runtime 绑进工具，避免 `self` 被当成工具参数。
"""

from __future__ import annotations

from typing import Protocol

from core.agent import ToolRegistry, tool


class RuntimePort(Protocol):
    async def send(self, text: str) -> None: ...

    async def describe_desktop(self) -> str: ...

    async def dispatch(
        self,
        task_text: str,
        *,
        agent_id: str | None = None,
        params: dict[str, object] | None = None,
        hint: str | None = None,
    ) -> str: ...

    async def task_status(self, task_id: str | None = None) -> str: ...

    async def cancel(self, task_id: str) -> str: ...

    async def retry(self, task_id: str) -> str: ...

    async def refresh_world(self) -> str: ...

    async def remember(self, note: str, kind: str) -> str: ...


def build_tools(runtime: RuntimePort, *, proactive: bool = False) -> ToolRegistry:
    @tool
    async def send_message(text: str) -> str:
        """给用户发一条消息。

        Args:
            text: 要发送的正文。
        """
        await runtime.send(text)
        return "已发送"

    @tool
    async def see_desktop() -> str:
        """看平板上现在有哪些工具和它们的状态。"""
        return await runtime.describe_desktop()

    @tool
    async def dispatch_task(
        task_text: str,
        agent_id: str | None = None,
        params: dict[str, object] | None = None,
        hint: str | None = None,
    ) -> str:
        """派人去做一件事。

        Args:
            task_text: 用自然语言描述要做的事。
            agent_id: 指定用哪个工具；不填则让她自己挑。
            params: 结构化参数。
            hint: 额外约束。
        """
        return await runtime.dispatch(
            task_text,
            agent_id=agent_id,
            params=params,
            hint=hint,
        )

    @tool
    async def task_status(task_id: str | None = None) -> str:
        """查任务进度。

        Args:
            task_id: 不填则返回最近的任务。
        """
        return await runtime.task_status(task_id)

    @tool
    async def cancel_task(task_id: str) -> str:
        """取消一个任务。

        Args:
            task_id: 要取消的任务。
        """
        return await runtime.cancel(task_id)

    @tool
    async def retry_task(task_id: str) -> str:
        """重试一个任务。

        Args:
            task_id: 要重试的任务。
        """
        return await runtime.retry(task_id)

    @tool
    async def refresh_world() -> str:
        """重新看一眼现在的情况。"""
        return await runtime.refresh_world()

    @tool
    async def remember(note: str, kind: str) -> str:
        """记住一件事。

        Args:
            note: 要记住的内容。
            kind: anchor / open_topic / unspoken / fact。
        """
        return await runtime.remember(note, kind)

    registry = ToolRegistry()
    if proactive:
        registry.register(send_message, see_desktop, task_status, refresh_world, remember)
    else:
        registry.register(
            send_message,
            see_desktop,
            dispatch_task,
            task_status,
            cancel_task,
            retry_task,
            refresh_world,
            remember,
        )
    return registry
