"""入口的信号注册测试。

只覆盖 `_register_signal` 的「走哪条路」——真正的信号收发交给手工冒烟：
`core/main.py` 在 coverage 里被 omit，也没必要为它拉起事件循环。
"""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable
from typing import cast

import pytest

import core.main as main_module


class FakeLoop:
    """只实现信号注册相关方法的事件循环替身。"""

    def __init__(self, *, supported: bool) -> None:
        self.supported: bool = supported
        self.registered: list[int] = []

    def add_signal_handler(self, sig: signal.Signals, _callback: object) -> None:
        if not self.supported:
            raise NotImplementedError
        self.registered.append(int(sig))

    def call_soon_threadsafe(self, callback: Callable[[], None]) -> None:
        callback()


def _as_loop(fake: FakeLoop) -> asyncio.AbstractEventLoop:
    """把替身收成事件循环类型。

    先经 object 再 cast：FakeLoop 只实现了被调用的那几个方法，与
    AbstractEventLoop 没有名义上的重叠，直接 cast 会被 reportInvalidCast 拦下。
    """
    return cast("asyncio.AbstractEventLoop", cast("object", fake))


def _register(loop: FakeLoop, on_signal: Callable[[], None]) -> bool:
    """调用入口的私有注册函数（白盒：本文件就是为它写的）。"""
    return main_module._register_signal(  # pyright: ignore[reportPrivateUsage]
        _as_loop(loop), signal.SIGINT, on_signal
    )


def test_优先使用事件循环注册信号() -> None:
    loop = FakeLoop(supported=True)
    called: list[str] = []

    used_loop = _register(loop, lambda: called.append("hit"))

    assert used_loop is True
    assert loop.registered == [int(signal.SIGINT)]
    assert called == []


def test_事件循环不支持时回退到_signal_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    """回归：Windows 的 proactor 循环不实现 add_signal_handler。"""
    loop = FakeLoop(supported=False)
    registered: list[tuple[int, object]] = []

    def _fake_signal(sig: int, handler: object) -> None:
        registered.append((sig, handler))

    monkeypatch.setattr(signal, "signal", _fake_signal)
    called: list[str] = []

    used_loop = _register(loop, lambda: called.append("hit"))

    assert used_loop is False
    assert registered[0][0] == int(signal.SIGINT)

    # 处理器必须经由 call_soon_threadsafe 置位——信号处理器不在事件循环线程里跑
    handler = cast("Callable[[int, object], None]", registered[0][1])
    handler(int(signal.SIGINT), None)
    assert called == ["hit"]
