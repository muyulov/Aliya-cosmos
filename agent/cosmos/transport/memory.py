"""进程内总线实现。

三条语义（设计 09 章 7 节）：

- 消息总线：每个订阅者独立队列（广播），无订阅者时进端点 backlog；
  队列满时丢最旧并 WARNING，不阻塞发布方。
- 事件总线：`sequence` 按主题递增、`event_id` 全局唯一、每主题环形缓冲；
  `since_sequence` 补发缺口，缺口超过 100 条时抛 `MessageDeliveryError`，
  由 `ControlService` 改走全量快照。
- 关闭：往所有队列放哨兵唤醒等待者，`subscribe` 的 `async for` 自然退出。
"""

from __future__ import annotations

import asyncio
import uuid
from collections import deque
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from datetime import datetime
from typing import cast

from agent.cosmos.contracts import CosmosEvent, InboundEnvelope
from agent.cosmos.errors import MessageDeliveryError
from core.logger import log

#: 队列关闭哨兵
_CLOSED: object = object()

#: `since_sequence` 补发上限：超过它说明缺口太大，改拉全量快照更划算
MAX_REPLAY = 100


def _new_id(prefix: str) -> str:
    """标识统一用「前缀 + uuid4 十六进制」，不引第三方 ULID 依赖。"""
    return f"{prefix}{uuid.uuid4().hex}"


class MemoryMessageBus:
    """进程内消息总线。"""

    def __init__(self, limit: int = 1000) -> None:
        self._limit: int = limit
        #: 端点 → 订阅者队列（广播）
        self._subscribers: dict[str, list[asyncio.Queue[object]]] = {}
        #: 端点 → 无订阅者期间的积压
        self._backlog: dict[str, deque[InboundEnvelope]] = {}
        self._closed: bool = False

    async def publish(self, endpoint: str, envelope: InboundEnvelope) -> None:
        """发布一条信封：有订阅者则广播，否则进 backlog。"""
        if self._closed:
            return
        queues = self._subscribers.get(endpoint, [])
        if not queues:
            self._push_backlog(endpoint, envelope)
            return
        for queue in queues:
            self._put(queue, envelope, endpoint)

    def subscribe(self, endpoint: str) -> AsyncGenerator[InboundEnvelope, None]:
        """订阅端点，返回异步迭代器；退出 `async for` 即取消订阅。

        注册在调用时完成（而不是首次 `anext` 时）：两个订阅者先后建立时，
        后建立的那个也能收到从它订阅那一刻起的全部消息。
        """
        queue: asyncio.Queue[object] = asyncio.Queue(maxsize=self._limit)
        # 先把积压搬进新队列，再注册：注册之后的实时消息不会插到积压之前
        for envelope in self._backlog.pop(endpoint, deque()):
            queue.put_nowait(envelope)
        self._subscribers.setdefault(endpoint, []).append(queue)
        return self._iterate(endpoint, queue)

    async def close(self) -> None:
        """关闭所有订阅者队列（幂等）；backlog 一并清空。"""
        if self._closed:
            return
        self._closed = True
        for queues in self._subscribers.values():
            for queue in queues:
                self._put_raw(queue, _CLOSED)
        self._subscribers.clear()
        self._backlog.clear()

    def _push_backlog(self, endpoint: str, envelope: InboundEnvelope) -> None:
        backlog = self._backlog.setdefault(endpoint, deque())
        if len(backlog) >= self._limit:
            _ = backlog.popleft()
            log.warning("端点无订阅者且积压已满，丢弃最旧消息", 端点=endpoint)
        backlog.append(envelope)

    def _put(self, queue: asyncio.Queue[object], envelope: InboundEnvelope, endpoint: str) -> None:
        if queue.full():
            _ = queue.get_nowait()
            log.warning("消息消费者过慢，已丢弃最旧消息", 端点=endpoint)
        self._put_raw(queue, envelope)

    @staticmethod
    def _put_raw(queue: asyncio.Queue[object], item: object) -> None:
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            # 关闭与发布竞争的极端情形：丢弃，不阻塞发布方
            log.warning("消息队列已满，丢弃一条消息")
            _ = item

    async def _iterate(
        self, endpoint: str, queue: asyncio.Queue[object]
    ) -> AsyncGenerator[InboundEnvelope, None]:
        try:
            while True:
                item = await queue.get()
                if item is _CLOSED:
                    return
                yield cast("InboundEnvelope", item)
        finally:
            queues = self._subscribers.get(endpoint)
            if queues is not None:
                if queue in queues:
                    queues.remove(queue)
                if not queues:
                    _ = self._subscribers.pop(endpoint, None)


