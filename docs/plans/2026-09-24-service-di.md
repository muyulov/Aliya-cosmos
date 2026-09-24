# service 层依赖注入改造 实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 把 `core/service` 从"按 name 排序启停的注册表"改造成"按类型装配并注入依赖的服务容器"，并新增 `ClockService` 示例兑现注入收益。

**Architecture:** 构造器注入 + 惰性装配。`ServiceManager` 只登记服务**类型**，首次访问时校验契约、拓扑排序、按序构造并把依赖作为普通 `__init__` 参数传入。`Settings` 由容器内置注入，服务自带带维度的 `self.log`。设计依据：`docs/plans/2026-09-12-service-di-design.md`。

**Tech Stack:** Python 3.12+、uv、pytest（asyncio_mode=auto）、ruff、loguru、pydantic-settings。

**前置状态：** 隔离工作区已建好（`.worktrees/service-di`，分支 `feature/service-di`），基线 61 tests 全绿。所有路径均相对于该工作区根目录。

**与设计文档的三处实施期偏差（已在下面各 Task 中体现）：**

1. 设计说测试里 `make_service` 改为"返回类"。实施改用**显式测试类**：契约校验要求 `dependencies` 与 `__init__` 签名逐一对上，动态造类必须用 `exec` 拼注解，可读性差且易碎。
2. 设计里 `ClockService.start` 写了"判状态后 return"。实施**不重写 `start`**：manager 已在调用前跳过 `RUNNING`，基类的空实现本身幂等，重写是死代码（YAGNI）。
3. 设计未提及 `get_by_name`。`name` 降级为展示标签后 `get_by_name` 语义已不成立（名字不再唯一），实施**删除该方法**（全仓库无调用点）。

---

## Task 1: 日志门面新增 `bind` / `prefix`

**Files:**

- Modify: `core/logger/setup.py`
- Test: `tests/test_logger_sinks.py`

**背景：** 服务要用 `log.bind(服务="clock").prefix("clock")` 拿到带自身维度的门面。当前 `core/logger/setup.py` 的 `Log` 类只暴露 `debug/info/success/warning/error/critical/exception/context`，没有 `bind`，也没有 `prefix`。这是后续所有 Task 的前置。

**Step 1: 写失败的测试**

在 `tests/test_logger_sinks.py` 末尾追加（该文件已有 `_cfg` 助手与 `_restore_logger` autouse fixture，直接复用）：

```python
def test_bind_附加基础字段(tmp_path: Path) -> None:
    from core.logger import log

    setup_logging(_cfg(tmp_path))
    log.bind(服务="clock").info("绑定了字段")
    logger.remove()

    text = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
    assert "服务: clock" in text


def test_prefix_渲染消息前缀(tmp_path: Path) -> None:
    from core.logger import log

    setup_logging(_cfg(tmp_path))
    log.prefix("clock").info("时钟服务已启动")
    logger.remove()

    text = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
    assert "[clock] 时钟服务已启动" in text


def test_bind_与_prefix_可链式叠加(tmp_path: Path) -> None:
    from core.logger import log

    setup_logging(_cfg(tmp_path))
    log.bind(服务="clock").prefix("clock").info("链式")
    logger.remove()

    text = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
    assert "[clock] 链式" in text
    assert "服务: clock" in text


def test_调用点字段覆盖_bind_字段(tmp_path: Path) -> None:
    """bind 的字段优先级最低，调用点显式传值应胜出。"""
    from core.logger import log

    setup_logging(_cfg(tmp_path))
    log.bind(服务="clock").info("覆盖", 服务="item")
    logger.remove()

    text = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
    assert "服务: item" in text
    assert "服务: clock" not in text


def test_prefix_作用于_exception(tmp_path: Path) -> None:
    from core.logger import log

    setup_logging(_cfg(tmp_path))
    try:
        raise KeyError("boom")
    except KeyError:
        log.prefix("clock").exception("带堆栈")
    logger.remove()

    text = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
    assert "[clock] 带堆栈" in text
```

**Step 2: 运行测试确认失败**

```bash
uv run pytest tests/test_logger_sinks.py -q -k "bind or prefix"
```

Expected: 5 个用例 FAIL（`AttributeError: 'Log' object has no attribute 'bind'` / `'prefix'`）。

**Step 3: 实现**

把 `core/logger/setup.py` 的 `Log` 类整体替换为（保留文件其余部分不动）：

