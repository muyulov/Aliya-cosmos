"""默认用户通道：把发出的消息收进内存队列。

真实通道（WebSocket / CLI）由部署方实现并注入；这份实现供测试与本地联调
读取「用户实际收到了什么」。
"""

from __future__ import annotations

from collections import deque

from agent.cosmos.contracts import TerminalMessage


class MemoryUserChannel:
    """内存用户通道。"""

    def __init__(self, limit: int = 1000) -> None:
        self._messages: deque[TerminalMessage] = deque(maxlen=limit)

    async def send(self, message: TerminalMessage) -> None:
        """记录一条待用户接收的消息。"""
        self._messages.append(message)

    def drain(self) -> list[TerminalMessage]:
        """取走全部消息并清空队列。"""
        messages = list(self._messages)
        self._messages.clear()
        return messages

    def __len__(self) -> int:
        return len(self._messages)
