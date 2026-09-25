"""容器装配与依赖注入测试。"""

from __future__ import annotations

from datetime import timedelta
from typing import ClassVar, cast

import pytest
from pydantic import BaseModel

import core.service.manager as manager_module
from core.config import AppSettings, ClockSettings, Settings, get_settings
from core.service.base import Service, ServiceState
from core.service.clock_service import ClockService
from core.service.manager import (
    ServiceContractError,
    ServiceManager,
    ServiceNotRegisteredError,
    ServiceStartError,
)
from core.service.registry import build_manager

built: list[str] = []


class DbService(Service):
    name: ClassVar[str] = "db"


class SettingsAwareService(Service):
    """声明 Settings 依赖的服务：容器内置注入。"""

    name: ClassVar[str] = "settings-aware"

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings: Settings = settings


class LazyProbeService(Service):
    """构造时登记，用于验证装配置的惰性。"""

    name: ClassVar[str] = "lazy-probe"

    def __init__(self) -> None:
        super().__init__()
        built.append(self.label)


class NotAService:
    """故意不是 Service 子类。"""


class DeclaredButMissingParam(Service):
    """声明了依赖，构造器却没有对应参数。"""

    name: ClassVar[str] = "declared-missing"
    dependencies: ClassVar[tuple[type[Service], ...]] = (DbService,)


class UndeclaredParam(Service):
    """构造器有依赖参数，却没有声明。"""

    name: ClassVar[str] = "undeclared"

    def __init__(self, db: DbService) -> None:
        super().__init__()
        self.db: DbService = db


class UnknownAnnotation(Service):
    """注解类型容器解释不了。"""

    name: ClassVar[str] = "unknown-annotation"

    def __init__(self, value: int) -> None:
        super().__init__()
        self.value: int = value


class VarArgs(Service):
    """可变参数无法注入。"""

    name: ClassVar[str] = "varargs"

    def __init__(self, *args: object) -> None:
        super().__init__()
        self.args: tuple[object, ...] = args


@pytest.fixture(autouse=True)
def _clear_built() -> None:
    built.clear()


def test_装配是惰性的() -> None:
    """register 之后、首次访问之前不构造任何实例。"""
    mgr = ServiceManager()
    _ = mgr.register(LazyProbeService)
    assert built == []

    _ = mgr.get(LazyProbeService)
    assert built == ["lazy-probe"]


def test_配置注入容器持有的实例() -> None:
    settings = Settings(app=AppSettings(app_name="注入校验"))
    mgr = ServiceManager(settings)
    _ = mgr.register(SettingsAwareService)

    assert mgr.get(SettingsAwareService).settings is settings


def test_查找未注册类型报错() -> None:
    mgr = ServiceManager()
    _ = mgr.register(DbService)

    with pytest.raises(ServiceNotRegisteredError):
        _ = mgr.get(SettingsAwareService)


def test_注册非_Service_子类报错() -> None:
    mgr = ServiceManager()

    with pytest.raises(ServiceContractError):
        _ = mgr.register(cast("type[Service]", NotAService))


def test_重复注册同一类型报错() -> None:
    mgr = ServiceManager()
    _ = mgr.register(DbService)

    with pytest.raises(ServiceContractError, match="重复注册"):
        _ = mgr.register(DbService)


def test_装配后不能再注册() -> None:
    mgr = ServiceManager()
    _ = mgr.register(DbService)
    _ = mgr.services  # 触发装配

    with pytest.raises(ServiceContractError, match="不能再注册"):
        _ = mgr.register(LazyProbeService)


def test_声明了依赖但签名没有对应参数() -> None:
    mgr = ServiceManager()
    _ = mgr.register(DbService)
    _ = mgr.register(DeclaredButMissingParam)

    with pytest.raises(ServiceContractError, match="构造器没有对应参数"):
        _ = mgr.services


def test_签名有依赖参数但未声明() -> None:
    mgr = ServiceManager()
    _ = mgr.register(DbService)
    _ = mgr.register(UndeclaredParam)

    with pytest.raises(ServiceContractError, match="未在 dependencies 中声明"):
        _ = mgr.services


def test_注解类型无法注入() -> None:
    mgr = ServiceManager()
    _ = mgr.register(UnknownAnnotation)

    with pytest.raises(ServiceContractError, match="容器只支持"):
        _ = mgr.services


def test_可变参数无法注入() -> None:
    mgr = ServiceManager()
    _ = mgr.register(VarArgs)

    with pytest.raises(ServiceContractError, match="可变参数"):
        _ = mgr.services