```python
class Log:
    """日志门面：把 kwargs 收集为结构化字段并注入 loguru。

    类型注解约定：face 为颜文字字符串，fields 为任意键值的结构化字段，
    因此这里必须宽松（object），这也是 loguru 自身的签名风格。

    门面可派生：bind 追加基础字段，prefix 追加消息前缀，两者互不干扰，
    各自返回新实例，互不修改原对象。
    """

    #: 颜文字常量表，便于 log.face.CHEER 这样取用
    face: ModuleType = faces_module

    def __init__(self, base: dict[str, object] | None = None, prefix: str = "") -> None:
        self._base: dict[str, object] = dict(base) if base else {}
        self._prefix: str = prefix

    def bind(self, **kv: object) -> Log:
        """返回带基础字段的派生门面。

        字段优先级为「基础字段 → 上下文 → 调用点」，调用点最高，
        因此服务里临时覆盖 服务= 这类字段是可行的。
        """
        return Log({**self._base, **kv}, self._prefix)

    def prefix(self, text: str) -> Log:
        """返回带消息前缀的派生门面，渲染为 [text] 消息。"""
        return Log(self._base, text)

    def _compose(self, message: str) -> str:
        return f"[{self._prefix}] {message}" if self._prefix else message

    def _emit(self, level: str, message: str, face: str | None = None, **fields: object) -> None:
        resolved = face or faces_module.DEFAULT_BY_LEVEL.get(level.upper(), "")
        merged = {**self._base, **context_module.current(), **fields}
        logger.bind(face=resolved, fields=merged).log(level.upper(), self._compose(message))

    def debug(self, message: str, face: str | None = None, **fields: object) -> None:
        self._emit("DEBUG", message, face, **fields)

    def info(self, message: str, face: str | None = None, **fields: object) -> None:
        self._emit("INFO", message, face, **fields)

    def success(self, message: str, face: str | None = None, **fields: object) -> None:
        self._emit("SUCCESS", message, face, **fields)

    def warning(self, message: str, face: str | None = None, **fields: object) -> None:
        self._emit("WARNING", message, face, **fields)

    def error(self, message: str, face: str | None = None, **fields: object) -> None:
        self._emit("ERROR", message, face, **fields)

    def critical(self, message: str, face: str | None = None, **fields: object) -> None:
        self._emit("CRITICAL", message, face, **fields)

    def exception(self, message: str, face: str | None = None, **fields: object) -> None:
        """记录异常并附带 traceback。"""
        resolved = face or faces_module.DEFAULT_BY_LEVEL.get("ERROR", "")
        merged = {**self._base, **context_module.current(), **fields}
        logger.bind(face=resolved, fields=merged).exception(self._compose(message))

    @contextmanager
    def context(self, **kv: object) -> Generator[None, None, None]:
        """临时附加业务维度到日志上下文，退出时自动还原。

        返回类型写 Generator 而非 Iterator：@contextmanager 用 Iterator 标注
        已被类型检查器视为弃用。
        """
        token = context_module.bind(**kv)
        try:
            yield
        finally:
            context_module.reset(token)
```

**Step 4: 运行测试确认通过**

```bash
uv run pytest tests/test_logger_sinks.py tests/test_logger.py tests/test_logger_context.py -q
```

Expected: 全部 PASS（新增 5 项 + 既有日志用例不回归）。

**Step 5: 静态检查**

```bash
uv run ruff check . && uv run ruff format --check .
```

Expected: 无输出、退出码 0。

**Step 6: 提交**

```bash
git add core/logger/setup.py tests/test_logger_sinks.py
git commit -m "feat(logger): 门面新增 bind 与 prefix 派生能力"
```

---

## Task 2: `Service` 契约层新增 `label` / `self.log` / `log_error`

**Files:**

- Modify: `core/service/base.py`
- Create: `tests/test_service_base.py`

**背景：** 服务要自带带维度的 logger，且 `manager.py` 里三处 `错误=f"{type(exc).__name__}: {exc}"` 手工格式化要收敛到一处。本 Task 只动基类，不动 manager，因此既有测试仍应全绿。

**Step 1: 写失败的测试**

创建 `tests/test_service_base.py`：

```python
"""Service 契约层测试：展示名、自带日志与统一错误格式。"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest
from loguru import logger

from core.config import LogSettings
from core.logger import setup_logging
from core.service.base import Service


@pytest.fixture(autouse=True)
def _restore_logger():
    """每个用例前后重置 loguru，避免 sink 泄漏到其他测试。"""
    yield
    _ = logger.remove()


def _cfg(tmp_path: Path) -> LogSettings:
    return LogSettings(level="DEBUG", dir=str(tmp_path / "logs"), retention="1 day")


class DemoService(Service):
    """显式声明 name 的服务。"""

    name: ClassVar[str] = "demo"


class UnnamedService(Service):
    """不声明 name 的服务。"""


def test_展示名缺省取类名() -> None:
    assert UnnamedService().label == "UnnamedService"


def test_展示名显式优先于类名() -> None:
    assert DemoService().label == "demo"


def test_服务日志自动带服务字段与消息前缀(tmp_path: Path) -> None:
    setup_logging(_cfg(tmp_path))
    DemoService().log.info("演示")
    logger.remove()

    text = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
    assert "[demo] 演示" in text
    assert "服务: demo" in text


def test_门面可继续派生(tmp_path: Path) -> None:
    """self.log 是普通门面，仍可 bind / prefix 出更细的维度。"""
    setup_logging(_cfg(tmp_path))
    DemoService().log.bind(阶段="预热").info("派生日志")
    logger.remove()

    text = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
    assert "[demo] 派生日志" in text
    assert "服务: demo" in text
    assert "阶段: 预热" in text


def test_log_error_统一错误格式(tmp_path: Path) -> None:
    setup_logging(_cfg(tmp_path))
    DemoService().log_error("保存失败", RuntimeError("磁盘满"))
    logger.remove()

    text = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
    assert "错误: RuntimeError: 磁盘满" in text


def test_log_error_不传异常时不带错误字段(tmp_path: Path) -> None:
    setup_logging(_cfg(tmp_path))
    DemoService().log_error("仅提示")
    logger.remove()

    text = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
    assert "仅提示" in text
    assert "错误:" not in text


def test_log_error_可附加自定义字段(tmp_path: Path) -> None:
    setup_logging(_cfg(tmp_path))
    DemoService().log_error("保存失败", ValueError("坏值"), 重试次数=2)
    logger.remove()

    text = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
    assert "错误: ValueError: 坏值" in text
    assert "重试次数: 2" in text
```

**Step 2: 运行测试确认失败**

