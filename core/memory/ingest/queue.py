"""有界观测队列：记忆层的背压闸门。

**为什么过载走丢弃，而不进死信**（这段与设计文档 §6.5 末段同源，动之前请读完）：

对话比记忆快是**正常现象**——说话是即时的，抽取与落库要过 LLM、要写图库。
把队列溢出塞进死信队列，会让 DLQ 无限膨胀，而且重放永远追不上新来的对话，
等于把「忙」错当成「坏」。所以这里的分界是：

- **过载 ⇒ 丢队列里还没结构化的回合 + 计数器 `dropped` 累加**（调用方据此记 ERROR 告警）；
- **故障 ⇒ 进死信队列**（瞬时重试、永久或重试用尽才落 DLQ），那是重试模块的职责。

两者不是一回事：前者是「太多」，后者是「坏了」。队列只负责前者。

**为什么不在这里记日志**：队列是纯数据结构，真假与计数就是它的全部回答；
记日志（含上下文、健康指标）由调用方 `MemoryService` 编排时做，职责不重叠。

**队列里装 `Observation` 原型对象，不装 dict**：入队前载荷已经过校验，
直接持有 pydantic 模型更清晰；死信要 dict 时，由消费方在那一刻 `model_dump()`。
"""

from __future__ import annotations

import asyncio

from core.memory.models import Observation


class ObservationQueue:
    """有界观测队列：满则丢弃，永不阻塞入队方。

    内部就是一层 `asyncio.Queue(maxsize=capacity)`，把「满」的语义从**抛异常**
    改成**返回假并计数**——调用方拿到的永远是布尔值，不必处理 `QueueFull`。
    """

    def __init__(self, capacity: int) -> None:
        self._queue: asyncio.Queue[Observation] = asyncio.Queue(maxsize=capacity)
        self._dropped: int = 0
        self._closed: bool = False

    async def put(self, observation: Observation) -> bool:
        """把观测交给队列：成功为真，被丢弃（满或已关闭）为假。

        用 `put_nowait` 而非 `await put`：容量满时**立刻**返回假，
        绝不等待消费者腾位——记忆层再忙也不能拖慢对话循环。
        """
        if self._closed:
            return False
        try:
            self._queue.put_nowait(observation)
        except asyncio.QueueFull:
            self._dropped += 1
            return False
        return True

    async def get(self) -> Observation:
        """取出一条观测；队列空时等待下一条到来。

        **不要在 `close()` 之后调用本方法**：关闭只栅住入队，不给 `get` 留出口，
        也没有哨兵——空队列上它会**永久阻塞**（见 `close()` 的契约）。消费者是被
        `cancel` 拉出来的，不是靠 `get` 返回。
        """
        return await self._queue.get()

    def close(self) -> None:
        """置关闭标志：之后入队一律被拒，免得 stop() 期间调用方吊死。幂等。

        这里要说清它**只管一半**，别把「关闭」理解成「排空后优雅收尾」：

        - 关闭**只影响** `put`：此后的入队一律返回 `False`（观测被丢弃，不计入 `dropped`）；
        - **关闭后 `get()` 的行为未定义**——队列里没有哨兵，在空队列上它会一直等下去；
        - 因此调用方的契约是：`close()` 之后必须主动 **cancel** 正在 `get()` 的消费者
          （`MemoryService.stop()` 正是 `close()` → `worker.cancel()` →
          `await gather(worker, return_exceptions=True)`），**不得**指望 `get()` 自己返回。

        换成白话：`close()` 之后**永远不该**再调 `get()`。这条不变量由
        `tests/test_memory_queue.py::test_关闭后拒绝入队` 与
        `::test_关闭后对空队列取用或永久阻塞_契约` 共同锁定。
        """
        self._closed = True

    @property
    def depth(self) -> int:
        """当前队列深度（尚未被消费的观测条数）。"""
        return self._queue.qsize()

    @property
    def dropped(self) -> int:
        """累计丢弃条数（过载计数的唯一出口，供调用方记 ERROR）。"""
        return self._dropped