class DuplicateDependencyService(Service):
    """dependencies 里重复写了同一个类型。"""

    name: ClassVar[str] = "duplicate-dependency"
    dependencies: ClassVar[tuple[type[Service], ...]] = (DbService, DbService)

    def __init__(self, db: DbService) -> None:
        super().__init__()
        self.db: DbService = db


def test_dependencies_重复类型报错() -> None:
    """回归：旧实现用集合对账，重复类型会被静默吃掉。"""
    mgr = ServiceManager()
    _ = mgr.register(DbService)
    _ = mgr.register(DuplicateDependencyService)

    with pytest.raises(ServiceContractError, match="重复类型"):
        _ = mgr.services


class MissingAnnotationService(Service):
    """构造器参数缺少类型注解的服务。"""

    name: ClassVar[str] = "no-annotation"

    def __init__(
        self,
        dependency,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    ) -> None:
        """故意的无注解参数，用于覆盖契约校验的最后一个分支。"""
        super().__init__()


def test_参数缺少类型注解报错() -> None:
    mgr = ServiceManager()
    _ = mgr.register(MissingAnnotationService)

    with pytest.raises(ServiceContractError, match="缺少类型注解"):
        _ = mgr.services


class BrokenDependentService(Service):
    """构造器抛错的服务，用于验证装配失败不留下半成品。"""

    name: ClassVar[str] = "broken-dependent"
    dependencies: ClassVar[tuple[type[Service], ...]] = (DbService,)

    def __init__(self, db: DbService) -> None:
        super().__init__()
        msg = "构造失败"
        raise RuntimeError(msg)


def test_装配失败不留下半成品实例() -> None:
    """构造期失败时容器保持未装配，不会留下已构造的前序服务。"""
    mgr = ServiceManager()
    _ = mgr.register(DbService)
    _ = mgr.register(BrokenDependentService)

    with pytest.raises(RuntimeError, match="构造失败"):
        _ = mgr.services

    # 白盒断言：没有公开 API 能观察"是否留下半成品"，只能读实例表
    assert mgr._instances == {}  # pyright: ignore[reportPrivateUsage]


class ClockConsumerService(Service):
    """消费 ClockService 的服务，用于验证容器按类型注入同一实例。"""

    name: ClassVar[str] = "clock-consumer"
    dependencies: ClassVar[tuple[type[Service], ...]] = (ClockService,)

    def __init__(self, clock: ClockService) -> None:
        super().__init__()
        self.clock: ClockService = clock


def test_容器按类型注入依赖实例() -> None:
    """注入的是容器里那个实例，而不是新建副本。"""
    mgr = ServiceManager()
    _ = mgr.register(ClockService)
    _ = mgr.register(ClockConsumerService)

    assert mgr.get(ClockConsumerService).clock is mgr.get(ClockService)


async def test_默认注册表装配后可启动并健康() -> None:
    mgr = build_manager()
    await mgr.start_all()

    assert mgr.get(ClockService).running is True
    statuses = await mgr.health()
    assert [status.name for status in statuses] == ["clock"]


async def test_健康检查用展示名() -> None:
    """展示名只有一个出口：重写 health() 时也必须走 label。"""
    service = ClockConsumerService(ClockService(ClockSettings()))

    health = await service.health()

    assert health.name == service.label


def test_配置注入的是容器持有的实例而非全局单例() -> None:
    """防止装配退化成读 get_settings() 全局单例，丢掉调用方传入的配置。"""
    own = Settings(app=AppSettings(app_name="容器持有"))
    mgr = ServiceManager(own)
    _ = mgr.register(SettingsAwareService)

    injected = mgr.get(SettingsAwareService).settings

    assert injected is own
    assert injected is not get_settings()
    assert injected.app.app_name == "容器持有"


def test_未传配置时回退到全局单例(monkeypatch: pytest.MonkeyPatch) -> None:
    """容器未持有配置时走全局单例。

    判定必须写 `is not None` 而不是 `or`：配置模型一旦定义了 __bool__ /
    __len__，空配置会被判为假而静默回退到这里，本用例守住这条回退路径。
    """
    own = Settings(app=AppSettings(app_name="全局单例"))
    monkeypatch.setattr(manager_module, "get_settings", lambda: own)

    mgr = ServiceManager()
    _ = mgr.register(SettingsAwareService)

    assert mgr.get(SettingsAwareService).settings is own


class FalsySettings(Settings):
    """故意判定为假的配置：用真值判断就会被误当成「未传配置」。"""

    def __bool__(self) -> bool:
        return False