```bash
uv run pytest tests/test_service_base.py -q
```

Expected: FAIL（`AttributeError: 'DemoService' object has no attribute 'label' / 'log'`）。

**Step 3: 实现**

把 `core/service/base.py` 的模块 docstring 与 `Service` 类替换为下面的内容（`ServiceState` 与 `HealthStatus` 保持不变，仅需把 `Service.health()` 里的 `name=self.name` 改为 `name=self.label`）：

模块顶部 import 追加：

```python
from core.logger import Log, log
```

模块 docstring 替换为：

```python
"""服务基类与状态机。

一个服务就是一个实现了 Service 的类，由 ServiceManager 统一管理生命周期。
服务之间按**类型**声明依赖，容器据此排序并在构造时注入。
"""
```

`Service` 类整体替换为：

```python
class Service:
    """服务基类。

    子类按需声明 name（展示标签，缺省取类名）与 dependencies（依赖的服务类型），
    并在构造器里接收依赖。容器在装配期把依赖作为普通参数注入。

    新增约定：`__init__` 只做赋值与接收依赖，连接、预热、加载这类动资源的活
    一律留到 `start()`。装配发生在 lifespan 之前，在 `__init__` 里连资源会让
    "装配失败"与"启动失败"混成一锅，回滚逻辑也会失去意义。

    start 与 stop 必须是幂等的：重复调用不应报错，因此基类提供空实现，
    子类只重写自己关心的方法，不做强制约束。

    不继承 ABC：服务的契约由 ServiceManager 在装配期校验（依赖声明与构造器
    签名对账），比抽象方法更适合"按需重写"的场景。
    """

    #: 展示名：仅用于日志与健康检查展示，缺省取类名，不参与依赖解析
    name: ClassVar[str] = ""

    #: 依赖的服务类型；容器据此排序并在构造时注入
    dependencies: ClassVar[tuple[type[Service], ...]] = ()

    def __init__(self) -> None:
        self.state: ServiceState = ServiceState.CREATED
        #: 带自身维度的日志门面：自带 服务= 字段与 [label] 消息前缀
        self.log: Log = log.bind(服务=self.label).prefix(self.label)

    @property
    def label(self) -> str:
        """展示名：显式 name 优先，否则取类名。"""
        return self.name or type(self).__name__

    def log_error(
        self,
        message: str,
        exc: BaseException | None = None,
        face: str | None = None,
        **fields: object,
    ) -> None:
        """统一错误日志：自动拼「错误=类型: 消息」，可选带异常对象。

        收敛 manager 与各服务里重复的 f"{type(exc).__name__}: {exc}" 格式化。
        face 显式声明而非依赖 **fields 的偶然绑定：否则类型检查器会认为
        任意 object 都可能是 face，报「无法赋值给 str | None」。
        """
        if exc is not None:
            fields["错误"] = f"{type(exc).__name__}: {exc}"
        self.log.error(message, face, **fields)
```

实施期修正：设计文档给的签名是 `log_error(message, exc=None, **fields)`，实施时补了显式
`face` 形参。原签名下 `self.log.error(message, **fields)` 会被 basedpyright 报
`reportArgumentType`（`object` 不能赋给 `face: str | None`），且 `face` 能否生效依赖
`**fields` 解包时的偶然绑定——显式声明后两个问题一起消失。
注意 `Log.error` 内部 `self._emit("ERROR", message, face, **fields)` 不报错，
是因为它把 `face` 按**位置**传递，已被绑定，类型检查器会跳过该形参。

```python
    @property
    def running(self) -> bool:
        return self.state is ServiceState.RUNNING

    async def start(self) -> None:
        """启动服务。子类重写时应先判断自身状态以保证幂等。"""

    async def stop(self) -> None:
        """停止服务。子类重写时应先判断自身状态以保证幂等。"""

    async def health(self) -> HealthStatus:
        """健康检查，默认按状态判断。"""
        healthy = self.state is ServiceState.RUNNING
        detail = "" if healthy else f"服务未运行，当前状态 {self.state.value}"
        return HealthStatus(name=self.label, healthy=healthy, state=self.state, detail=detail)

    @override
    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r} state={self.state.value}>"
```

**Step 4: 运行测试确认通过**

```bash
uv run pytest -q
```

Expected: 全部 PASS（Task 1 的 5 项 + 本 Task 7 项 + 既有 61 项）。此步不能出现回归：`ItemService.dependencies` 此时仍是空的 `()`，`RecordService` 仍声明 `tuple[str, ...]`，manager 未改动，因此行为不变。

**Step 5: 静态检查与提交**

```bash
uv run ruff check . && uv run ruff format --check .
git add core/service/base.py tests/test_service_base.py
git commit -m "feat(service): 基类新增展示名、自带日志与统一错误格式"
```

---

## Task 3: `ServiceManager` 改造为按类型装配的服务容器

**Files:**

- Modify: `core/service/manager.py`（整体重写）
- Modify: `core/service/registry.py`
- Modify: `core/service/__init__.py`
- Modify: `core/api/app.py:31`
- Rewrite: `tests/test_service_manager.py`
- Create: `tests/test_service_injection.py`

**背景：** 容器改按类型注册与查找，装配惰性化，契约校验 fail fast，关闭顺序在装配期固化（修掉"重复 `start_all()` 后 `stop_all()` 退化成注册顺序"的 bug）。本 Task 结束时 `ItemService` 仍是老样子（无依赖），只把"注册类型"这件事打通。

**Step 1: 重写测试（先写测试，此时必然失败）**

