"""Kùzu 会话：记忆层唯一接触图库的地方。

**为什么是单线程执行器**（这段最容易被后人"优化"掉，动之前请读完）：

1. Kùzu 的 Python API 全是**同步阻塞**的，直接 await 会卡住事件循环，所以必须
   沉到别的线程上执行。
2. 但 Kùzu 的 `Connection` **不是线程安全的**：同一连接在不同线程上交替使用会
   出现难以复现的崩溃与脏读。因此不能随手用 `asyncio.to_thread`——它把任务丢进
   默认的**多线程池**，每次调用可能落到不同线程上。
3. 折中是 `ThreadPoolExecutor(max_workers=1)`：既把阻塞移出事件循环，又保证所有
   语句永远排在**同一个线程**上跑，线程亲和由此得到硬保证。多出来的那点排队开销
   相对图库本身的耗时可以忽略。

改这里之前，先看一眼 `tests/test_memory_store_session.py::test_全部语句跑在同一个线程上`
——那条用例会在换回 `to_thread` 时立刻变红。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

import kuzu

from core.memory.errors import MemoryStoreError

#: 打开库后要装的扩展：向量索引与全文索引。实测 wheel 自带扩展，离线可用。
_EXTENSIONS: tuple[tuple[str, str], ...] = (
    ("INSTALL VECTOR", "LOAD VECTOR"),
    ("INSTALL FTS", "LOAD FTS"),
)

#: 库文件名。配置里的 `db_path` 是**目录**（一库一世界，换目录即换世界），
#: 而 Kùzu 的 `Database` 只接受**文件路径**（实测传已存在的目录会直接报
#: "Database path cannot be a directory"），故在目录里放一个固定名文件。
_DB_FILE = "memory.kuzu"


def _fetch(conn: kuzu.Connection, query: str) -> list[list[object]]:
    """执行一条语句并取回全部行（同步，必须在会话的执行器线程里调用）。

    取行用 `has_next()` / `get_next()`，**不要用 `get_as_df()`**——后者依赖 numpy，
    本仓没有装。实测纯 Python 取值不需要任何额外依赖。
    """
    # execute 的返回是 `QueryResult | list[QueryResult]`（多语句时才返回列表）。
    # 本层一律只传单条语句，故收窄到单个结果。
    result = cast("kuzu.QueryResult", conn.execute(query))
    rows: list[list[object]] = []
    while result.has_next():
        # get_next 声明为 `list[Any] | dict[str, Any]`，按行读到的就是 list。
        rows.append(cast("list[object]", result.get_next()))
    return rows


async def _run_in[V](executor: ThreadPoolExecutor, work: Callable[[], V]) -> V:
    """把同步调用丢到指定执行器上等结果。

    收成模块级函数而不是方法：`close()` 需要在把 `self._executor` 置空**之后**
    仍能借捕获到的执行器收尾，走方法会读到已置空的字段。
    """
    return await asyncio.get_running_loop().run_in_executor(executor, work)


class MemorySession:
    """Kùzu 会话：开库、开连接、执行语句。

    只负责"怎么跟图库说话"，不掺业务语句——Cypher 常量归 `queries.py`，
    表结构与版本归 `schema.py`。

    `__init__` 只赋值、不碰任何资源（本仓约定）：动资源的活一律留到 `open()`，
    免得"装配失败"与"启动失败"混成一锅。
    """

    def __init__(self, db_path: Path, *, dimensions: int) -> None:
        self._db_path: Path = db_path
        self._dimensions: int = dimensions
        self._db: kuzu.Database | None = None
        self._conn: kuzu.Connection | None = None
        self._executor: ThreadPoolExecutor | None = None

    @property
    def dimensions(self) -> int:
        """建表用的向量维度：`FLOAT[d]` 的 d 由配置给定，建表后不可改。"""
        return self._dimensions

    @property
    def opened(self) -> bool:
        """连接是否已建立。"""
        return self._conn is not None

    async def open(self) -> None:
        """建目录、开库、开连接。幂等：已打开直接返回。"""
        if self._conn is not None:
            return
        self._db_path.mkdir(parents=True, exist_ok=True)
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="memory-store")
        self._executor = executor
        try:
            self._db, self._conn = await self._in_thread(self._open_sync)
        except BaseException:
            # 半途失败不留悬挂线程池：置回 None 让实例回到"未打开"这个干净状态，
            # 也让 close() 对它是安全的。
            self._executor = None
            executor.shutdown(wait=False)
            raise

    async def close(self) -> None:
        """关连接、关库、收执行器。

        幂等，且**对从未打开过的实例安全**——回滚时会这么调：`start()` 半途失败
        后 `stop()` 仍会被作用到这个实例上。三样资源都先取出置 None 再关，
        免得关闭动作抛错时留下半关状态。
        """
        conn, self._conn = self._conn, None
        db, self._db = self._db, None
        executor, self._executor = self._executor, None
        if executor is None:
            return
        # 关闭动作本身也要落在那个线程上（Kùzu 的句柄一样不跨线程），因此用捕获的
        # executor 直接下沉，不能走 _in_thread——那条路读 self._executor，此刻已是 None。
        if conn is not None:
            await _run_in(executor, conn.close)
        if db is not None:
            await _run_in(executor, db.close)
        executor.shutdown(wait=False)

    async def run(self, query: str) -> list[list[object]]:
        """执行一条语句并取回全部行。

        **不接受外部参数**：本层所有语句都是模块内常量加已转义的字面量拼接，
        没有参数化查询的需求，多留一个没人用的形参就是预留。
        """
        conn = self._require()
        try:
            return await self._in_thread(lambda: _fetch(conn, query))
        except RuntimeError as exc:
            # Kùzu 的报错一律是裸 RuntimeError，不包的话调用方按错误类型分支时
            # 只能猜；原始异常挂 __cause__ 供排查。
            raise MemoryStoreError(f"Kùzu 语句执行失败：{exc}", operation="run") from exc

    def _require(self) -> kuzu.Connection:
        """取连接；未打开时抛存储错误而不是裸 AttributeError。"""
        if self._conn is None:
            raise MemoryStoreError("会话尚未打开", operation="run")
        return self._conn

    async def _in_thread[V](self, work: Callable[[], V]) -> V:
        """把同步调用沉进单线程执行器（线程亲和，见模块 docstring）。"""
        executor = self._executor
        if executor is None:
            raise MemoryStoreError("会话尚未打开，执行器不可用", operation="run")
        return await _run_in(executor, work)

    def _open_sync(self) -> tuple[kuzu.Database, kuzu.Connection]:
        """开库开连接并装载扩展（同步，在执行器线程里跑一次）。"""
        db = kuzu.Database(self._db_path / _DB_FILE)
        conn = kuzu.Connection(db)
        for install, load in _EXTENSIONS:
            _ = conn.execute(install)
            _ = conn.execute(load)
        return db, conn
