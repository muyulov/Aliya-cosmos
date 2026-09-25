"""服务生命周期测试：分层并发与启停超时。"""

from __future__ import annotations

import asyncio
from typing import ClassVar, override

import pytest

from core.service.base import Service
from core.service.manager import ServiceManager, ServiceStartError

#: 同层并发用的事件表：键是服务 label
entered: dict[str, asyncio.Event] = {}
#: 跨层串行用的进入/退出记录
order_log: list[str] = []


@pytest.fixture(autouse=True)
def _reset_probe_state() -> None:
    """每个用例重置探针状态：事件按需新建，记录清空。"""
    entered.clear()
    order_log.clear()


class GateA(Service):
    """同层探针：置位自己后等对端。"""

    name: ClassVar[str] = "gate-a"

    @override
    async def start(self) -> None:
        entered.setdefault("gate-a", asyncio.Event()).set()
        _ = await asyncio.wait_for(
            entered.setdefault("gate-b", asyncio.Event()).wait(), timeout=1.0
        )


class GateB(Service):
    """同层探针：置位自己后等对端。"""

    name: ClassVar[str] = "gate-b"

    @override
    async def start(self) -> None:
        entered.setdefault("gate-b", asyncio.Event()).set()
        _ = await asyncio.wait_for(
            entered.setdefault("gate-a", asyncio.Event()).wait(), timeout=1.0
        )


class InnerService(Service):
    """被依赖的一方。"""

    name: ClassVar[str] = "inner"

    @override
    async def start(self) -> None:
        order_log.append("enter:inner")
        order_log.append("exit:inner")


class OuterService(Service):
    """依赖 InnerService 的一方。"""

    name: ClassVar[str] = "outer"
    dependencies: ClassVar[tuple[type[Service], ...]] = (InnerService,)

    def __init__(self, inner: InnerService) -> None:
        super().__init__()
        self._inner: InnerService = inner

    @override
    async def start(self) -> None:
        order_log.append("enter:outer")
        order_log.append("exit:outer")


class FailingGateA(Service):
    """同层失败探针（先注册）。"""

    name: ClassVar[str] = "fail-a"

    @override
    async def start(self) -> None:
        entered.setdefault("fail-a", asyncio.Event()).set()
        _ = await asyncio.wait_for(
            entered.setdefault("fail-b", asyncio.Event()).wait(), timeout=1.0
        )
        msg = "甲炸了"
        raise RuntimeError(msg)


class FailingGateB(Service):
    """同层失败探针（后注册）。"""

    name: ClassVar[str] = "fail-b"

    @override
    async def start(self) -> None:
        entered.setdefault("fail-b", asyncio.Event()).set()
        _ = await asyncio.wait_for(
            entered.setdefault("fail-a", asyncio.Event()).wait(), timeout=1.0
        )
        msg = "乙炸了"
        raise RuntimeError(msg)


async def test_同层服务并发启动() -> None:
    """两个无依赖服务互等对方进入 start：串行则必然超时失败。"""
    mgr = ServiceManager()
    _ = mgr.register(GateA)
    _ = mgr.register(GateB)

    await mgr.start_all()

    assert mgr.get(GateA).running is True
    assert mgr.get(GateB).running is True


async def test_跨层严格串行() -> None:
    """依赖必须先跑完，因此不同层不会并发。"""
    mgr = ServiceManager()
    _ = mgr.register(InnerService)
    _ = mgr.register(OuterService)

    await mgr.start_all()

    assert order_log == ["enter:inner", "exit:inner", "enter:outer", "exit:outer"]


async def test_同层某个服务失败时回滚且报错取先注册者() -> None:
    """同层两个都失败：报错取注册顺序最靠前的那个，保证多次运行结果一致。"""
    mgr = ServiceManager()
    _ = mgr.register(FailingGateA)
    _ = mgr.register(FailingGateB)

    with pytest.raises(ServiceStartError) as excinfo:
        await mgr.start_all()

    assert excinfo.value.service_name == "fail-a"