把 `tests/test_service_manager.py` 整体替换为：

```python
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
        self.db = db


class ApiService(RecordService):
    name: ClassVar[str] = "api"
    dependencies: ClassVar[tuple[type[Service], ...]] = (CacheService, DbService)

    def __init__(self, cache: CacheService, db: DbService) -> None:
        super().__init__()
        self.cache = cache
        self.db = db


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
        self.db = db

    @override
    async def start(self) -> None:
        self.start_calls += 1
        events.append(f"start:{self.label}")
        msg = "启动炸了"
        raise RuntimeError(msg)


class CycleA(RecordService):
    """环的一侧：注解延迟解析，因此可以先写字符串注解再补 dependencies。"""

    name: ClassVar[str] = "a"

    def __init__(self, b: CycleB) -> None:
        super().__init__()
        self.b = b


class CycleB(RecordService):
    name: ClassVar[str] = "b"

    def __init__(self, a: CycleA) -> None:
        super().__init__()
        self.a = a


# 成环声明必须在两个类都定义后补上（注解因 from __future__ import annotations 延迟求值）
CycleA.dependencies = (CycleB,)
CycleB.dependencies = (CycleA,)


class OrphanService(RecordService):
    """依赖的类型没注册。"""

    name: ClassVar[str] = "orphan"
    dependencies: ClassVar[tuple[type[Service], ...]] = (DbService,)

    def __init__(self, db: DbService) -> None:
        super().__init__()
        self.db = db


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
    _ = mgr.register(ApiService)

    await mgr.start_all()
    await mgr.stop_all()

    stops = [e for e in events if e.startswith("stop:")]
    assert stops == ["stop:api", "stop:cache", "stop:db"]


async def test_重复启动后关闭仍严格逆序() -> None:
    """回归：旧实现只记「本次新启动」的服务，二次 start_all 后关闭会退化。"""
    mgr = ServiceManager()
    _ = mgr.register(DbService)
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
    _ = mgr.register(ApiService)

    before = await mgr.health()
    assert all(not item.healthy for item in before)

    await mgr.start_all()
    after = await mgr.health()
    assert all(item.healthy for item in after)
    assert [item.name for item in after] == ["db", "cache", "api"]
```

创建 `tests/test_service_injection.py`：

```python
"""容器装配与依赖注入测试。"""

from __future__ import annotations

from typing import ClassVar, cast

import pytest

from core.config import AppSettings, Settings
from core.service.base import Service
from core.service.manager import (
    ServiceContractError,
    ServiceManager,
    ServiceNotRegisteredError,
)

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
```

**Step 2: 运行测试确认失败**

```bash
uv run pytest tests/test_service_manager.py tests/test_service_injection.py -q
```

Expected: 大量 FAIL（`register` 收到类对象后被当成实例访问 `service.name`，`get` / `services` 等行为也不符）。

**Step 3: 实现**

把 `core/service/manager.py` 整体替换为：