class MemoryEventBus:
    """进程内事件总线：按主题递增序列 + 环形缓冲 + 补发。"""

    def __init__(
        self,
        now: Callable[[], datetime],
        *,
        buffer_limit: int = 1000,
        subscriber_limit: int = 1000,
    ) -> None:
        self._now: Callable[[], datetime] = now
        self._buffer_limit: int = buffer_limit
        self._subscriber_limit: int = subscriber_limit
        self._sequences: dict[str, int] = {}
        self._buffers: dict[str, deque[CosmosEvent]] = {}
        self._subscribers: dict[str, list[asyncio.Queue[object]]] = {}
        self._closed: bool = False

    async def publish(self, topic: str, payload: object) -> CosmosEvent:
        """发布事件：先写缓冲，再广播给订阅者。"""
        sequence = self._sequences.get(topic, 0) + 1
        self._sequences[topic] = sequence
        event = CosmosEvent(
            event_id=_new_id("evt_"),
            topic=topic,
            sequence=sequence,
            at=self._now(),
            payload=_to_payload(payload),
        )
        buffer = self._buffers.setdefault(topic, deque(maxlen=self._buffer_limit))
        buffer.append(event)
        if self._closed:
            return event
        for queue in self._subscribers.get(topic, []):
            self._put(queue, event, topic)
        return event

    def subscribe(
        self,
        topics: Sequence[str],
        *,
        since_sequence: int | None = None,
        cursors: Mapping[str, int] | None = None,
    ) -> AsyncGenerator[CosmosEvent, None]:
        """订阅若干主题：先补发缺口，再接收实时事件。

        `cursors` 优先于 `since_sequence`：每个主题按自己的断点补发，避免
        一个落后主题把其他主题的游标一起拉回。注册与补发都在调用时完成：
        缺口过大直接在订阅点抛出，不必等到第一次迭代才暴露；两个订阅者也
        不会互相偷走积压。
        """
        topic_list = list(topics)
        queue: asyncio.Queue[object] = asyncio.Queue(maxsize=self._subscriber_limit)
        for event in self._collect_replay(topic_list, since_sequence, cursors):
            queue.put_nowait(event)
        for topic in topic_list:
            self._subscribers.setdefault(topic, []).append(queue)
        return self._iterate(topic_list, queue)

    async def close(self) -> None:
        """关闭所有订阅者队列（幂等）。"""
        if self._closed:
            return
        self._closed = True
        for queues in self._subscribers.values():
            for queue in queues:
                self._put_raw(queue, _CLOSED)
        self._subscribers.clear()

    async def _iterate(
        self, topics: list[str], queue: asyncio.Queue[object]
    ) -> AsyncGenerator[CosmosEvent, None]:
        try:
            while True:
                item = await queue.get()
                if item is _CLOSED:
                    return
                yield cast("CosmosEvent", item)
        finally:
            for topic in topics:
                queues = self._subscribers.get(topic)
                if queues is not None and queue in queues:
                    queues.remove(queue)
                    if not queues:
                        _ = self._subscribers.pop(topic, None)

    def _collect_replay(
        self,
        topics: list[str],
        since_sequence: int | None,
        cursors: Mapping[str, int] | None,
    ) -> list[CosmosEvent]:
        """按主题取缺口；缺口过大直接抛错，不做静默截断。"""
        if since_sequence is None and not cursors:
            return []
        replay: list[CosmosEvent] = []
        for topic in topics:
            cursor = since_sequence
            if cursors is not None and topic in cursors:
                cursor = cursors[topic]
            if cursor is None:
                continue
            head = self._sequences.get(topic, 0)
            if head - cursor > MAX_REPLAY:
                raise MessageDeliveryError(
                    f"事件缺口过大：{topic} 缺少 {head - cursor} 条",
                    code="gap_too_large",
                )
            replay.extend(
                event
                for event in self._buffers.get(topic, deque())
                if event.sequence > cursor
            )
        return replay

    def _put(self, queue: asyncio.Queue[object], event: CosmosEvent, topic: str) -> None:
        if queue.full():
            _ = queue.get_nowait()
            log.warning("事件消费者过慢，已丢弃最旧事件", 主题=topic)
            self._put_raw(queue, self._slow_consumer_event(topic))
        self._put_raw(queue, event)

    def _slow_consumer_event(self, topic: str) -> CosmosEvent:
        """为慢消费单独构造一条告警事件：不走 publish，避免递归。"""
        sequence = self._sequences.get(topic, 0)
        return CosmosEvent(
            event_id=_new_id("evt_"),
            topic=topic,
            sequence=sequence,
            at=self._now(),
            payload={"alert": "slow_consumer"},
        )

    @staticmethod
    def _put_raw(queue: asyncio.Queue[object], item: object) -> None:
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            log.warning("事件队列已满，丢弃一条事件")
            _ = item


def _to_payload(payload: object) -> dict[str, object]:
    """把事件载荷统一成可 JSON 序列化的 dict。"""
    if isinstance(payload, dict):
        return cast("dict[str, object]", payload)
    dump = getattr(payload, "model_dump", None)
    if callable(dump):
        dumped = dump(mode="json")
        if isinstance(dumped, dict):
            return cast("dict[str, object]", dumped)
    return {"value": payload}
