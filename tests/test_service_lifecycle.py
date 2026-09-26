"""服务生命周期测试：分层并发与启停超时。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import ClassVar, override

import pytest
from loguru import logger
from pydantic import ValidationError

from core.config import LogSettings, ServiceSettings, Settings
from core.logger import setup_logging
from core.service.base import UNSET, Service, ServiceState, Unset
from core.service.manager import HookTimeoutError, ServiceManager, ServiceStartError

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

    assert excinfo.value.label == "fail-a"


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

    assert excinfo.value.label == "slow-start"
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

    assert excinfo.value.label == "half-started"
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


class BusinessTimeoutOnStart(Service):
    """start 里抛业务自己的 TimeoutError（socket 超时这类）。"""

    name: ClassVar[str] = "business-timeout"

    @override
    async def start(self) -> None:
        msg = "业务侧 socket 超时"
        raise TimeoutError(msg)


class BusinessTimeoutOnStop(Service):
    """stop 里抛业务自己的 TimeoutError。"""

    name: ClassVar[str] = "business-timeout-stop"

    @override
    async def stop(self) -> None:
        msg = "业务侧关闭超时"
        raise TimeoutError(msg)


class RerunInner(Service):
    """二轮场景：首轮已成功启动的依赖。"""

    name: ClassVar[str] = "rerun-inner"

    @override
    async def stop(self) -> None:
        order_log.append("stop:rerun-inner")


class RerunOuter(Service):
    """二轮场景：可切换为「第二次启动失败」。"""

    name: ClassVar[str] = "rerun-outer"
    dependencies: ClassVar[tuple[type[Service], ...]] = (RerunInner,)
    #: 置 True 后 start 抛错，用于构造「依赖已 RUNNING、自己启动失败」
    fail_on_second: ClassVar[bool] = False

    def __init__(self, inner: RerunInner) -> None:
        super().__init__()
        self._inner: RerunInner = inner

    @override
    async def start(self) -> None:
        if type(self).fail_on_second:
            msg = "二轮启动失败"
            raise RuntimeError(msg)


async def test_业务_timeout_不被当成启动超时() -> None:
    """回归：业务抛的 TimeoutError 与框架超时同型，不能被伪装成「启动超时」。"""
    mgr = ServiceManager()
    _ = mgr.register(BusinessTimeoutOnStart)

    with pytest.raises(ServiceStartError) as excinfo:
        await mgr.start_all()

    assert type(excinfo.value.cause) is TimeoutError
    assert str(excinfo.value.cause) == "业务侧 socket 超时"


async def test_业务_timeout_不被当成关闭超时(tmp_path: Path) -> None:
    """回归：关闭路径同理，必须是「关闭异常」而不是「关闭超时」。"""
    cfg = _log_cfg(tmp_path)
    setup_logging(cfg)

    mgr = ServiceManager()
    _ = mgr.register(BusinessTimeoutOnStop)

    await mgr.start_all()
    await mgr.stop_all()
    logger.remove()

    text = (Path(cfg.dir) / cfg.file_name).read_text(encoding="utf-8")
    assert "[business-timeout-stop] 服务关闭异常" in text
    assert "服务关闭超时" not in text


async def test_框架超时用独立的异常类型标记() -> None:
    """框架侧超时抛 HookTimeoutError，与业务 TimeoutError 从类型上分开。"""
    mgr = ServiceManager(_settings(start_timeout=0.05))
    _ = mgr.register(SlowStartService)

    with pytest.raises(ServiceStartError) as excinfo:
        await mgr.start_all()

    assert type(excinfo.value.cause) is HookTimeoutError
    assert "启动超时" in str(excinfo.value.cause)


async def test_已运行的依赖在启动失败时也会被回滚() -> None:
    """回归：被跳过的已 RUNNING 依赖同样要进回滚名单。

    二轮 start_all 里依赖已 RUNNING 会被跳过；若不登记，整体失败后它会留在
    RUNNING——而 lifespan.__aenter__ 抛错时 __aexit__ 不执行、stop_all 不会被调用。
    """
    mgr = ServiceManager()
    _ = mgr.register(RerunInner)
    _ = mgr.register(RerunOuter)
    await mgr.start_all()

    # 白盒让外层需要重新启动（依赖保持 RUNNING）
    mgr.get(RerunOuter).state = ServiceState.STOPPED
    RerunOuter.fail_on_second = True
    try:
        with pytest.raises(ServiceStartError):
            await mgr.start_all()
    finally:
        RerunOuter.fail_on_second = False

    assert "stop:rerun-inner" in order_log
    assert mgr.get(RerunInner).state is ServiceState.STOPPED


def test_超时配置必须为正数() -> None:
    """回归：0 / 负数会被 asyncio.timeout 变成「立即超时」，须在校验期拦下。"""
    with pytest.raises(ValidationError):
        _ = ServiceSettings(start_timeout=-1.0)

    with pytest.raises(ValidationError):
        _ = ServiceSettings(stop_timeout=0.0)


async def test_启停日志走服务门面带前缀与耗时(tmp_path: Path) -> None:
    """单服务日志与失败日志同形：`[label]` 前缀 + `服务=` 字段 + 数值耗时。

    耗时字段是排查「同层里是谁慢」的唯一依据——整层耗时只能定位到批次。
    """
    cfg = _log_cfg(tmp_path)
    setup_logging(cfg)

    mgr = ServiceManager()
    _ = mgr.register(QuickService)
    await mgr.start_all()
    await mgr.stop_all()
    logger.remove()

    text = (Path(cfg.dir) / cfg.file_name).read_text(encoding="utf-8")
    assert "[quick] 服务已启动" in text
    assert "[quick] 服务已停止" in text
    assert "耗时毫秒: " in text
    assert "全部服务启动完成" in text
    assert "全部服务已停止" in text
    assert "服务数: 1" in text


async def test_同层日志列出服务名与一基层号(tmp_path: Path) -> None:
    """回归：只报「服务数」看不出这一层是谁，层号也给成人读的 1-based。"""
    cfg = _log_cfg(tmp_path)
    setup_logging(cfg)

    mgr = ServiceManager()
    _ = mgr.register(GateA)
    _ = mgr.register(GateB)
    await mgr.start_all()
    logger.remove()

    text = (Path(cfg.dir) / cfg.file_name).read_text(encoding="utf-8")
    assert "同层服务启动完成" in text
    assert "层: 1" in text
    assert "服务: gate-a、gate-b" in text
    assert "耗时毫秒: " in text


async def test_回滚会留痕(tmp_path: Path) -> None:
    """回归：回滚原先完全不留痕，日志里几条「已启动」后凭空冒失败，看不出谁清理的。"""
    cfg = _log_cfg(tmp_path)
    setup_logging(cfg)

    mgr = ServiceManager()
    _ = mgr.register(HalfStartedInner)
    _ = mgr.register(HalfStartedOuter)

    with pytest.raises(ServiceStartError):
        await mgr.start_all()
    logger.remove()

    text = (Path(cfg.dir) / cfg.file_name).read_text(encoding="utf-8")
    assert "启动未完成，回滚本次动过的服务" in text
    assert "回滚时服务已关闭" in text
    assert "[half-inner] 回滚时服务已关闭" in text
    assert "回滚完成" in text


async def test_超时日志不重复打错误类型(tmp_path: Path) -> None:
    """超时的「错误」就是超时本身：消息 + 超时秒数 已经说清，不再拼异常类型。"""
    cfg = _log_cfg(tmp_path)
    setup_logging(cfg)

    mgr = ServiceManager(_settings(start_timeout=0.05))
    _ = mgr.register(SlowStartService)

    with pytest.raises(ServiceStartError):
        await mgr.start_all()
    logger.remove()

    text = (Path(cfg.dir) / cfg.file_name).read_text(encoding="utf-8")
    assert "[slow-start] 服务启动超时" in text
    assert "超时秒数: 0.05" in text
    assert "HookTimeoutError" not in text