```python
"""服务容器：类型注册、惰性装配与生命周期编排。

职责：
- 注册服务**类型**（不是实例），服务之间按**类型**声明依赖。
- 惰性装配：首次访问时校验契约、拓扑排序、按序构造并注入依赖。
- 启动：按装配期固化的顺序逐个启动并记录耗时；任一失败则逆序回滚。
- 关闭：按启动顺序的逆序逐个关闭，吞掉单个异常，保证其余服务都能停下。
- 健康检查：聚合所有服务的状态。

装配失败一律 fail fast，抛 ServiceError 子树；ServiceError 不是 AppError，
因此只会让 lifespan 启动失败、进程退出，不会变成 4xx。
"""

from __future__ import annotations

import inspect
import time
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from typing import TypeVar, cast, get_type_hints

from core.config import Settings, get_settings
from core.logger import faces, log
from core.service.base import HealthStatus, Service, ServiceState

S = TypeVar("S", bound=Service)


class ServiceManager:
    """服务注册表与生命周期编排器。

    装配是惰性的：register 只登记类型，首次访问（get / services / names /
    health / start_all / stop_all）才校验契约、排序并构造实例。
    因此模块级构造一个 manager 不读配置、不实例化服务，没有 import 副作用。
    """

    def __init__(self, settings: Settings | None = None) -> None:
        #: 容器持有的配置；装配时若为 None 则回退到全局单例
        self._settings = settings
        #: 注册顺序（装配前）
        self._types: list[type[Service]] = []
        #: 装配产出的实例表
        self._instances: dict[type[Service], Service] = {}
        #: 装配后固化的启动顺序，关闭时直接逆序，无需可变状态
        self._order: list[type[Service]] = []
        self._built = False

    # ---------- 注册 ----------

    def register(self, service_type: type[S]) -> type[S]:
        """注册服务类型。非 Service 子类、重复注册、装配后注册都会抛错。"""
        self._ensure_not_built()
        if not (isinstance(service_type, type) and issubclass(service_type, Service)):
            name = getattr(service_type, "__name__", repr(service_type))
            raise ServiceContractError(f"{name} 不是 Service 子类，无法注册")
        if service_type in self._types:
            raise ServiceContractError(f"服务类型重复注册：{service_type.__name__}")
        self._types.append(service_type)
        return service_type

    def register_all(self, service_types: Sequence[type[Service]]) -> None:
        for service_type in service_types:
            _ = self.register(service_type)

    # ---------- 查找 ----------

    def get(self, service_type: type[S]) -> S:
        """按**精确类型**取服务实例。

        刻意不做 isinstance 线性扫描：那会在"注册的是子类、查的是基类"时
        静默返回第一个匹配，属于隐式行为。因此依赖必须声明被注册的具体类型。
        """
        self._ensure_built()
        try:
            instance = self._instances[service_type]
        except KeyError as exc:
            raise ServiceNotRegisteredError(service_type) from exc
        return cast("S", instance)

    @property
    def services(self) -> Sequence[Service]:
        """全部服务实例，按启动顺序。"""
        self._ensure_built()
        return tuple(self._instances[service_type] for service_type in self._order)

    @property
    def names(self) -> Sequence[str]:
        """全部服务的展示名，按启动顺序。"""
        return tuple(service.label for service in self.services)

    # ---------- 启动与关闭 ----------

    async def start_all(self) -> None:
        """按依赖顺序启动全部服务，失败则回滚。"""
        self._ensure_built()
        started: list[Service] = []

        for service_type in self._order:
            service = self._instances[service_type]
            if service.state is ServiceState.RUNNING:
                continue

            service.state = ServiceState.STARTING
            begin = time.perf_counter()
            try:
                await service.start()
            except Exception as exc:
                service.state = ServiceState.FAILED
                service.log_error("服务启动失败，开始回滚", exc, 已启动=len(started))
                await self._rollback(started)
                raise ServiceStartError(service.label, exc) from exc

            service.state = ServiceState.RUNNING
            started.append(service)
            elapsed_ms = round((time.perf_counter() - begin) * 1000, 1)
            log.info("服务已启动", face=faces.START, 服务=service.label, 耗时=f"{elapsed_ms}ms")

        log.info("全部服务启动完成", 服务数=len(started))

    async def stop_all(self) -> None:
        """按启动的逆序关闭所有服务。

        顺序在装配期就已固定为 reversed(self._order)，因此这里不需要记录
        "本次启动了哪些"，重复调用天然幂等。
        """
        self._ensure_built()
        for service_type in reversed(self._order):
            service = self._instances[service_type]
            if service.state in (ServiceState.STOPPED, ServiceState.CREATED):
                continue

            service.state = ServiceState.STOPPING
            try:
                await service.stop()
            except Exception as exc:
                # 关闭阶段不阻断其他服务，仅记录
                service.state = ServiceState.FAILED
                service.log_error("服务关闭异常", exc)
                continue

            service.state = ServiceState.STOPPED
            log.info("服务已停止", face=faces.BYE, 服务=service.label)

        log.info("全部服务已停止")

    async def _rollback(self, started: Sequence[Service]) -> None:
        """逆序回滚本次已启动的服务。"""
        for service in reversed(started):
            try:
                await service.stop()
            except Exception as exc:
                service.state = ServiceState.FAILED
                service.log_error("回滚时服务关闭异常", exc)
            else:
                service.state = ServiceState.STOPPED

    # ---------- 健康检查 ----------

    async def health(self) -> list[HealthStatus]:
        """聚合所有服务的健康状态。"""
        results: list[HealthStatus] = []
        for service in self.services:
            try:
                results.append(await service.health())
            except Exception as exc:
                results.append(
                    HealthStatus(
                        name=service.label,
                        healthy=False,
                        state=service.state,
                        detail=f"健康检查抛错：{type(exc).__name__}: {exc}",
                    )
                )
        return results

    # ---------- 生命周期上下文 ----------

    @asynccontextmanager
    async def lifespan(self) -> AsyncGenerator[ServiceManager]:
        """异步上下文：进入时启动全部服务，退出时全部关闭。"""
        await self.start_all()
        try:
            yield self
        finally:
            await self.stop_all()

    # ---------- 装配 ----------

    def _ensure_built(self) -> None:
        """首次访问时触发一次装配。"""
        if not self._built:
            self._build()

    def _ensure_not_built(self) -> None:
        if self._built:
            msg = "服务容器已完成装配，不能再注册新服务"
            raise ServiceContractError(msg)

    def _build(self) -> None:
        """校验契约 → 拓扑排序 → 按序构造注入 → 固化顺序。"""
        settings = self._settings or get_settings()
        signatures = {
            service_type: self._validate_contract(service_type) for service_type in self._types
        }
        order = self._resolve_order()

        for service_type in order:
            kwargs: dict[str, object] = {}
            for param_name, param_type in signatures[service_type].items():
                if param_type is Settings:
                    kwargs[param_name] = settings
                else:
                    kwargs[param_name] = self._instances[param_type]
            self._instances[service_type] = service_type(**kwargs)

        self._order = order
        self._built = True

    def _validate_contract(self, service_type: type[Service]) -> dict[str, type[object]]:
        """对账 dependencies 声明与 __init__ 签名，返回「参数名 → 注入类型」。

        用 get_type_hints 而非裸注解：本仓库满屏 from __future__ import
        annotations，注解都是字符串，必须显式解析。
        """
        hints = get_type_hints(service_type.__init__)
        _ = hints.pop("return", None)

        params: dict[str, type[object]] = {}
        for param_name, param in inspect.signature(service_type.__init__).parameters.items():
            if param_name == "self":
                continue
            if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
                raise ServiceContractError(
                    f"{service_type.__name__}.__init__ 含可变参数 {param_name}，容器无法注入"
                )

            param_type = hints.get(param_name)
            if param_type is None:
                raise ServiceContractError(
                    f"{service_type.__name__}.__init__ 的参数 {param_name} 缺少类型注解，"
                    f"容器无法注入"
                )
            if param_type is Settings:
                params[param_name] = Settings
                continue
            if isinstance(param_type, type) and issubclass(param_type, Service):
                params[param_name] = param_type
                continue

            label = getattr(param_type, "__name__", repr(param_type))
            raise ServiceContractError(
                f"{service_type.__name__}.__init__ 的参数 {param_name} 注解为 {label}，"
                f"容器只支持 Service 与 Settings"
            )

        declared = set(service_type.dependencies)
        injected = {value for value in params.values() if value is not Settings}

        undeclared = injected - declared
        if undeclared:
            names = "、".join(sorted(item.__name__ for item in undeclared))
            raise ServiceContractError(
                f"{service_type.__name__} 的构造器依赖 {names} 未在 dependencies 中声明"
            )

        unbound = declared - injected
        if unbound:
            names = "、".join(sorted(item.__name__ for item in unbound))
            raise ServiceContractError(
                f"{service_type.__name__} 声明了依赖 {names}，但构造器没有对应参数"
            )

        return params

    def _resolve_order(self) -> list[type[Service]]:
        """按 dependencies 做拓扑排序，检测环与未注册依赖。"""
        order: list[type[Service]] = []
        visiting: set[type[Service]] = set()
        visited: set[type[Service]] = set()

        def visit(service_type: type[Service]) -> None:
            if service_type in visited:
                return
            if service_type in visiting:
                raise CircularDependencyError(service_type.__name__)
            if service_type not in self._types:
                raise MissingDependencyError(service_type.__name__)

            visiting.add(service_type)
            for dependency in service_type.dependencies:
                visit(dependency)
            visiting.discard(service_type)
            visited.add(service_type)
            order.append(service_type)

        for service_type in self._types:
            visit(service_type)
        return order


class ServiceError(Exception):
    """服务相关错误基类。

    不是 AppError，因此不会被转成 4xx：装配期错误只会让 lifespan 启动失败、
    进程退出，这是预期的 fail fast。
    """


class ServiceContractError(ServiceError):
    """服务契约不合法。

    覆盖：非 Service 子类、重复注册、装配后注册、构造器签名不可解释、
    dependencies 声明与构造器签名不一致。
    """


class ServiceNotRegisteredError(ServiceError):
    """按类型查找时该类型未注册。"""

    service_type: type[Service]

    def __init__(self, service_type: type[Service]) -> None:
        self.service_type = service_type
        super().__init__(f"服务未注册：{service_type.__name__}")


class ServiceStartError(ServiceError):
    """服务启动失败。"""

    service_name: str
    cause: BaseException

    def __init__(self, service_name: str, cause: BaseException) -> None:
        self.service_name = service_name
        self.cause = cause
        super().__init__(f"服务 {service_name} 启动失败：{type(cause).__name__}: {cause}")


class CircularDependencyError(ServiceError):
    """检测到循环依赖。"""

    def __init__(self, service_name: str) -> None:
        super().__init__(f"检测到循环依赖，涉及服务：{service_name}")


class MissingDependencyError(ServiceError):
    """依赖的服务未注册。"""

    def __init__(self, service_name: str) -> None:
        super().__init__(f"依赖的服务未注册：{service_name}")
```

