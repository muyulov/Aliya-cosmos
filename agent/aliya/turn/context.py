"""一个回合的输入上下文。

presence / tasks / world / memory 由 runtime 填好后交给 `context_text()`，
PromptBuilder 把它拼成一条独立的上下文消息（不进系统提示词）；
人格、准则、工具索引与技能索引由 PromptBuilder 自己负责。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, cast

from openai.types.chat import ChatCompletionMessageParam

from agent.cosmos.contracts import InboundEnvelope

_WEEKDAYS = ("一", "二", "三", "四", "五", "六", "日")


def format_moment(value: datetime) -> str:
    """把时间写成她读得懂的样子：日期 + 时分 + 星期。"""
    return f"{value:%Y-%m-%d %H:%M}（星期{_WEEKDAYS[value.weekday()]}）"


@dataclass(slots=True)
class TurnContext:
    user_text: str = ""
    history: list[ChatCompletionMessageParam] = field(default_factory=list)
    #: 本回合的当前时间；由 runtime 按控制面时区填入，避免模型凭感觉猜时段。
    now: datetime | None = None
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
    #: 主动回合决定这次不开口：空回复不再触发系统提示。
    silent: bool = False
    #: 这个回合由哪条未来意图触发；回合结束后据此结算或重试。
    intent_text: str = ""
    #: 队列满时是否允许丢弃；只有进度类汇报可以丢。
    droppable: bool = False
    replies: list[str] = field(default_factory=list)
    sender: Callable[[str], Awaitable[bool | None]] | None = field(default=None, repr=False)
    #: 系统提示发送器：失败提示以 system 身份投递，不记成 Aliya 的回复。
    system_sender: Callable[[str], Awaitable[bool | None]] | None = field(
        default=None, repr=False
    )

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

    async def send_system(self, text: str) -> bool:
        """投递系统提示：不置位 `sent`，也不写入 `replies`。

        失败提示不是 Aliya 说的话：它既不进对话轮次，也不进记忆与统计。
        """
        if self.system_sender is None:
            return False
        accepted = await self.system_sender(text)
        return accepted is not False

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
        """把可选上下文拼成一段；空段直接跳过。

        时间放最前面：模型不查 `current_time` 时也不会把下午当成清晨。
        """
        parts = [f"现在：{format_moment(self.now)}"] if self.now is not None else []
        parts.extend(
            part.strip()
            for part in (self.memory, self.presence, self.tasks, self.desktop, self.world)
            if part is not None and part.strip()
        )
        return "\n\n".join(parts)

    def history_messages(self) -> list[ChatCompletionMessageParam]:
        return list(self.history)
