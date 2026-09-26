"""入口的单元测试：信号注册走哪条路 + 健康检查日志的字段取舍。

真正的信号收发交给手工冒烟：`core/main.py` 在 coverage 里被 omit，这里只测
两块纯逻辑，不为它拉起完整的事件循环。
"""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar, cast, override

import pytest
from loguru import logger

import core.main as main_module
from core.config import LogSettings
from core.logger import setup_logging
from core.service.base import HealthStatus, Service, ServiceState
from core.service.manager import ServiceManager


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


class HealthyProbe(Service):
    """健康检查永远通过。"""

    name: ClassVar[str] = "healthy-probe"

    @override
    async def health(self) -> HealthStatus:
        return HealthStatus(name=self.label, healthy=True, state=ServiceState.RUNNING)


class UnhealthyProbe(Service):
    """健康检查永远失败，并给出原因。"""

    name: ClassVar[str] = "unhealthy-probe"

    @override
    async def health(self) -> HealthStatus:
        return HealthStatus(
            name=self.label, healthy=False, state=ServiceState.FAILED, detail="探测失败"
        )


async def test_健康检查日志只在异常时带状态与详情(tmp_path: Path) -> None:
    """回归：健康时 `状态` 恒为 running、`详情` 恒为空，无条件带上只有冗余。"""
    cfg = LogSettings(level="DEBUG", dir=str(tmp_path / "logs"), retention="1 day")
    setup_logging(cfg)

    mgr = ServiceManager()
    _ = mgr.register(HealthyProbe)
    _ = mgr.register(UnhealthyProbe)
    # 白盒：直接调入口里的健康检查日志函数（本用例就是为它写的）
    await main_module._report_health(mgr)  # pyright: ignore[reportPrivateUsage]
    logger.remove()

    text = (Path(cfg.dir) / cfg.file_name).read_text(encoding="utf-8")
    _, rest = text.split("服务: healthy-probe", 1)
    healthy_block, unhealthy_block = rest.split("服务: unhealthy-probe", 1)

    assert "健康: true" in healthy_block
    assert "状态: " not in healthy_block
    assert "详情" not in healthy_block
    assert "健康: false" in unhealthy_block
    assert "状态: failed" in unhealthy_block
    assert "详情: 探测失败" in unhealthy_block