把 `core/service/registry.py` 整体替换为：

```python
"""服务注册表。

提供两件事：
- build_manager()：构造一个装配好全部服务的 manager，供应用与脚本使用。
- default_manager：全局默认实例，供 CLI 与测试等无 Web 场景使用。

新增服务时，在 build_manager() 里 register 类型即可，无需改动其他文件。
装配是惰性的：这里只登记类型，不读配置、不实例化服务，因此模块级构造
default_manager 没有 import 副作用。
"""

from __future__ import annotations

from core.config import Settings
from core.service.item_service import ItemService
from core.service.manager import ServiceManager


def build_manager(settings: Settings | None = None) -> ServiceManager:
    """构造并装配全部服务的 manager。"""
    manager = ServiceManager(settings)
    _ = manager.register(ItemService)
    return manager


#: 全局默认 manager。Web 场景请使用 app.state.services，避免多实例互相干扰。
default_manager = build_manager()
```

把 `core/service/__init__.py` 整体替换为：

```python
"""服务层对外出口。"""

from core.service.base import HealthStatus, Service, ServiceState
from core.service.item_service import Item, ItemNotFoundError, ItemService
from core.service.manager import (
    CircularDependencyError,
    MissingDependencyError,
    ServiceContractError,
    ServiceError,
    ServiceManager,
    ServiceNotRegisteredError,
    ServiceStartError,
)
from core.service.registry import build_manager, default_manager

__all__ = [
    "CircularDependencyError",
    "HealthStatus",
    "Item",
    "ItemNotFoundError",
    "ItemService",
    "MissingDependencyError",
    "Service",
    "ServiceContractError",
    "ServiceError",
    "ServiceManager",
    "ServiceNotRegisteredError",
    "ServiceStartError",
    "ServiceState",
    "build_manager",
    "default_manager",
]
```

修改 `core/api/app.py` 第 31 行，把配置交给容器：

```python
    services = manager or build_manager(cfg)
```

**Step 4: 运行测试确认通过**

```bash
uv run pytest -q
```

Expected: 全部 PASS。此时 `tests/conftest.py` 与 `tests/test_logger_sinks.py` 里的 `mgr.register(ItemService())` 会失败——它们必须在本 Task 一并改掉（设计文档把这两处归在 P3，但 manager 一改就必须同步）：

- `tests/conftest.py`：`manager` fixture 改为