def test_假值配置不会被当成未传() -> None:
    """回归：容器判定「是否持有配置」必须用 `is not None`。

    改回 `settings or get_settings()` 时本用例会红——假值配置会被丢弃，
    注入的变成全局单例（或默认值），而不是容器持有的那份。
    """
    own = FalsySettings(app=AppSettings(app_name="假值配置"))
    mgr = ServiceManager(own)
    _ = mgr.register(SettingsAwareService)

    assert mgr.get(SettingsAwareService).settings is own


class NodeConsumerService(Service):
    """声明配置节点依赖的服务。"""

    name: ClassVar[str] = "node-consumer"

    def __init__(self, config: ClockSettings) -> None:
        super().__init__()
        self.config: ClockSettings = config


class UnregisteredNode(BaseModel):
    """故意不挂到 Settings 上的配置模型。"""


class UnknownNodeService(Service):
    """注解了一个没注册到 Settings 的配置节点。"""

    name: ClassVar[str] = "unknown-node"

    def __init__(self, config: UnregisteredNode) -> None:
        super().__init__()
        self.config: UnregisteredNode = config


class DuplicateNodeSettings(Settings):
    """两个字段同类型：容器无法确定注入哪个。"""

    extra_clock: ClockSettings = ClockSettings()


def test_配置节点按类型注入() -> None:
    own = Settings(clock=ClockSettings(tz="Asia/Shanghai"))
    mgr = ServiceManager(own)
    _ = mgr.register(NodeConsumerService)

    assert mgr.get(NodeConsumerService).config is own.clock


def test_配置节点未注册到_settings_时报错() -> None:
    mgr = ServiceManager()
    _ = mgr.register(UnknownNodeService)

    with pytest.raises(ServiceContractError, match="顶层节点索引"):
        _ = mgr.services


def test_配置节点类型重复时报错() -> None:
    mgr = ServiceManager(DuplicateNodeSettings(extra_clock=ClockSettings()))
    _ = mgr.register(NodeConsumerService)

    with pytest.raises(ServiceContractError, match="出现多次"):
        _ = mgr.services


class CacheSettings(BaseModel):
    """可选节点探针用的配置模型。"""

    size: int = 100


class CacheConsumerService(Service):
    """声明可选配置节点依赖的服务。"""

    name: ClassVar[str] = "cache-consumer"

    def __init__(self, config: CacheSettings) -> None:
        super().__init__()
        self.config: CacheSettings = config


class OptionalNodeSettings(Settings):
    """可选配置节点，当前有值。"""

    cache: CacheSettings | None = CacheSettings(size=42)


class NoneValuedNodeSettings(Settings):
    """可选配置节点，当前值为 None。"""

    cache: CacheSettings | None = None


def test_可选配置节点有值时按类型注入() -> None:
    """注解写 `X | None` 但当前有值时，仍应能按类型注入。"""
    mgr = ServiceManager(OptionalNodeSettings())
    _ = mgr.register(CacheConsumerService)

    assert mgr.get(CacheConsumerService).config.size == 42


def test_可选配置节点为_none_时报错并说明原因() -> None:
    """回归：原来一律报「不是顶层字段」，把「可选且为 None」误导成「没挂上去」。"""
    mgr = ServiceManager(NoneValuedNodeSettings())
    _ = mgr.register(CacheConsumerService)

    with pytest.raises(ServiceContractError, match="为 None"):
        _ = mgr.services


async def test_时钟按配置时区取值() -> None:
    clock = ClockService(ClockSettings(tz="Asia/Shanghai"))

    assert clock.now().utcoffset() == timedelta(hours=8)


async def test_时钟默认时区为_utc() -> None:
    assert ClockService(ClockSettings()).now().utcoffset() == timedelta(0)


async def test_非法时区在启动期失败() -> None:
    """时区解析放 start()：非法值在启动期 fail fast，不回落到 UTC 硬跑。"""
    mgr = ServiceManager(Settings(clock=ClockSettings(tz="Not/AZone")))
    _ = mgr.register(ClockService)

    with pytest.raises(ServiceStartError) as excinfo:
        await mgr.start_all()

    assert excinfo.value.service_name == "clock"
    assert mgr.get(ClockService).state is ServiceState.FAILED


def test_容器把配置节点注入时钟服务() -> None:
    own = Settings(clock=ClockSettings(tz="Asia/Shanghai"))
    mgr = ServiceManager(own)
    _ = mgr.register(ClockService)

    assert mgr.get(ClockService).now().utcoffset() == timedelta(hours=8)
