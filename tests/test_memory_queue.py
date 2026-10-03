"""记忆层有界观测队列测试。

本文件锁住四条不变量，任何一条被「优化掉」都会当场变红：

1. **入队即返回**：`put` 把观测交给队列后立刻回话，调用方不被吊住。
2. **不吊死**：容量满时 `put` 立刻返回假并计数丢弃，**不等消费者腾位**。
   这条是背压策略的命门——记忆层再忙也不能拖慢对话循环。
3. **过载 ≠ 故障**：满队列只丢弃 + 计数，不进死信（设计文档 §6.2 第 3 条、
   §6.5 末段）。队列只返回真假与计数，不在此处记日志、不做重试。
4. **关闭契约**：`close()` 只栅住入队；**关闭后 `get()` 行为未定义**，
   空队列上会永久阻塞，因此调用方（`MemoryService.stop()`）必须 cancel 消费者。

关于「不吊死」的**证明边界**（请勿再把它说成「已证伪阻塞」）：
`test_满队列连续入队不阻塞` 证伪的是**无界等待**——消费者缺席时不会永久阻塞。
它**不等于**「任何形式的阻塞都不会发生」：若把满时路径改成「有界但很慢」
（例如 `async with asyncio.timeout(0.05): await self._queue.put(...)`），
该用例照样全绿。所以另有 `test_满队列连续入队耗时上界` 作为**宽松上界护栏**
兜住「有界但很慢」，两条互补、不是二选一。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from core.memory.ingest.queue import ObservationQueue, PendingObservation
from core.memory.models import Observation

# 「有界但很慢」护栏的阈值：**不是性能指标**，只用来拒绝「满时还去等」的实现。
# 本地实测纯满拒绝 50 次约 1e-4 秒，这里留了四个数量级余量，CI 上不会抖。
满拒绝耗时上界秒 = 1.0


def _观测(文本: str) -> Observation:
    """造一条最小合法观测：对话框里的发言。"""
    return Observation(kind="dialogue", text=文本)


def _待处理(文本: str) -> PendingObservation:
    """造一条待处理观测：载荷配上入队即定的主键（队列元素类型）。

    主键与文本一一对应，便于断言「取回的正是入队那一份」。
    """
    return PendingObservation(utterance_id=f"u-{文本}", observation=_观测(文本))


async def test_入队即返回() -> None:
    """put 后 get 能取回同一条观测。"""
    queue = ObservationQueue(capacity=4)
    _ = await queue.put(_待处理("甲"))
    assert await queue.get() == _待处理("甲")


async def test_满队列丢弃并记数() -> None:
    """过载走丢弃 + 告警，与故障走死信是两回事（设计文档 6.5）。"""
    queue = ObservationQueue(capacity=2)
    assert await queue.put(_待处理("甲")) is True
    assert await queue.put(_待处理("乙")) is True
    assert await queue.put(_待处理("丙")) is False
    assert queue.dropped == 1


async def test_关闭后拒绝入队() -> None:
    """close 之后的观测一律拒收，免得 stop() 期间吊死调用方。"""
    queue = ObservationQueue(capacity=2)
    queue.close()
    assert await queue.put(_待处理("甲")) is False


async def test_关闭幂等() -> None:
    """反复 close 不报错，也不重复清空或改变计数语义。"""
    queue = ObservationQueue(capacity=2)
    _ = await queue.put(_待处理("甲"))
    queue.close()
    queue.close()
    assert queue.depth == 1


async def test_深度随入队出队变化() -> None:
    """depth 是只读属性：空时为 0，入队后累加，出队后回落。"""
    queue = ObservationQueue(capacity=3)
    assert queue.depth == 0
    _ = await queue.put(_待处理("甲"))
    _ = await queue.put(_待处理("乙"))
    assert queue.depth == 2
    _ = await queue.get()
    assert queue.depth == 1


async def test_满队列连续入队不阻塞() -> None:
    """满容量下连塞 N 次必须立刻返回，绝不等待消费者腾位。

    **本用例证伪的是「无界等待」**：若把 put_nowait 换成 await queue.put(...)，
    消费者缺席时这里会永久挂住，整条用例被外部超时杀掉——这是它的证伪开关。
    用不抛 asyncio.QueueFull 的语义把这条钉死：队列满只回假，不把异常推给调用方。

    **但它不等于「任何形式的阻塞都不会发生」**：把满时路径改成「有界但很慢」
    （例如 `async with asyncio.timeout(0.05): await self._queue.put(...)`），
    下面的断言照样全绿。所以「有界但很慢」由 `test_满队列连续入队耗时上界`
    单独兜住——两条互补，不是二选一。
    """
    queue = ObservationQueue(capacity=1)
    assert await queue.put(_待处理("甲")) is True
    for i in range(50):
        assert await queue.put(_待处理(f"溢出{i}")) is False
    assert queue.dropped == 50


async def test_满队列连续入队耗时上界() -> None:
    """满容量下连续入队的**总耗时**必须远低于一个宽松上界。

    这条是「有界但很慢」的护栏：它不拦「永久吊死」（那是前一条的活），
    只拦「满时还去等」——哪怕只等 50ms，50 次也会累积到秒级而触发红线。
    阈值 `满拒绝耗时上界秒` 是纯护栏、不是性能指标，余量四个数量级。
    """
    queue = ObservationQueue(capacity=1)
    assert await queue.put(_待处理("甲")) is True
    起点 = time.monotonic()
    for i in range(50):
        assert await queue.put(_待处理(f"溢出{i}")) is False
    耗时 = time.monotonic() - 起点
    assert queue.dropped == 50
    assert 耗时 < 满拒绝耗时上界秒, f"满队列入队耗时 {耗时:.4f}s，超出上界 {满拒绝耗时上界秒}s"


async def test_关闭后对空队列取用或永久阻塞_契约() -> None:
    """锁定 close() 的契约：只栅入队，不给 get 留出口（无哨兵）。

    用 wait_for 证明「close 之后空队列上 get 不会返回」，从而说明消费者必须被
    cancel 拉出（`MemoryService.stop()` 的顺序正是 close → cancel → gather），
    不得指望 get 自己返回。给 50ms 足够了：合法实现里这条分支永不返回。
    """
    queue = ObservationQueue(capacity=1)
    queue.close()
    with pytest.raises(TimeoutError):
        _ = await asyncio.wait_for(queue.get(), timeout=0.05)