```python
@pytest.fixture
def manager(test_settings: Settings) -> ServiceManager:
    """只装配示例服务的 manager，并注入测试配置。"""
    return build_manager(test_settings)
```

  并在文件顶部把 `from core.service import ServiceManager` 改为
  `from core.service import ServiceManager` + `from core.service.registry import build_manager`。

- `tests/test_logger_sinks.py` 的 `_build_app` 改为

```python
def _build_app(tmp_path: Path):
    """构造带日志配置的应用，供接口级测试使用。"""
    from core.api import create_app
    from core.config import AppSettings, Settings, get_settings
    from core.service.registry import build_manager

    get_settings.cache_clear()
    settings = Settings(
        app=AppSettings(app_name="t", env="test", debug=False),
        log=_cfg(tmp_path),
    )
    return create_app(settings=settings, manager=build_manager(settings))
```

**Step 5: 静态检查与提交**

```bash
uv run ruff check . && uv run ruff format --check .
git add core/service/manager.py core/service/registry.py core/service/__init__.py core/api/app.py tests/test_service_manager.py tests/test_service_injection.py tests/conftest.py tests/test_logger_sinks.py
git commit -m "feat(service): 容器改为按类型装配并注入依赖"
```

---

## Task 4: 新增 `ClockService` 并让 `ItemService` 依赖注入

**Files:**

- Create: `core/service/clock_service.py`
- Modify: `core/service/item_service.py`
- Modify: `core/service/registry.py`
- Modify: `core/service/__init__.py`
- Modify: `tests/test_health.py`
- Test: `tests/test_service_injection.py`

**背景：** 这一步兑现注入的实际收益：单测里 `ItemService(FakeClock(...))` 就能锁死时间，断言 `created_at` 精确值，完全不需要容器参与。

**保留 `Item.created_at` 的缺省值（取舍说明）：** `Item` 的
`created_at: datetime = field(default_factory=lambda: datetime.now(UTC))` 保持不变——
它让 `Item` 能脱离服务单独构造。代价是同一字段存在两条来源（缺省值 vs 服务注入），
因此 `Item` 的 docstring 必须写明「缺省值仅为单独构造兜底，服务路径一律由 ClockService 供给」，
避免后来者直接 `Item(...)` 绕过注入的时钟。不改 `created_at` 为必填，是因为那需要重排
dataclass 字段顺序（`description` 有默认值），属对演示 DTO 公开签名的破坏性改动。

**Step 1: 写失败的测试**

在 `tests/test_service_injection.py` 末尾追加：

```python
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
```

同时把文件顶部 import 补全为：

```python
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
```

**Step 2: 运行测试确认失败**

```bash
uv run pytest tests/test_service_injection.py -q
```

Expected: FAIL（`ModuleNotFoundError: No module named 'core.service.clock_service'`）。

**Step 3: 实现**

创建 `core/service/clock_service.py`：

```python
"""时钟服务：为其他服务提供可替换的时间来源。

now() 刻意保持**同步**：它不涉及 I/O，服务方法不必一律 async。
这样依赖它的服务无需为了取时间把纯计算逻辑改成异步。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

from core.service.base import Service


class ClockService(Service):
    """约定：时间一律从这里取，测试时可注入固定时间的替身。"""

    name: ClassVar[str] = "clock"

    def now(self) -> datetime:
        """当前时间（UTC）。"""
        return datetime.now(UTC)
```

修改 `core/service/item_service.py`：

- 模块 docstring 改为：

```python
"""示例服务：内存仓储的条目管理。

演示完整链路：
- 声明依赖类型，由容器在构造时注入。
- start 预热数据，stop 释放资源。
- 时间来源取自 ClockService，因此测试可注入固定时间。
- 业务异常直接用 AppError 子类表达，由 api 层统一转成 HTTP 响应。
"""
```

- import 追加：

```python
from core.service.clock_service import ClockService
```

- `ItemService` 的声明与构造器改为：

```python
class ItemService(Service):
    """示例服务实现。"""

    name: ClassVar[str] = "item"
    dependencies: ClassVar[tuple[type[Service], ...]] = (ClockService,)

    def __init__(self, clock: ClockService) -> None:
        super().__init__()
        self._clock = clock
        self._items: dict[int, Item] = {}
        self._counter: count[int] = count(1)
```

- `_create` 改为：

```python
    def _create(self, name: str, description: str) -> Item:
        item = Item(
            id=next(self._counter),
            name=name,
            description=description,
            created_at=self._clock.now(),
        )
        self._items[item.id] = item
        return item
```

`core/service/registry.py` 里注册时钟（顺序即声明顺序）：

```python
from core.service.clock_service import ClockService
from core.service.item_service import ItemService
from core.service.manager import ServiceManager


def build_manager(settings: Settings | None = None) -> ServiceManager:
    """构造并装配全部服务的 manager。"""
    manager = ServiceManager(settings)
    _ = manager.register(ClockService)
    _ = manager.register(ItemService)
    return manager
```

`core/service/__init__.py` 追加 `ClockService` 的导入与导出：

```python
from core.service.clock_service import ClockService
```

`__all__` 里在 `"CircularDependencyError"` 之后插入 `"ClockService"`。

**Step 4: 运行测试确认通过**

```bash
uv run pytest -q
```

Expected: 全部 PASS。注意 `tests/test_items.py` 不需要改动：它走 `client` fixture，`ItemService` 已由容器装配好并注入时钟，`test_启动时预热一条示例数据` 仍应通过。

`tests/test_health.py` 也必须同步：新增 `ClockService` 后 `/api/v1/health` 返回两个服务，
该用例原断言 `len(services) == 1` 需要改为断言 `clock` 与 `item` 两个服务。

