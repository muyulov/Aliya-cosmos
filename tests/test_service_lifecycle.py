"""服务生命周期测试：分层并发与启停超时。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import ClassVar, override

import pytest
from loguru import logger

from core.config import LogSettings, ServiceSettings, Settings
from core.logger import setup_logging
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


@pytest.fixture(autouse=True)
def _restore_logger():
    """用例结束后清掉 loguru sink，避免文件句柄与日志内容泄漏到其他测试。"""
    yield
    _ = logger.remove()


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


def _log_cfg(tmp_path: Path) -> LogSettings:
    """构造只关心落盘位置的日志配置。"""
    return LogSettings(level="DEBUG", dir=str(tmp_path / "logs"), retention="1 day")


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


class CancelProbeQuick(Service):
    """取消场景：第 0 层先成功启动，用来验证取消后它仍被回滚。"""

    name: ClassVar[str] = "cancel-quick"

    @override
    async def stop(self) -> None:
        order_log.append("stop:cancel-quick")


class CancelProbeHanging(Service):
    """取消场景：第 1 层挂在 start 里，让 gather 被取消时仍处于等待。"""

    name: ClassVar[str] = "cancel-hanging"
    dependencies: ClassVar[tuple[type[Service], ...]] = (CancelProbeQuick,)

    def __init__(self, quick: CancelProbeQuick) -> None:
        super().__init__()
        self._quick: CancelProbeQuick = quick

    @override
    async def start(self) -> None:
        entered.setdefault("cancel-hanging", asyncio.Event()).set()
        _ = await asyncio.Event().wait()


class HalfStartedService(Service):
    """start 里先占资源再抛错：验证失败者也会被 stop() 回收半途资源。"""

    name: ClassVar[str] = "half-started"

    @override
    async def start(self) -> None:
        order_log.append("open:half-started")
        msg = "建到一半炸了"
        raise RuntimeError(msg)

    @override
    async def stop(self) -> None:
        order_log.append("close:half-started")


class HalfStartedInner(Service):
    """混合场景：第 0 层成功启动。"""

    name: ClassVar[str] = "half-inner"

    @override
    async def stop(self) -> None:
        order_log.append("stop:half-inner")


class HalfStartedOuter(Service):
    """混合场景：第 1 层启动失败。"""

    name: ClassVar[str] = "half-outer"
    dependencies: ClassVar[tuple[type[Service], ...]] = (HalfStartedInner,)

    def __init__(self, inner: HalfStartedInner) -> None:
        super().__init__()
        self._inner: HalfStartedInner = inner

    @override
    async def start(self) -> None:
        msg = "外层炸了"
        raise RuntimeError(msg)

    @override
    async def stop(self) -> None:
        order_log.append("stop:half-outer")


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


async def test_关闭超时会写进日志字段(tmp_path: Path) -> None:
    """超时日志必须带 超时秒数，否则排查时看不出配的是多少。"""
    cfg = _log_cfg(tmp_path)
    setup_logging(cfg)

    mgr = ServiceManager(_settings(stop_timeout=0.05))
    _ = mgr.register(HangingStopService)

    await mgr.start_all()
    await mgr.stop_all()
    logger.remove()

    text = (Path(cfg.dir) / cfg.file_name).read_text(encoding="utf-8")
    assert "[hanging-stop] 服务关闭超时" in text
    assert "超时秒数: 0.05" in text


async def test_启动被取消时回滚已启动的服务() -> None:
    """回归：gather 自身被取消时拿不到 outcomes，已启动的服务仍须被回滚。

    `lifespan()` 的 `__aenter__` 抛错时 `__aexit__` 不会执行、`stop_all` 也不会
    被调用，因此回滚只能在 `start_all` 内部完成。第 0 层先成功，第 1 层挂住后
    取消任务：此时 `started` 只能靠 `_start_one` 在协程内部登记才拿得到。
    """
    mgr = ServiceManager()
    _ = mgr.register(CancelProbeQuick)
    _ = mgr.register(CancelProbeHanging)

    task = asyncio.create_task(mgr.start_all())
    # 第 1 层的 start 被调用，说明第 0 层 gather 已返回、登记已完成（层间串行）
    _ = await entered.setdefault("cancel-hanging", asyncio.Event()).wait()
    _ = task.cancel()

    with pytest.raises(asyncio.CancelledError):
        _ = await task

    assert "stop:cancel-quick" in order_log
    assert mgr.get(CancelProbeQuick).state is ServiceState.STOPPED
    assert mgr.get(CancelProbeHanging).state is ServiceState.FAILED


async def test_启动失败的服务自身也会被回滚清理() -> None:
    """回归：失败者不在「已启动」名单里，但它同样需要 stop() 回收半途资源。"""
    mgr = ServiceManager()
    _ = mgr.register(HalfStartedService)

    with pytest.raises(ServiceStartError) as excinfo:
        await mgr.start_all()

    assert excinfo.value.service_name == "half-started"
    assert order_log == ["open:half-started", "close:half-started"]
    # 已被清理，但启动失败是它的终态标记，不该被抹成 STOPPED
    assert mgr.get(HalfStartedService).state is ServiceState.FAILED


async def test_回滚同时覆盖成功者与失败者() -> None:
    """回滚名单含失败者，且按「动过」的顺序逆序清理。"""
    mgr = ServiceManager()
    _ = mgr.register(HalfStartedInner)
    _ = mgr.register(HalfStartedOuter)

    with pytest.raises(ServiceStartError):
        await mgr.start_all()

    assert order_log == ["stop:half-outer", "stop:half-inner"]
    assert mgr.get(HalfStartedInner).state is ServiceState.STOPPED
    assert mgr.get(HalfStartedOuter).state is ServiceState.FAILED
