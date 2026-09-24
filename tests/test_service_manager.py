"""ServiceManager 生命周期测试。"""

from __future__ import annotations

from typing import ClassVar, override

import pytest

from core.service.base import Service, ServiceState
from core.service.manager import (
    CircularDependencyError,
    MissingDependencyError,
    ServiceManager,
    ServiceStartError,
)

events: list[str] = []


class RecordService(Service):
    """记录启停顺序的测试服务基类。

    name 与 dependencies 是类级常量，每个场景用一个显式子类表达。
    契约校验要求 dependencies 与构造器签名逐一对上，动态造类要靠 exec
    拼注解，可读性差且易碎，因此这里全部写成显式类。
    """

    name: ClassVar[str] = "recorder"
    dependencies: ClassVar[tuple[type[Service], ...]] = ()

    def __init__(self) -> None:
        super().__init__()
        self.start_calls: int = 0
        self.stop_calls: int = 0

    @override
    async def start(self) -> None:
        self.start_calls += 1
        events.append(f"start:{self.label}")

    @override
    async def stop(self) -> None:
        self.stop_calls += 1
        events.append(f"stop:{self.label}")


class DbService(RecordService):
    name: ClassVar[str] = "db"


class CacheService(RecordService):
    name: ClassVar[str] = "cache"
    dependencies: ClassVar[tuple[type[Service], ...]] = (DbService,)

    def __init__(self, db: DbService) -> None:
        super().__init__()
        self.db: DbService = db


class ApiService(RecordService):
    name: ClassVar[str] = "api"
    dependencies: ClassVar[tuple[type[Service], ...]] = (CacheService, DbService)

    def __init__(self, cache: CacheService, db: DbService) -> None:
        super().__init__()
        self.cache: CacheService = cache
        self.db: DbService = db


class BadStopService(RecordService):
    name: ClassVar[str] = "bad-stop"

    @override
    async def stop(self) -> None:
        msg = "关闭炸了"
        raise RuntimeError(msg)


class FailingDbService(RecordService):
    name: ClassVar[str] = "db"


class FailingApiService(RecordService):
    name: ClassVar[str] = "api"
    dependencies: ClassVar[tuple[type[Service], ...]] = (FailingDbService,)

    def __init__(self, db: FailingDbService) -> None:
        super().__init__()
        self.db: FailingDbService = db

    @override
    async def start(self) -> None:
        # 先走基类逻辑（计数 + 记事件），再模拟启动失败
        await super().start()
        msg = "启动炸了"
        raise RuntimeError(msg)


class CycleA(RecordService):
    """环的一侧：注解延迟解析，因此可以先写字符串注解再补 dependencies。"""

    name: ClassVar[str] = "a"

    def __init__(self, b: CycleB) -> None:
        super().__init__()
        self.b: CycleB = b


class CycleB(RecordService):
    name: ClassVar[str] = "b"

    def __init__(self, a: CycleA) -> None:
        super().__init__()
        self.a: CycleA = a


# 成环声明必须在两个类都定义后补上（注解因 from __future__ import annotations 延迟求值）
CycleA.dependencies = (CycleB,)
CycleB.dependencies = (CycleA,)


class OrphanService(RecordService):
    """依赖的类型没注册。"""

    name: ClassVar[str] = "orphan"
    dependencies: ClassVar[tuple[type[Service], ...]] = (DbService,)

    def __init__(self, db: DbService) -> None:
        super().__init__()
        self.db: DbService = db


@pytest.fixture(autouse=True)
def _clear_events() -> None:
    events.clear()


async def test_按依赖顺序启动() -> None:
    mgr = ServiceManager()
    _ = mgr.register(DbService)
    _ = mgr.register(CacheService)
    _ = mgr.register(ApiService)

    await mgr.start_all()

    assert events.index("start:db") < events.index("start:cache") < events.index("start:api")


async def test_依赖被注入为同一个实例() -> None:
    """容器注入的是注册的那个实例，而不是新建的副本。"""
    mgr = ServiceManager()
    _ = mgr.register(DbService)
    _ = mgr.register(CacheService)
    _ = mgr.register(ApiService)

    db = mgr.get(DbService)
    cache = mgr.get(CacheService)
    api = mgr.get(ApiService)

    assert cache.db is db
    assert api.db is db
    assert api.cache is cache


async def test_逆序关闭() -> None:
    mgr = ServiceManager()
    _ = mgr.register(DbService)
    _ = mgr.register(CacheService)
    _ = mgr.register(ApiService)

    await mgr.start_all()
    await mgr.stop_all()

    stops = [e for e in events if e.startswith("stop:")]
    assert stops == ["stop:api", "stop:cache", "stop:db"]


async def test_重复启动后关闭仍严格逆序() -> None:
    """回归：旧实现只记「本次新启动」的服务，二次 start_all 后关闭会退化。"""
    mgr = ServiceManager()
    _ = mgr.register(DbService)
    _ = mgr.register(CacheService)
    _ = mgr.register(ApiService)

    await mgr.start_all()
    await mgr.start_all()
    await mgr.stop_all()

    stops = [e for e in events if e.startswith("stop:")]
    assert stops == ["stop:api", "stop:cache", "stop:db"]


async def test_启动失败时回滚已启动服务() -> None:
    mgr = ServiceManager()
    _ = mgr.register(FailingDbService)
    _ = mgr.register(FailingApiService)

    with pytest.raises(ServiceStartError) as excinfo:
        await mgr.start_all()

    assert excinfo.value.service_name == "api"
    assert "stop:db" in events
    assert mgr.get(FailingDbService).state is ServiceState.STOPPED
    assert mgr.get(FailingApiService).state is ServiceState.FAILED


async def test_启停幂等() -> None:
    mgr = ServiceManager()
    _ = mgr.register(DbService)
    svc = mgr.get(DbService)

    await mgr.start_all()
    await mgr.start_all()
    assert svc.start_calls == 1

    await mgr.stop_all()
    await mgr.stop_all()
    assert svc.stop_calls == 1


async def test_检测循环依赖() -> None:
    mgr = ServiceManager()
    _ = mgr.register(CycleA)
    _ = mgr.register(CycleB)

    with pytest.raises(CircularDependencyError):
        await mgr.start_all()


async def test_检测缺失依赖() -> None:
    mgr = ServiceManager()
    _ = mgr.register(OrphanService)

    with pytest.raises(MissingDependencyError):
        await mgr.start_all()


async def test_关闭异常不影响其他服务() -> None:
    mgr = ServiceManager()
    _ = mgr.register(DbService)
    _ = mgr.register(BadStopService)

    await mgr.start_all()
    await mgr.stop_all()

    assert "stop:db" in events
    assert mgr.get(BadStopService).state is ServiceState.FAILED


async def test_健康检查聚合() -> None:
    mgr = ServiceManager()
    _ = mgr.register(DbService)
    _ = mgr.register(CacheService)
    _ = mgr.register(ApiService)

    before = await mgr.health()
    assert all(not item.healthy for item in before)

    await mgr.start_all()
    after = await mgr.health()
    assert all(item.healthy for item in after)
    assert [item.name for item in after] == ["db", "cache", "api"]