**Step 5: 冒烟启动**

```bash
uv run python -c "
import asyncio
from core.logger import setup_logging
from core.service import build_manager

async def main() -> None:
    setup_logging()
    async with build_manager().lifespan() as mgr:
        for item in await mgr.health():
            print(item.to_dict())

asyncio.run(main())
"
```

Expected: 打印两个服务（`clock`、`item`）且 `healthy` 均为 `True`，日志里能看到 `[clock]` / `[item]` 前缀与 `服务:` 字段。

**Step 6: 静态检查与提交**

```bash
uv run ruff check . && uv run ruff format --check .
git add core/service/clock_service.py core/service/item_service.py core/service/registry.py core/service/__init__.py tests/test_service_injection.py
git commit -m "feat(service): 新增时钟服务并让条目服务依赖注入"
```

---

## Task 5: README 更新与全量校验

**Files:**

- Modify: `README.md`

**Step 1: 重写 README 的服务章节**

把 `README.md` 中「## 如何新增一个服务」整节替换为：

```markdown
## 如何新增一个服务

服务是继承 `Service` 的类，`ServiceManager` 负责装配、启停与健康检查。
依赖按**类型**声明，容器在构造时注入。

1. 在 `core/service/` 新建文件，继承 `Service`：

```python
from typing import ClassVar

from core.service.base import Service


class CacheService(Service):
    # 依赖的服务类型；容器据此排序并在构造时注入
    dependencies: ClassVar[tuple[type[Service], ...]] = (DbService,)

    def __init__(self, db: DbService) -> None:
        super().__init__()  # 必须调用：负责设置 state 与 self.log
        self._db = db

    async def start(self) -> None: ...  # 建连接
    async def stop(self) -> None: ...   # 关连接
```

2. 在 `core/service/registry.py` 里注册**类型**（不是实例）：

```python
_ = manager.register(CacheService)
```

约定：

| 约定 | 说明 |
| --- | --- |
| `__init__` 只赋值 | 连接、预热、加载这类动资源的活一律留到 `start()`。装配发生在 lifespan 之前，在 `__init__` 里连资源会让「装配失败」和「启动失败」混成一锅，回滚也会失去意义 |
| 声明与签名对账 | `dependencies` 与构造器参数必须一一对应，容器装配时校验，不一致直接报 `ServiceContractError` |
| 依赖写具体类型 | 写**被注册的那个具体类型**，不能拿抽象基类占位，`get()` 按精确类型查找 |
| `name` 只是标签 | 缺省取类名，不参与依赖解析，也不要求全局唯一，仅用于日志与健康检查展示 |
| 自带日志 | `self.log` 已带 `服务=<label>` 字段与 `[<label>]` 消息前缀，仍可继续 `bind` / `prefix` |
| 统一错误格式 | 报错用 `self.log_error("消息", exc)`，自动拼「错误=类型: 消息」 |
| 配置注入 | 需要配置时在构造器声明 `settings: Settings`，容器会注入应用持有的配置 |

启停语义：`start` / `stop` 需幂等；启动按依赖拓扑排序，失败会逆序回滚；关闭严格按启动的逆序，单个服务出错不影响其余服务停下。

装配期错误都是 `ServiceError` 的子类，且**不是** `AppError`，因此不会被转成 4xx，只会让启动失败、进程退出（fail fast）：

| 类型 | 触发条件 |
| --- | --- |
| `ServiceContractError` | 非 `Service` 子类、重复注册、装配后注册、构造器签名不可解释、声明与签名不一致 |
| `ServiceNotRegisteredError` | `get()` 查的类型未注册 |
| `MissingDependencyError` | `dependencies` 里的类型没注册 |
| `CircularDependencyError` | 依赖成环 |
| `ServiceStartError` | 某个服务 `start()` 抛错（携带 `service_name` 与 `cause`） |
```

同时在「## 目录结构」中把 `service/` 一行改为：

```text
  service/       业务层：服务基类、容器、注册表、具体服务实现
```

**Step 2: 全量校验**

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

Expected: 全绿、无 lint 与格式问题。

**Step 3: 检查 IDE 诊断**

对 `core/service/`、`core/logger/setup.py`、`core/api/app.py` 读取 linter/类型诊断。
Expected: 无 error / warning。若出现 basedpyright 的 `reportDeprecated` 或类型参数缺失告警，按项目既有约定处理（`@contextmanager` 用 `Generator[X, None, None]`，不要写 `Iterator[X]`）。

**Step 4: 提交**

```bash
git add README.md
git commit -m "docs: 重写 README 服务章节"
```

---

## 完成标准

- `uv run pytest -q` 全绿（既有 61 项 + 新增约 25 项）。
- `uv run ruff check .` 与 `uv run ruff format --check .` 无输出。
- `core/api/v1/health` 返回 `clock` 与 `item` 两个服务且均健康。
- 依赖图可 grep：`dependencies: ClassVar[tuple[type[Service], ...]]` 在类顶端一眼可见。
- 关闭顺序回归测试（`test_重复启动后关闭仍严格逆序`）通过。
- README 示例照抄可跑。

## 交付后

按 superpowers:finishing-a-development-branch 处理合并：`feature/service-di` 以 `--no-ff` 并入 `main`，合并后跑全量校验，再清理隔离工作区。设计文档 `docs/plans/2026-09-12-service-di-design.md` 已随 `2d27331` 在 main 上，本计划文件随分支合入。
