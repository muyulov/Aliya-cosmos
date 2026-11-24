"""`BusService`：进程内总线的生命周期宿主。

端口是接口，进程内实现只是默认；把它的启停交给容器，其他服务照常按类型
依赖注入。这样「换传输层」只换这一层实现，服务代码不动。
"""

from __future__ import annotations

from typing import ClassVar, override

from agent.cosmos.transport.memory import MemoryEventBus, MemoryMessageBus
from agent.cosmos.transport.ports import EventBusPort, MessageBusPort
from core.config import AgentSettings
from core.service import ClockService, Service


class BusService(Service):
    """持有 `MessageBus` 与 `EventBus` 的进程内实现。"""

    name: ClassVar[str] = "bus"
    dependencies: ClassVar[tuple[type[Service], ...]] = (ClockService,)

    def __init__(self, config: AgentSettings, clock: ClockService) -> None:
        super().__init__()
        #: 消息积压与订阅队列共用同一个上限（link.queue_limit）
        self._limit: int = config.cosmos.link.queue_limit
        self._clock: ClockService = clock
        self._messages: MemoryMessageBus = MemoryMessageBus(limit=self._limit)
        self._events: MemoryEventBus = MemoryEventBus(now=clock.now)
        self._closed: bool = False

    @property
    def messages(self) -> MessageBusPort:
        """对话消息端口。"""
        return self._messages

    @property
    def events(self) -> EventBusPort:
        """事件端口。"""
        return self._events

    @override
    async def start(self) -> None:
        """只做初始化，不建连接；重复调用不报错。"""
        if self.running:
            return
        if self._closed:
            # stop 之后再次 start：换一份干净的总线，避免把已关闭的队列交出去
            self._messages = MemoryMessageBus(limit=self._limit)
            self._events = MemoryEventBus(now=self._clock.now)
            self._closed = False

    @override
    async def stop(self) -> None:
        """关闭所有队列并唤醒等待者（幂等）。"""
        if self._closed:
            return
        self._closed = True
        await self._messages.close()
        await self._events.close()
