"""容器装配与依赖注入测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar, cast, override

import pytest

from core.config import AppSettings, Settings
from core.service.base import Service
from core.service.clock_service import ClockService
from core.service.item_service import ItemService
from core.service.manager import (
    ServiceContractError,
    ServiceManager,
    ServiceNotRegisteredError,
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
        self.settings = settings


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
        self.db = db


class UnknownAnnotation(Service):
    """注解类型容器解释不了。"""

    name: ClassVar[str] = "unknown-annotation"

    def __init__(self, value: int) -> None:
        super().__init__()
        self.value = value


class VarArgs(Service):
    """可变参数无法注入。"""

    name: ClassVar[str] = "varargs"

    def __init__(self, *args: object) -> None:
        super().__init__()
        self.args = args


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


class MissingAnnotationService(Service):
    """构造器参数缺少类型注解的服务。"""

    name: ClassVar[str] = "no-annotation"

    def __init__(self, dependency) -> None:  # pyright: ignore[reportUnknownParameterType]
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

    assert mgr._instances == {}


class FakeClock(ClockService):
    """固定时间的时钟替身，证明时间来源可替换。"""

    def __init__(self, fixed: datetime) -> None:
        super().__init__()
        self._fixed = fixed

    @override
    def now(self) -> datetime:
        return self._fixed


async def test_时间来源可替换() -> None:
    """绕过容器直接构造，created_at 精确等于假时钟时间。"""
    fixed = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    service = ItemService(FakeClock(fixed))

    item = await service.create_item("手写")

    assert item.created_at == fixed


async def test_容器把注册的时钟注入给条目服务() -> None:
    mgr = build_manager()

    assert mgr.get(ItemService)._clock is mgr.get(ClockService)


async def test_容器装配后条目服务可用() -> None:
    mgr = build_manager()
    await mgr.start_all()

    item = await mgr.get(ItemService).create_item("走容器")

    assert item.id == 2
    assert mgr.get(ItemService).running is True
