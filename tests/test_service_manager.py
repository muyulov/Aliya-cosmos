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
    """记录启停顺序的测试服务。

    name 与 dependencies 是类级常量（ClassVar），因此在类上声明，
    实例化时通过工厂函数 make_service 动态生成子类来区分不同服务。
    """

    name: ClassVar[str] = "recorder"
    dependencies: ClassVar[tuple[type[Service], ...]] = ()

    def __init__(self) -> None:
        super().__init__()
        self.start_calls: int = 0
        self.stop_calls: int = 0
        self.fail_on_start: bool = False

    @override
    async def start(self) -> None:
        self.start_calls += 1
        if self.fail_on_start:
            msg = f"{self.name} 启动失败"
            raise RuntimeError(msg)
        events.append(f"start:{self.name}")

    @override
    async def stop(self) -> None:
        self.stop_calls += 1
        events.append(f"stop:{self.name}")


def make_service(name: str, deps: tuple[str, ...] = ()) -> RecordService:
    """动态生成具名服务子类，模拟真实的「一个类一个服务名」写法。"""
    return type(
        f"RecordService_{name}",
        (RecordService,),
        {"name": name, "dependencies": deps},
    )()


@pytest.fixture(autouse=True)
def _clear_events() -> None:
    events.clear()


async def test_按依赖顺序启动() -> None:
    mgr = ServiceManager()
    _ = mgr.register(make_service("db"))
    _ = mgr.register(make_service("cache", ("db",)))
    _ = mgr.register(make_service("api", ("cache", "db")))

    await mgr.start_all()

    assert events.index("start:db") < events.index("start:cache") < events.index("start:api")


async def test_逆序关闭() -> None:
    mgr = ServiceManager()
    _ = mgr.register(make_service("db"))
    _ = mgr.register(make_service("api", ("db",)))

    await mgr.start_all()
    await mgr.stop_all()

    stops = [e for e in events if e.startswith("stop:")]
    assert stops == ["stop:api", "stop:db"]


async def test_启动失败时回滚已启动服务() -> None:
    mgr = ServiceManager()
    ok = make_service("db")
    bad = make_service("api", ("db",))
    bad.fail_on_start = True
    _ = mgr.register(ok)
    _ = mgr.register(bad)

    with pytest.raises(ServiceStartError) as excinfo:
        await mgr.start_all()

    assert excinfo.value.service_name == "api"
    assert "stop:db" in events
    assert ok.state is ServiceState.STOPPED
    assert bad.state is ServiceState.FAILED


async def test_启停幂等() -> None:
    mgr = ServiceManager()
    svc = make_service("db")
    _ = mgr.register(svc)

    await mgr.start_all()
    await mgr.start_all()
    assert svc.start_calls == 1

    await mgr.stop_all()
    await mgr.stop_all()
    assert svc.stop_calls == 1


async def test_检测循环依赖() -> None:
    mgr = ServiceManager()
    _ = mgr.register(make_service("a", ("b",)))
    _ = mgr.register(make_service("b", ("a",)))

    with pytest.raises(CircularDependencyError):
        await mgr.start_all()


async def test_检测缺失依赖() -> None:
    mgr = ServiceManager()
    _ = mgr.register(make_service("api", ("db",)))

    with pytest.raises(MissingDependencyError):
        await mgr.start_all()


async def test_同名服务重复注册报错() -> None:
    mgr = ServiceManager()
    _ = mgr.register(make_service("db"))
    with pytest.raises(ValueError, match="服务名重复"):
        _ = mgr.register(make_service("db"))


async def test_关闭异常不影响其他服务() -> None:
    class BadStop(RecordService):
        name: ClassVar[str] = "bad-stop"

        @override
        async def stop(self) -> None:
            msg = "关闭炸了"
            raise RuntimeError(msg)

    mgr = ServiceManager()
    _ = mgr.register(make_service("db"))
    bad = BadStop()
    _ = mgr.register(bad)

    await mgr.start_all()
    await mgr.stop_all()

    assert "stop:db" in events


async def test_健康检查聚合() -> None:
    mgr = ServiceManager()
    _ = mgr.register(make_service("db"))
    _ = mgr.register(make_service("api", ("db",)))

    before = await mgr.health()
    assert all(not item.healthy for item in before)

    await mgr.start_all()
    after = await mgr.health()
    assert all(item.healthy for item in after)
    assert [item.name for item in after] == ["db", "api"]
