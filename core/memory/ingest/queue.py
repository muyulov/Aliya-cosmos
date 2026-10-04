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

**队列里装 `IngestJob`（`PendingObservation` 或 `AccessWriteBack`），不装裸
`Observation`、更不装 dict**：入队前载荷已经过校验，直接持有 pydantic 模型更清晰；
死信要 dict 时，由消费方在那一刻 `model_dump()`。而把**主键**一起装进来是本模块对
Task 6 的**有意修改**——理由见 `PendingObservation` 的 docstring：只有入队时就固定
主键，`observe` 的返回值才能与最终落库的 `Utterance.id` 一致。召回的访问回写
（`AccessWriteBack`）走同一个队列，是为了让**所有写操作都排在同一个 worker 上**。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from core.memory.models import Observation


@dataclass(frozen=True, slots=True)
class PendingObservation:
    """待处理观测：一条载荷 + 它的**入队即定**的主键。

    这是对 Task 6 已审队列代码的**有意修改**（原来队列只装 `Observation`）：

    主键必须**在入队那一刻固定**，并由 `MemoryService.observe` 生成、原样返回给调用方。
    若让 worker 在消费时另生成一个 id 落库，调用方拿到的 id 就**永远不会出现在库里**——
    无法按 id 溯源、无法日志关联，违背设计文档 §6.1「`observe` 返回 `utterance_id`」的契约：
    那个 id 就应该是这条原文在库里的主键。故把 id 随载荷一起过队列。

    不可变且带 `slots=True`：它是跨越「入队 → 队列 → 出队」的传递载体，
    一次定下就不该在途中被改写（落库与写死信都读同一份 `utterance_id`）。
    """

    utterance_id: str
    observation: Observation


@dataclass(frozen=True, slots=True)
class AccessWriteBack:
    """一次召回的访问回写：把 `access_count` / `last_access` 交给 worker 串行落库。

    为什么经队列而不是在召回路径直写：这两个字段是衰减（期三）的输入，回写本身虽
    只是单条 `SET`、不涉事务，但它与写路径共用同一个 Kùzu 会话；把所有写操作都收在
    唯一写者（worker）上，才不会有「回写插进别人事务中间」这类隐患（设计文档 §7.2）。

    `moment` 是本次访问的时刻（世界时间优先，缺省取进程时间），写进 `last_access`。
    """

    utterance_ids: tuple[str, ...]
    moment: int


#: 队列元素：一条待处理观测，或一次访问回写。两者都由**同一个 worker** 串行落库。
type IngestJob = PendingObservation | AccessWriteBack


class ObservationQueue:
    """有界观测队列：满则丢弃，永不阻塞入队方。

    内部就是一层 `asyncio.Queue(maxsize=capacity)`，把「满」的语义从**抛异常**
    改成**返回假并计数**——调用方拿到的永远是布尔值，不必处理 `QueueFull`。
    元素是 `IngestJob`：待处理观测（载荷 + 入队即定的主键）或一次访问回写。
    """

    def __init__(self, capacity: int) -> None:
        self._queue: asyncio.Queue[IngestJob] = asyncio.Queue(maxsize=capacity)
        self._dropped: int = 0
        self._closed: bool = False

    async def put(self, job: IngestJob) -> bool:
        """把一件待处理工作交给队列：成功为真，被丢弃（满或已关闭）为假。

        用 `put_nowait` 而非 `await put`：容量满时**立刻**返回假，
        绝不等待消费者腾位——记忆层再忙也不能拖慢对话循环。
        """
        if self._closed:
            return False
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            self._dropped += 1
            return False
        return True

    async def get(self) -> IngestJob:
        """取出一件工作；队列空时等待下一件到来。

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
