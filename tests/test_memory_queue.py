"""记忆层有界观测队列测试。

本文件锁住三条不变量，任何一条被「优化掉」都会当场变红：

1. **入队即返回**：`put` 把观测交给队列后立刻回话，调用方不被吊住。
2. **绝不阻塞**：容量满时 `put` 立刻返回假并计数丢弃，**不等消费者腾位**。
   这条是背压策略的命门——记忆层再忙也不能拖慢对话循环。
3. **过载 ≠ 故障**：满队列只丢弃 + 计数，不进死信（设计文档 §6.2 第 3 条、
   §6.5 末段）。队列只返回真假与计数，不在此处记日志、不做重试。
"""

from __future__ import annotations

from core.memory.ingest.queue import ObservationQueue
from core.memory.models import Observation


def _观测(文本: str) -> Observation:
    """造一条最小合法观测：对话框里的发言。"""
    return Observation(kind="dialogue", text=文本)


async def test_入队即返回() -> None:
    """put 后 get 能取回同一条观测。"""
    queue = ObservationQueue(capacity=4)
    _ = await queue.put(_观测("甲"))
    assert await queue.get() == _观测("甲")


async def test_满队列丢弃并记数() -> None:
    """过载走丢弃 + 告警，与故障走死信是两回事（设计文档 6.5）。"""
    queue = ObservationQueue(capacity=2)
    assert await queue.put(_观测("甲")) is True
    assert await queue.put(_观测("乙")) is True
    assert await queue.put(_观测("丙")) is False
    assert queue.dropped == 1


async def test_关闭后拒绝入队() -> None:
    """close 之后的观测一律拒收，免得 stop() 期间吊死调用方。"""
    queue = ObservationQueue(capacity=2)
    queue.close()
    assert await queue.put(_观测("甲")) is False


async def test_关闭幂等() -> None:
    """反复 close 不报错，也不重复清空或改变计数语义。"""
    queue = ObservationQueue(capacity=2)
    _ = await queue.put(_观测("甲"))
    queue.close()
    queue.close()
    assert queue.depth == 1


async def test_深度随入队出队变化() -> None:
    """depth 是只读属性：空时为 0，入队后累加，出队后回落。"""
    queue = ObservationQueue(capacity=3)
    assert queue.depth == 0
    _ = await queue.put(_观测("甲"))
    _ = await queue.put(_观测("乙"))
    assert queue.depth == 2
    _ = await queue.get()
    assert queue.depth == 1


async def test_满队列连续入队不阻塞() -> None:
    """满容量下连塞 N 次必须立刻返回，绝不等待消费者腾位。

    若把 put_nowait 换成 await queue.put(...)，消费者缺席时这里会一直挂住，
    整条用例超时变红——这正是「绝不阻塞」的证伪开关。
    用不抛 asyncio.QueueFull 的语义把这条钉死：队列满只回假，不把异常推给调用方。
    """
    queue = ObservationQueue(capacity=1)
    assert await queue.put(_观测("甲")) is True
    for i in range(50):
        assert await queue.put(_观测(f"溢出{i}")) is False
    assert queue.dropped == 50
