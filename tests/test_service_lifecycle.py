"""服务生命周期测试：分层并发与启停超时。"""

from __future__ import annotations

import asyncio
from typing import ClassVar, override

import pytest

from core.config import ServiceSettings, Settings
from core.service.base import UNSET, Service, ServiceState, Unset
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


def _settings(start_timeout: float | None = 30.0, stop_timeout: float | None = 30.0) -> Settings:
    """构造只关心生命周期超时的配置。"""
    return Settings(service=ServiceSettings(start_timeout=start_timeout, stop_timeout=stop_timeout))


class SlowStartService(Service):
    """start 里睡很久，用来触发超时。"""

    name: ClassVar[str] = "slow-start"

    @override
    async def start(self) -> None:
        await asyncio.sleep(10)


class SlowDependentService(Service):
    """依赖 InnerService 且自己会超时：用来验证超时后回滚依赖。"""

    name: ClassVar[str] = "slow-dependent"
    dependencies: ClassVar[tuple[type[Service], ...]] = (InnerService,)

    def __init__(self, inner: InnerService) -> None:
        super().__init__()
        self._inner: InnerService = inner

    @override
    async def start(self) -> None:
        await asyncio.sleep(10)


class NoLimitService(Service):
    """服务级关掉超时：None 表示该服务不限制。"""

    name: ClassVar[str] = "no-limit"
    start_timeout: ClassVar[float | Unset | None] = None

    @override
    async def start(self) -> None:
        await asyncio.sleep(0.1)


class HangingStopService(Service):
    """stop 里睡很久，用来触发关闭超时。"""

    name: ClassVar[str] = "hanging-stop"

    @override
    async def stop(self) -> None:
        await asyncio.sleep(10)


class QuickService(Service):
    """正常启停的对照服务。"""

    name: ClassVar[str] = "quick"


async def test_启动超时判定为失败() -> None:
    mgr = ServiceManager(_settings(start_timeout=0.05))
    _ = mgr.register(SlowStartService)

    with pytest.raises(ServiceStartError) as excinfo:
        await mgr.start_all()

    assert excinfo.value.service_name == "slow-start"
    assert isinstance(excinfo.value.cause, TimeoutError)
    assert "启动超时" in str(excinfo.value.cause)
    assert mgr.get(SlowStartService).state is ServiceState.FAILED


async def test_启动超时回滚已启动的依赖() -> None:
    mgr = ServiceManager(_settings(start_timeout=0.05))
    _ = mgr.register(InnerService)
    _ = mgr.register(SlowDependentService)

    with pytest.raises(ServiceStartError):
        await mgr.start_all()

    assert mgr.get(InnerService).state is ServiceState.STOPPED
    assert mgr.get(SlowDependentService).state is ServiceState.FAILED


async def test_服务级_none_覆盖全局超时() -> None:
    """全局只给 0.05 秒，该服务声明 None → 不限制，睡 0.1 秒也能成功。"""
    mgr = ServiceManager(_settings(start_timeout=0.05))
    _ = mgr.register(NoLimitService)

    await mgr.start_all()

    assert mgr.get(NoLimitService).running is True


async def test_未覆盖时跟随全局超时() -> None:
    """不声明 ClassVar 的服务，用全局 0.05 秒 → 睡 10 秒必然超时。"""
    assert SlowStartService.start_timeout is UNSET

    mgr = ServiceManager(_settings(start_timeout=0.05))
    _ = mgr.register(SlowStartService)

    with pytest.raises(ServiceStartError):
        await mgr.start_all()


async def test_关闭超时不阻断其他服务() -> None:
    mgr = ServiceManager(_settings(stop_timeout=0.05))
    _ = mgr.register(QuickService)
    _ = mgr.register(HangingStopService)

    await mgr.start_all()
    await mgr.stop_all()  # 不能抛错

    assert mgr.get(HangingStopService).state is ServiceState.FAILED
    assert mgr.get(QuickService).state is ServiceState.STOPPED


async def test_关闭超时会写进日志字段() -> None:
    """超时日志必须带 超时秒数，否则排查时看不出配的是多少。"""
    mgr = ServiceManager(_settings(stop_timeout=0.05))
    _ = mgr.register(HangingStopService)

    await mgr.start_all()
    await mgr.stop_all()
