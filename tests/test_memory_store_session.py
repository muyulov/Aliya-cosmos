"""记忆层 Kùzu 会话测试。

Kùzu 的 Python API 是同步且线程不亲和的，会话把全部调用沉进**单线程执行器**。
本文件最要紧的一条是 `test_全部语句跑在同一个线程上`：它锁住「线程亲和」这个
核心不变量，任何把执行器换成 `asyncio.to_thread`（默认多线程池）的「优化」
都会被它当场打红。
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import cast

import kuzu
import pytest

from core.memory import MemoryStoreError
from core.memory.store import MemorySession


class _FakeResult:
    """冒牌的 Kùzu 查询结果：没有下一行。"""

    def has_next(self) -> bool:
        return False

    def get_next(self) -> list[object]:
        raise AssertionError("空结果不应被取行")


class _ThreadProbeConnection:
    """冒牌连接：每次 execute 记下当前线程 id，并短暂占住线程以制造并发重叠。

    用它替换真连接后，`run` 依旧走会话的执行器，因此 `execute` 一定运行在
    执行器线程上——记下的就是「这条语句实际是在哪个线程跑的」。
    """

    def __init__(self) -> None:
        self.thread_ids: list[int] = []

    def execute(self, query: str) -> _FakeResult:
        _ = query  # 探针不关心语句内容，只为占住执行器线程
        self.thread_ids.append(threading.get_ident())
        time.sleep(0.05)
        return _FakeResult()

    def close(self) -> None:
        """关闭时也会被会话调一次，冒牌件给个空实现即可。"""


async def test_会话可打开与关闭(tmp_path: Path) -> None:
    """打开即建库目录；关闭后状态复位。"""
    session = MemorySession(tmp_path / "db", dimensions=4)

    await session.open()
    assert (tmp_path / "db").exists()

    await session.close()
    assert session.opened is False


async def test_同步语句经执行器跑通(tmp_path: Path) -> None:
    session = MemorySession(tmp_path / "db", dimensions=4)

    await session.open()
    assert await session.run("RETURN 1 AS x") == [[1]]

    await session.close()


async def test_事务可回滚(tmp_path: Path) -> None:
    """建表后插一行、回滚，计数不变——证明 BEGIN/ROLLBACK 真的生效。"""
    session = MemorySession(tmp_path / "db", dimensions=4)

    await session.open()
    _ = await session.run("CREATE NODE TABLE T(id STRING, PRIMARY KEY(id))")
    _ = await session.run("BEGIN TRANSACTION")
    _ = await session.run("CREATE (:T {id:'a'})")
    _ = await session.run("ROLLBACK")

    assert await session.run("MATCH (t:T) RETURN count(t)") == [[0]]

    await session.close()


async def test_事务可提交(tmp_path: Path) -> None:
    """回滚的反面：提交后计数留下。"""
    session = MemorySession(tmp_path / "db", dimensions=4)

    await session.open()
    _ = await session.run("CREATE NODE TABLE T(id STRING, PRIMARY KEY(id))")
    _ = await session.run("BEGIN TRANSACTION")
    _ = await session.run("CREATE (:T {id:'a'})")
    _ = await session.run("COMMIT")

    assert await session.run("MATCH (t:T) RETURN count(t)") == [[1]]

    await session.close()


async def test_查询出错包成存储错误(tmp_path: Path) -> None:
    """Kùzu 的 RuntimeError 不能裸抛，否则调用方兜不住。"""
    session = MemorySession(tmp_path / "db", dimensions=4)

    await session.open()
    with pytest.raises(MemoryStoreError) as captured:
        _ = await session.run("这不是 Cypher")

    assert captured.value.operation == "run"
    assert isinstance(captured.value.__cause__, RuntimeError)

    await session.close()


async def test_未打开就执行报存储错误(tmp_path: Path) -> None:
    session = MemorySession(tmp_path / "db", dimensions=4)

    with pytest.raises(MemoryStoreError) as captured:
        _ = await session.run("RETURN 1")

    assert captured.value.operation == "run"


async def test_打开幂等(tmp_path: Path) -> None:
    """连开两次不炸，第二次直接返回。"""
    session = MemorySession(tmp_path / "db", dimensions=4)

    await session.open()
    await session.open()

    assert session.opened is True
    assert await session.run("RETURN 1 AS x") == [[1]]

    await session.close()


async def test_关闭幂等(tmp_path: Path) -> None:
    session = MemorySession(tmp_path / "db", dimensions=4)

    await session.open()
    await session.close()
    await session.close()

    assert session.opened is False


async def test_关闭从未打开的实例也不炸(tmp_path: Path) -> None:
    """回滚场景会这么调：start() 半途失败了，stop() 仍会被作用到。"""
    session = MemorySession(tmp_path / "db", dimensions=4)

    await session.close()

    assert session.opened is False


async def test_关闭之后不能再执行(tmp_path: Path) -> None:
    session = MemorySession(tmp_path / "db", dimensions=4)

    await session.open()
    await session.close()

    with pytest.raises(MemoryStoreError) as captured:
        _ = await session.run("RETURN 1")

    assert captured.value.operation == "run"


async def test_打开之前属性取值(tmp_path: Path) -> None:
    session = MemorySession(tmp_path / "db", dimensions=4)

    assert session.opened is False
    assert session.dimensions == 4


async def test_打开失败不留悬挂执行器(tmp_path: Path) -> None:
    """开库半途失败要把执行器收掉，实例退回「未打开」——否则线程池会漏。"""
    session = MemorySession(tmp_path / "db", dimensions=4)

    def _boom() -> tuple[kuzu.Database, kuzu.Connection]:
        raise RuntimeError("开库失败")

    session._open_sync = _boom  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(RuntimeError):
        await session.open()

    assert session.opened is False
    # 半途失败后 close() 必须仍然安全（此时三样资源都是 None）
    await session.close()


async def test_无执行器时下沉报存储错误(tmp_path: Path) -> None:
    """白盒：连接在、执行器被抽走这种不该出现的状态，下沉时要报存储错误而非崩溃。"""
    session = MemorySession(tmp_path / "db", dimensions=4)
    await session.open()
    session._executor = None  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(MemoryStoreError) as captured:
        _ = await session._in_thread(lambda: 1)  # pyright: ignore[reportPrivateUsage]

    assert captured.value.operation == "run"

    await session.close()


async def test_全部语句跑在同一个线程上(tmp_path: Path) -> None:
    """核心不变量：Kùzu 的 Connection 不是线程安全的，所有调用必须在同一个线程。

    探针必须是**并发重叠**的：单发两次调用即便落到默认多线程池也可能复用同一个
    空闲线程，区分不出来。这里一次性并发 8 条，让它们在同一时刻争线程——
    `max_workers=1` 会把它们排成一条线（同一个 id），而 `asyncio.to_thread`
    的共享池会扩容到多个线程（多个 id）。
    """
    session = MemorySession(tmp_path / "db", dimensions=4)
    await session.open()
    probe = _ThreadProbeConnection()
    # 白盒注入：冒牌件顶替真连接，只为记下语句实际跑在哪个线程。
    # 先转 object 再转目标类型（双重 cast），否则会撞 reportInvalidCast。
    session._conn = cast("kuzu.Connection", cast("object", probe))  # pyright: ignore[reportPrivateUsage]

    result = await asyncio.gather(*[session.run(f"RETURN {n}") for n in range(8)])

    assert result == [[] for _ in range(8)]
    assert len(set(probe.thread_ids)) == 1

    await session.close()
