"""一个回合的输入上下文。

提示词只从这里取数据：人格、准则、工具说明由 PromptBuilder 负责，
presence / tasks / world / memory 由 runtime 填好后交给 `context_text()`。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, cast

from openai.types.chat import ChatCompletionMessageParam

from agent.cosmos.contracts import InboundEnvelope


@dataclass(slots=True)
class TurnContext:
    user_text: str = ""
    history: list[ChatCompletionMessageParam] = field(default_factory=list)
    presence: str | None = None
    tasks: str | None = None
    desktop: str | None = None
    world: str | None = None
    memory: str | None = None
    session_id: str = ""
    message_id: str = ""
    sent: bool = False
    proactive: bool = False
    #: 回合来源决定出站通道：用户回复 / 主动消息 / 事件汇报。
    origin: Literal["user", "proactive", "event"] = "user"
    #: 排队中的主动回合被用户活动取消后置位，工作循环直接跳过。
    canceled: bool = False
    #: 主动回合决定这次不开口：空回复不再触发兜底消息。
    silent: bool = False
    #: 这个回合由哪条未来意图触发；回合结束后据此结算或重试。
    intent_text: str = ""
    #: 队列满时是否允许丢弃；只有进度类汇报可以丢。
    droppable: bool = False
    replies: list[str] = field(default_factory=list)
    sender: Callable[[str], Awaitable[bool | None]] | None = field(default=None, repr=False)

    async def send(self, text: str) -> bool:
        """经出站检查后投递；被通道拒绝时不记为已发送。"""
        if self.sender is None:
            raise RuntimeError("回合没有配置发送器")
        accepted = await self.sender(text)
        if accepted is False:
            return False
        self.sent = True
        self.replies.append(text)
        return True

    @classmethod
    def from_envelope(cls, envelope: InboundEnvelope) -> TurnContext:
        history: list[ChatCompletionMessageParam] = [
            cast(
                "ChatCompletionMessageParam",
                cast(
                    "object",
                    {
                        "role": "user" if turn.role == "user" else "assistant",
                        "content": turn.text,
                    },
                ),
            )
            for turn in envelope.conversation.turns
        ]
        tasks = "\n".join(f"{task.label}：{task.status}" for task in envelope.tasks) or None
        return cls(
            user_text=envelope.message.text,
            history=history,
            presence=envelope.presence.summary if envelope.presence else None,
            tasks=tasks,
            memory=envelope.conversation.summary,
            session_id=envelope.message.session_id,
            message_id=envelope.message.message_id,
        )

    def context_text(self) -> str:
        """把可选上下文拼成一段；空段直接跳过。"""
        parts = [
            part.strip()
            for part in (self.memory, self.presence, self.tasks, self.desktop, self.world)
            if part is not None and part.strip()
        ]
        return "\n\n".join(parts)

    def history_messages(self) -> list[ChatCompletionMessageParam]:
        return list(self.history)
