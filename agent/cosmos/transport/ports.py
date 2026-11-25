"""传输层端口。

设计 01 章 7 节：传输层是端口不是组件——`TerminalService` 只依赖端口，
不知道背后是 asyncio 队列还是 WebSocket。进程内实现由 `BusService` 承载生命周期。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping, Sequence
from typing import Protocol

from agent.cosmos.contracts import (
    CosmosEvent,
    InboundEnvelope,
    TerminalMessage,
    WorldInference,
    WorldInput,
)


class MessageBusPort(Protocol):
    """对话消息通道：按端点（`"aliya"`）收发信封。"""

    async def publish(self, endpoint: str, envelope: InboundEnvelope) -> None: ...

    def subscribe(self, endpoint: str) -> AsyncGenerator[InboundEnvelope, None]: ...


class EventBusPort(Protocol):
    """事件通道：按主题发布与订阅，支持按 `sequence` 补发。"""

    async def publish(self, topic: str, payload: object) -> CosmosEvent: ...

    def subscribe(
        self,
        topics: Sequence[str],
        *,
        since_sequence: int | None = None,
        cursors: Mapping[str, int] | None = None,
    ) -> AsyncGenerator[CosmosEvent, None]: ...


class UserChannelPort(Protocol):
    """用户侧投递端口；真实实现（WebSocket / CLI）由部署方提供。"""

    async def send(self, message: TerminalMessage) -> None: ...


class WorldReasoningPort(Protocol):
    """世界推理端口：单方法、无状态、可替换、可超时。"""

    async def infer(self, payload: WorldInput) -> WorldInference: ...
