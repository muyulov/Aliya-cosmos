"""日志上下文测试。"""

from __future__ import annotations

import asyncio

from core.logger import context


def test_未绑定时_request_id_返回占位符() -> None:
    assert context.request_id() == "-"


def test_current_未绑定时返回空字典() -> None:
    assert context.current() == {}


def test_bind_后可读出() -> None:
    token = context.bind(request_id="abc", 会话="s1")
    try:
        assert context.request_id() == "abc"
        assert context.current()["会话"] == "s1"
    finally:
        context.reset(token)
    assert context.request_id() == "-"


def test_嵌套_bind_按逆序还原() -> None:
    outer = context.bind(request_id="outer")
    inner = context.bind(request_id="inner")
    assert context.request_id() == "inner"
    context.reset(inner)
    assert context.request_id() == "outer"
    context.reset(outer)
    assert context.request_id() == "-"


def test_new_request_id_为_16_位十六进制() -> None:
    rid = context.new_request_id()
    assert len(rid) == 16
    assert all(c in "0123456789abcdef" for c in rid)
    assert context.new_request_id() != rid


def test_request_scope_自动生成并还原() -> None:
    with context.request_scope() as rid:
        assert rid
        assert context.request_id() == rid
    assert context.request_id() == "-"


def test_request_scope_沿用传入的_id() -> None:
    with context.request_scope("fixed-id") as rid:
        assert rid == "fixed-id"
        assert context.request_id() == "fixed-id"


async def test_并发任务互不污染() -> None:
    """两个协程各自绑定，互不串号。"""

    async def worker(tag: str, gate: asyncio.Event) -> str:
        with context.request_scope(tag):
            await gate.wait()
            return context.request_id()

    gate = asyncio.Event()
    task_a = asyncio.create_task(worker("aaa", gate))
    task_b = asyncio.create_task(worker("bbb", gate))
    await asyncio.sleep(0)
    gate.set()
    assert await task_a == "aaa"
    assert await task_b == "bbb"
