# service 层配置注入与生命周期健壮性 实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 让服务能按类型拿到自己的配置节点，并让容器启停带超时、按依赖分层并发启动。

**Architecture:** 配置节点定义在 `core/config/settings.py` 并挂成 `Settings` 的顶层字段，容器装配时建 `{节点类型: 节点实例}` 索引，按构造器注解类型注入。生命周期侧把 `_resolve_order()` 换成 `_resolve_levels()`（`depth = max(依赖 depth) + 1`），同层用 `asyncio.gather` 并发启动、层间串行；`start` / `stop` 各套一个 `asyncio.timeout`，超时值由 `ServiceSettings` 给默认、服务级 `ClassVar` 三态覆盖。

**Tech Stack:** Python 3.12、uv、pytest（`asyncio_mode=auto`）、ruff、basedpyright、pydantic、loguru。

**设计依据：** `docs/plans/2026-09-25-service-config-lifecycle-design.md`（本计划与设计无偏差）。

**工作区：** 所有命令都在 `.worktrees/service-lifecycle` 根目录执行（分支 `feature/service-lifecycle`，基点 `532bec7`，基线 104 tests 全绿）。

**每个 Task 的固定收尾（不重复啰嗦）：**

```bash
uv run pytest | tail -3          # 注意别加 -q，addopts 里已有 -q，叠加会静默掉汇总行
uv run ruff check . && uv run ruff format --check .
uvx basedpyright core tests       # worktree 内 CLI 可信，基线为 0 errors 0 warnings 0 notes
```

**编码约定提醒（本项目既有规则，照做即可）：**

- 实例属性一律写类型注解（`self.x: T = v`），否则 basedpyright 报 `reportUnannotatedClassAttribute`。
- 函数调用结果未使用时写 `_ = ...`，否则报 `reportUnusedCallResult`。
- 中文全角标点会导致 ruff `RUF001~003`，但项目已在 `[tool.ruff.lint].ignore` 里放行。
- 编辑完文件后 `read_file` 核验一次是否真的落盘（本仓库有编辑器并发改文件的先例）。

---

## Task 1: 配置层新增 `ServiceSettings` / `ClockSettings`

**Files:**

- Modify: `core/config/settings.py`
- Modify: `core/config/__init__.py`
- Modify: `data/config/app.yaml`
- Test: `tests/test_config_loader.py`

**Step 1: 写失败的测试**

追加到 `tests/test_config_loader.py` 末尾（该文件已有 `_load(tmp_path, yaml_text, env_text=None)` 助手，直接用）：

```python
# ---- 服务与时钟配置节点 ----


def test_服务与时钟配置节点缺省值() -> None:
    settings = Settings()

    assert settings.service.start_timeout == 30.0
    assert settings.service.stop_timeout == 30.0
    assert settings.clock.tz == "UTC"


def test_服务与时钟配置可被yaml覆盖(tmp_path: Path) -> None:
    settings = _load(
        tmp_path,
        "service:\n  start_timeout: 1.5\n  stop_timeout: 2\nclock:\n  tz: Asia/Shanghai\n",
    )

    assert settings.service.start_timeout == 1.5
    assert settings.service.stop_timeout == 2.0
    assert settings.clock.tz == "Asia/Shanghai"


def test_时钟时区可走占位符(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOCK_TZ", "Asia/Tokyo")

    settings = _load(tmp_path, "clock:\n  tz: ${CLOCK_TZ:UTC}\n")

    assert settings.clock.tz == "Asia/Tokyo"
```

**Step 2: 运行测试确认失败**

```bash
uv run pytest tests/test_config_loader.py -k "配置节点 or 时钟时区" -v
```

Expected: 3 FAIL，`AttributeError: 'Settings' object has no attribute 'service'` / `'clock'`。

**Step 3: 实现**

`core/config/settings.py`：在 `LogSettings` 定义之后、`Settings` 定义之前插入：

```python
class ServiceSettings(BaseModel):
    """服务生命周期配置。"""

    start_timeout: float | None = 30.0  # 秒；None 表示不限制
    stop_timeout: float | None = 30.0


class ClockSettings(BaseModel):
    """时钟服务配置。"""

    tz: str = "UTC"
```

`Settings` 的字段区改为：

```python
    app: AppSettings = AppSettings()
    log: LogSettings = LogSettings()
    service: ServiceSettings = ServiceSettings()
    clock: ClockSettings = ClockSettings()
```

`core/config/__init__.py` 的 import 块改为：

```python
from core.config.settings import (
    AppSettings,
    ClockSettings,
    LogSettings,
    ServiceSettings,
    Settings,
    get_settings,
    load_settings,
)
```

`__all__` 里在 `"AppSettings"` 之后插 `"ClockSettings"`、在 `"LogSettings"` 之后插 `"ServiceSettings"`。

`data/config/app.yaml` 在 `log:` 段之后追加（值等于默认，保持"骨架即全貌"）：

```yaml
service:
  start_timeout: 30
  stop_timeout: 30

clock:
  tz: UTC          # 部署侧要覆盖时写成占位符：${CLOCK_TZ:UTC}
```

**Step 4: 运行测试确认通过**

```bash
uv run pytest tests/test_config_loader.py -v
uv run pytest | tail -3
```

Expected: 全绿，104 → 107 项。

**Step 5: 收尾检查与提交**

```bash
uv run ruff check . && uv run ruff format --check .
uvx basedpyright core tests
git add core/config/settings.py core/config/__init__.py data/config/app.yaml tests/test_config_loader.py
git commit -m "feat(config): 新增服务生命周期与时钟配置节点"
```

---

## Task 2: `Service` 基类新增超时三态

**Files:**

- Modify: `core/service/base.py`
- Modify: `core/service/__init__.py`
- Test: `tests/test_service_base.py`

**背景：** 哨兵类必须**公开**（`Unset` 而非 `_Unset`）。basedpyright 对可变量按不变型检查，子类把 `ClassVar[float | None | Unset]` 窄化成 `ClassVar[float | None]` 会报 `reportIncompatibleVariableOverride`（已用探针实测），所以服务作者覆盖时必须写全联合类型，也就必须能 import 到这个类名。

**Step 1: 写失败的测试**

在 `tests/test_service_base.py` 的 import 区追加：

```python
from core.service.base import UNSET, Service, Unset
```

（原来是 `from core.service.base import Service`，直接替换这一行。）

在文件末尾追加：

```python
class StartTimeoutService(Service):
    """覆盖 start 超时：None 表示该服务不限制。"""

    start_timeout: ClassVar[float | None | Unset] = None


class StopTimeoutService(Service):
    """覆盖 stop 超时：写具体秒数。"""

    stop_timeout: ClassVar[float | None | Unset] = 1.5


def test_超时缺省为未覆盖哨兵() -> None:
    assert Service.start_timeout is UNSET
    assert Service.stop_timeout is UNSET
    assert repr(UNSET) == "UNSET"


def test_服务可覆盖超时三态() -> None:
    assert StartTimeoutService.start_timeout is None
    assert StopTimeoutService.stop_timeout == 1.5


def test_未覆盖的那一项仍跟随全局() -> None:
    """两个 ClassVar 各自独立，覆盖了一个不影响另一个。"""
    assert StartTimeoutService.stop_timeout is UNSET
    assert StopTimeoutService.start_timeout is UNSET
```

**Step 2: 运行测试确认失败**

```bash
uv run pytest tests/test_service_base.py -k "超时" -v
```

Expected: FAIL，`ImportError: cannot import name 'UNSET' from 'core.service.base'`。

**Step 3: 实现**

`core/service/base.py`：import 区把 `from typing import ClassVar, override` 改为：

```python
from typing import ClassVar, Final, override
```

在 `ServiceState` 之前（模块级）插入：

```python
class Unset:
    """「未覆盖」哨兵。

    ClassVar 的默认值无法同时表达「未覆盖，跟随全局」与「显式设为 None，
    即不限制」两件事，因此引入一个只有身份意义的哨兵。

    公开成类名而不是私有：basedpyright 按不变型检查可变量，子类覆盖时必须
    写全联合类型 `float | None | Unset`，也就必须能 import 到它。
    """

    __slots__: ClassVar[tuple[str, ...]] = ()

    @override
    def __repr__(self) -> str:
        return "UNSET"


#: 服务未覆盖该项超时，沿用 ServiceSettings 里的默认值
UNSET: Final[Unset] = Unset()
```

`Service` 类里，在 `dependencies` 声明之后插入：

```python
    #: start 超时（秒）覆盖；UNSET 跟随全局，None 不限制，数字即该值
    start_timeout: ClassVar[float | None | Unset] = UNSET

    #: stop 超时（秒）覆盖；语义同 start_timeout
    stop_timeout: ClassVar[float | None | Unset] = UNSET
```

`core/service/__init__.py`：把 `from core.service.base import HealthStatus, Service, ServiceState` 改为：

```python
from core.service.base import UNSET, HealthStatus, Service, ServiceState, Unset
```

`__all__` 里插 `"UNSET"`（`"ServiceState"` 之前）与 `"Unset"`（`"ServiceState"` 之后）。

**Step 4: 运行测试确认通过**

```bash
uv run pytest tests/test_service_base.py -v
uv run pytest | tail -3
```

Expected: 全绿，107 → 110 项。

**Step 5: 收尾检查与提交**

```bash
uv run ruff check . && uv run ruff format --check .
uvx basedpyright core tests
git add core/service/base.py core/service/__init__.py tests/test_service_base.py
git commit -m "feat(service): 基类新增启停超时三态声明"
```

---

## Task 3: 容器按类型装配配置节点

**Files:**

- Modify: `core/service/manager.py`
- Test: `tests/test_service_injection.py`

**Step 1: 写失败的测试**

在 `tests/test_service_injection.py` 的 import 区追加：

```python
from pydantic import BaseModel

from core.config import ClockSettings
```

（现有 import 块保持不动，把上面两行插进去；`from core.config import AppSettings, Settings, get_settings` 这行不用改。）

在文件末尾追加：

```python
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

    with pytest.raises(ServiceContractError, match="顶层字段"):
        _ = mgr.services


def test_配置节点类型重复时报错() -> None:
    mgr = ServiceManager(DuplicateNodeSettings(extra_clock=ClockSettings()))
    _ = mgr.register(NodeConsumerService)

    with pytest.raises(ServiceContractError, match="出现多次"):
        _ = mgr.services
```

**Step 2: 运行测试确认失败**

```bash
uv run pytest tests/test_service_injection.py -k "配置节点" -v
```

Expected: 3 FAIL。前两条报 `ServiceContractError: ... 容器只支持 Service 与 Settings`（旧的校验分支不认 `BaseModel`），第三条同样走旧分支而不报"出现多次"。

**Step 3: 实现**

`core/service/manager.py` 的改动：

① import 区改为：

```python
import asyncio  # Task 6 才用到，本 Task 先别加，见下方说明
import inspect
import time
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TypeVar, cast, get_type_hints

from pydantic import BaseModel

from core.config import Settings, get_settings
from core.logger import faces, log
from core.service.base import HealthStatus, Service, ServiceState
```

> 本 Task **只加** `dataclass` 与 `from pydantic import BaseModel` 两处，`asyncio` 留到 Task 6。

② `_Plan` 类型别名替换为 dataclass：

```python
@dataclass(slots=True)
class _Plan:
    """单个服务的构造计划：三类注入参数按来源分组。"""

    services: dict[str, type[Service]]  # 参数名 → 依赖服务类型
    nodes: dict[str, type[BaseModel]]  # 参数名 → 配置节点类型
    whole_settings: list[str]  # 声明整份 Settings 的参数名
```

（删掉原来的 `_Plan = tuple[dict[str, type[Service]], list[str]]`。）

③ `_build` 改为：

```python
    def _build(self) -> None:
        """校验契约 → 拓扑排序 → 按序构造注入 → 固化顺序。

        构造结果先攒在局部字典里，全部成功后才一次性提交：任一步失败时
        容器保持未装配状态，不会留下半成品实例。
        """
        settings = self._settings or get_settings()
        nodes = self._index_config_nodes(settings)
        plans = {
            service_type: self._validate_contract(service_type, nodes)
            for service_type in self._types
        }
        order = self._resolve_order()

        instances: dict[type[Service], Service] = {}
        for service_type in order:
            plan = plans[service_type]
            kwargs: dict[str, object] = {}
            for param_name in plan.whole_settings:
                kwargs[param_name] = settings
            for param_name, node_type in plan.nodes.items():
                kwargs[param_name] = nodes[node_type]
            for param_name, dependency_type in plan.services.items():
                kwargs[param_name] = instances[dependency_type]
            # 构造器参数是动态拼出来的，签名无法静态校验，故这里显式收敛类型
            factory = cast("Callable[..., Service]", service_type)
            instances[service_type] = factory(**kwargs)

        self._instances = instances
        self._order = order
        self._built = True
```

④ 新增 `_index_config_nodes`（放在 `_build` 之后、`_validate_contract` 之前）：

```python
    @staticmethod
    def _index_config_nodes(settings: Settings) -> dict[type[BaseModel], BaseModel]:
        """按类型索引 Settings 的顶层配置节点，供构造器注解命中。

        只认顶层字段、不递归：服务要拿的配置写在哪一眼可见，也避免"某个嵌套
        深处的同类节点被静默注入"。
        """
        nodes: dict[type[BaseModel], BaseModel] = {}
        for field_name, field_info in type(settings).model_fields.items():
            # 显式收成 object：model_fields 是运行期字典，取值是 Any，
            # 直接用会让 Any 渗进后面的 isinstance 与 cast。
            annotation: object = field_info.annotation
            if not (isinstance(annotation, type) and issubclass(annotation, BaseModel)):
                continue
            if annotation is Settings:  # 整份配置由构造器直接声明 Settings 承接
                continue
            if annotation in nodes:
                raise ServiceContractError(
                    f"配置节点类型 {annotation.__name__} 在 Settings 中出现多次，"
                    f"容器无法确定注入哪个"
                )
            nodes[annotation] = cast("BaseModel", getattr(settings, field_name))
        return nodes
```

⑤ `_validate_contract` 改为带 `nodes` 参数、返回 `_Plan`；签名与函数体按下述替换（`dependencies` 对账部分逻辑不变，只把取数据的来源换成 `plan.services`）：

```python
    def _validate_contract(
        self, service_type: type[Service], nodes: dict[type[BaseModel], BaseModel]
    ) -> _Plan:
        """对账 dependencies 声明与 __init__ 签名，返回该服务的构造计划。

        用 get_type_hints 而非裸注解：本仓库满屏 from __future__ import
        annotations，注解都是字符串，必须显式解析。
        """
        hints = get_type_hints(service_type.__init__)
        prefix = f"{service_type.__name__}.__init__"

        plan = _Plan(services={}, nodes={}, whole_settings=[])

        for param_name, param in inspect.signature(service_type.__init__).parameters.items():
            if param_name == "self":
                continue
            if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                raise ServiceContractError(f"{prefix} 含可变参数 {param_name}，容器无法注入")

            # 显式收成 object：get_type_hints 取值是 Any，直接使用会让 Any
            # 扩散到后面的 isinstance / repr，触发类型检查器的未知类型告警。
            annotation: object = hints.get(param_name)
            if annotation is None:
                raise ServiceContractError(
                    f"{prefix} 的参数 {param_name} 缺少类型注解，容器无法注入"
                )
            if annotation is Settings:
                plan.whole_settings.append(param_name)
                continue
            if isinstance(annotation, type) and issubclass(annotation, Service):
                plan.services[param_name] = annotation
                continue
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                if annotation not in nodes:
                    raise ServiceContractError(
                        f"{prefix} 的参数 {param_name} 注解为 {annotation.__name__}，"
                        f"它不是 Settings 的顶层字段，请在 core/config/settings.py 中注册"
                    )
                plan.nodes[param_name] = annotation
                continue

            shown = annotation.__name__ if isinstance(annotation, type) else repr(annotation)
            raise ServiceContractError(
                f"{prefix} 的参数 {param_name} 注解为 {shown}，容器只支持 Service 与配置节点"
            )

        declared = set(service_type.dependencies)
        injected = set(plan.services.values())

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

        return plan
```

> 注意：`容器只支持 Service 与配置节点` 这句必须保留前缀"容器只支持"，既有测试 `test_注解类型无法注入` 用的是 `match="容器只支持"`。

**Step 4: 运行测试确认通过**

```bash
uv run pytest tests/test_service_injection.py -v
uv run pytest | tail -3
```

Expected: 全绿，110 → 113 项。既有 `SettingsAwareService`（整份 `Settings` 注入）用例必须仍然通过。

**Step 5: 收尾检查与提交**

```bash
uv run ruff check . && uv run ruff format --check .
uvx basedpyright core tests
git add core/service/manager.py tests/test_service_injection.py
git commit -m "feat(service): 容器按类型注入配置节点"
```

---

## Task 4: `ClockService` 改为配置驱动

**Files:**

- Modify: `core/service/clock_service.py`
- Modify: `tests/test_service_injection.py`（同步改直接构造处）
- Test: `tests/test_service_injection.py`

**背景：** `ClockService.__init__` 新增必填参数属于**破坏性变更**，仓库内唯一直接构造它的地方是 `test_健康检查用展示名` 的 `ClockConsumerService(ClockService())`，本 Task 一并改掉。

**Step 1: 写失败的测试**

在 `tests/test_service_injection.py` 的 import 区追加：

```python
from datetime import timedelta
```

并把 `from core.service.manager import (...)` 的列表补上 `ServiceStartError`；`from core.service.base import Service` 改为 `from core.service.base import Service, ServiceState`。

在文件末尾追加：

```python
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
```

同时把既有的 `test_健康检查用展示名` 里那行：

```python
    service = ClockConsumerService(ClockService())
```

改为：

```python
    service = ClockConsumerService(ClockService(ClockSettings()))
```

**Step 2: 运行测试确认失败**

```bash
uv run pytest tests/test_service_injection.py -k "时钟 or 时区" -v
```

Expected: FAIL，`TypeError: ClockService.__init__() takes 1 positional argument but 2 were given` / `ClockService(ClockSettings())` 报参数个数不对。

**Step 3: 实现**

`core/service/clock_service.py` 整体替换为：

```python
"""时钟服务：为其他服务提供可替换的时间来源。

时区由 ClockSettings 决定，默认 UTC。
now() 刻意保持**同步**：它不涉及 I/O，服务方法不必一律 async。
这样依赖它的服务无需为了取时间把纯计算逻辑改成异步。
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar, override
from zoneinfo import ZoneInfo

from core.config import ClockSettings
from core.service.base import Service, ServiceState


class ClockService(Service):
    """约定：时间一律从这里取，时区由配置决定，测试可注入固定时间的替身。"""

    name: ClassVar[str] = "clock"

    def __init__(self, config: ClockSettings) -> None:
        super().__init__()
        self._config: ClockSettings = config

    @override
    async def start(self) -> None:
        """校验时区可解析：非法时区在启动期 fail fast，而不是等第一次取值。

        只校验不缓存：zoneinfo 内部有缓存，now() 每次取时区开销可忽略，
        这样 now() 也就不依赖 start() 是否跑过。
        """
        if self.state is ServiceState.RUNNING:
            return
        _ = ZoneInfo(self._config.tz)

    def now(self) -> datetime:
        """当前时间（配置时区）。"""
        return datetime.now(ZoneInfo(self._config.tz))
```

> `_ = ZoneInfo(...)` 的 `_ =` 不能省：basedpyright 的 `reportUnusedCallResult` 会拦裸调用。

**Step 4: 运行测试确认通过**

```bash
uv run pytest tests/test_service_injection.py -v
uv run pytest | tail -3
```

Expected: 全绿，113 → 117 项。`test_默认注册表装配后可启动并健康` 应仍通过（容器会把 `get_settings()` 里的 `clock` 节点注入）。

**Step 5: 冒烟**

```bash
uv run python -m core.main &
sleep 2
kill -INT %1
wait
```

Expected: 打印 `clock` 健康且 `running`（`服务健康 服务=clock 状态=running`），`kill -INT` 后打印 `全部服务已停止` 与 `应用已关闭`。

**Step 6: 收尾检查与提交**

```bash
uv run ruff check . && uv run ruff format --check .
uvx basedpyright core tests
git add core/service/clock_service.py tests/test_service_injection.py
git commit -m "feat(service): 时钟服务改为配置驱动时区"
```

---

## Task 5: `_resolve_order` 换成 `_resolve_levels`

**Files:**

- Modify: `core/service/manager.py`
- Test: `tests/test_service_manager.py`

**背景：** 本 Task 只改分层数据结构，**不动执行模型**（`start_all` 仍串行按扁平 `_order` 跑），所以全量测试必须原样全绿 —— 这是"分层没改变既有语义"的证明。

**Step 1: 写失败的测试**

在 `tests/test_service_manager.py` 的 `RecordService` 子类区追加：

```python
class MetricsService(RecordService):
    """与 DbService 同层（都无依赖）的服务。"""

    name: ClassVar[str] = "metrics"
```

在文件末尾追加：

```python
def test_按依赖分层且同层按注册顺序() -> None:
    mgr = ServiceManager()
    _ = mgr.register(MetricsService)  # 无依赖 → 第 0 层
    _ = mgr.register(DbService)  # 无依赖 → 第 0 层
    _ = mgr.register(CacheService)  # 依赖 DbService → 第 1 层
    _ = mgr.services  # 触发装配

    # 白盒：分层结构没有公开出口，而它是并发启动的唯一依据，必须直接断言；
    # 否则分层的正确性只能靠"跑起来像不像并发"间接猜。
    levels = mgr._levels  # pyright: ignore[reportPrivateUsage]

    assert levels == [[MetricsService, DbService], [CacheService]]


def test_扁平顺序按层分组() -> None:
    """同层服务连续排列，让 services / names 的顺序与"启动即分批"对应。"""
    mgr = ServiceManager()
    _ = mgr.register(DbService)
    _ = mgr.register(MetricsService)
    _ = mgr.register(CacheService)
    _ = mgr.register(ApiService)

    assert mgr.names == ["db", "metrics", "cache", "api"]


async def test_分层后关闭顺序仍严格逆序() -> None:
    mgr = ServiceManager()
    _ = mgr.register(DbService)
    _ = mgr.register(MetricsService)
    _ = mgr.register(CacheService)
    _ = mgr.register(ApiService)

    await mgr.start_all()
    await mgr.stop_all()

    stops = [e for e in events if e.startswith("stop:")]
    assert stops == ["stop:api", "stop:cache", "stop:metrics", "stop:db"]
```

**Step 2: 运行测试确认失败**

```bash
uv run pytest tests/test_service_manager.py -k "分层 or 扁平顺序" -v
```

Expected: FAIL，`AttributeError: 'ServiceManager' object has no attribute '_levels'`；`test_扁平顺序按层分组` 报 `assert ['db', 'cache', 'api', 'metrics'] == [...]`（旧 DFS 顺序把 metrics 排到了最后）。

**Step 3: 实现**

`core/service/manager.py`：

① `__init__` 里在 `self._order` 声明之后插入：

```python
        #: 装配后按依赖分好的层，索引越小越靠前；启动时逐层推进
        self._levels: list[list[type[Service]]] = []
```

② `_build` 里的排序与固化改为：

```python
        levels = self._resolve_levels()

        instances: dict[type[Service], Service] = {}
        for level in levels:
            for service_type in level:
                # ……构造逻辑与 Task 3 相同，整段不动……
```

并把收尾的固化改为：

```python
        self._instances = instances
        self._levels = levels
        # 扁平序按层展开：仍是合法拓扑序，reversed 仍是合法逆拓扑序，
        # 因此 services / names 的展示顺序与 stop_all 的关闭顺序都不用改
        self._order = [service_type for level in levels for service_type in level]
        self._built = True
```

③ 把 `_resolve_order()` 整体替换为 `_resolve_levels()`：

```python
    def _resolve_levels(self) -> list[list[type[Service]]]:
        """按 dependencies 分层：同层之间必无依赖，可并发启动。

        层号 = max(依赖层号) + 1；同层内按注册顺序排列，保证结果确定。
        顺带完成环检测与未注册依赖检测。
        """
        depth: dict[type[Service], int] = {}
        visiting: set[type[Service]] = set()

        def visit(service_type: type[Service]) -> int:
            if service_type in depth:
                return depth[service_type]
            if service_type in visiting:
                raise CircularDependencyError(service_type.__name__)
            if service_type not in self._types:
                raise MissingDependencyError(service_type.__name__)

            visiting.add(service_type)
            level = 0
            for dependency in service_type.dependencies:
                level = max(level, visit(dependency) + 1)
            visiting.discard(service_type)
            depth[service_type] = level
            return level

        for service_type in self._types:
            _ = visit(service_type)

        levels: list[list[type[Service]]] = [
            [] for _ in range(max(depth.values(), default=-1) + 1)
        ]
        for service_type in self._types:  # 注册顺序 → 同层内顺序确定
            levels[depth[service_type]].append(service_type)
        return levels
```

**Step 4: 运行测试确认通过**

```bash
uv run pytest tests/test_service_manager.py -v
uv run pytest | tail -3
```

Expected: 全绿，117 → 120 项。既有 `test_按依赖顺序启动` / `test_逆序关闭` / `test_健康检查聚合` 必须全部照旧通过（db / cache / api 分属 0 / 1 / 2 层，扁平序与旧 DFS 序一致）。

**Step 5: 收尾检查与提交**

```bash
uv run ruff check . && uv run ruff format --check .
uvx basedpyright core tests
git add core/service/manager.py tests/test_service_manager.py
git commit -m "refactor(service): 依赖排序改为分层并固化层结构"
```

---

## Task 6: 同层并发启动

**Files:**

- Modify: `core/service/manager.py`
- Create: `tests/test_service_lifecycle.py`

**并发判定思路：** 不用 `sleep` 断言耗时（会抖动），改用**事件互等**：同层两个服务在 `start()` 里各自先置位"我已进入"，再等对方的"已进入"事件。若容器是串行的，第一个服务的等待必然超时（1 秒）→ 启动失败 → 测试红。这样断言是确定性的，失败时的代价也只有 1 秒。

**Step 1: 写失败的测试**

新建 `tests/test_service_lifecycle.py`：

```python
"""服务生命周期测试：分层并发与启停超时。"""

from __future__ import annotations

import asyncio
from typing import ClassVar, override

import pytest

from core.service.base import Service
from core.service.manager import ServiceManager

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
        await asyncio.wait_for(entered.setdefault("gate-b", asyncio.Event()).wait(), timeout=1.0)


class GateB(Service):
    """同层探针：置位自己后等对端。"""

    name: ClassVar[str] = "gate-b"

    @override
    async def start(self) -> None:
        entered.setdefault("gate-b", asyncio.Event()).set()
        await asyncio.wait_for(entered.setdefault("gate-a", asyncio.Event()).wait(), timeout=1.0)


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
```

其中 `FailingGateA` / `FailingGateB` 两个测试服务定义在文件里（紧接 `OuterService` 之后）：

```python
class FailingGateA(Service):
    """同层失败探针（先注册）。"""

    name: ClassVar[str] = "fail-a"

    @override
    async def start(self) -> None:
        entered.setdefault("fail-a", asyncio.Event()).set()
        await asyncio.wait_for(
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
        await asyncio.wait_for(
            entered.setdefault("fail-a", asyncio.Event()).wait(), timeout=1.0
        )
        msg = "乙炸了"
        raise RuntimeError(msg)
```

并把 import 区补上：

```python
from core.service.manager import ServiceManager, ServiceStartError
```

**Step 2: 运行测试确认失败**

```bash
uv run pytest tests/test_service_lifecycle.py -v
```

Expected: `test_同层服务并发启动` FAIL（串行 → `asyncio.wait_for` 超时 → `ServiceStartError`）；`test_同层某个服务失败时回滚且报错取先注册者` FAIL（当前串行下先跑 `fail-a`，它自己就会因等不到 `fail-b` 而超时抛错，`service_name` 恰好也是 `fail-a`，**这个用例要等 Task 6 实现后才真正验证并发语义**——所以本 Task 里它可能"意外通过"，属正常，Task 7 会用日志字段把它锁死）。`test_跨层严格串行` 应直接通过。

**Step 3: 实现**

`core/service/manager.py`：

① import 区加 `import asyncio`（放在 `import inspect` 之前）。

② `start_all` 整体替换为：

```python
    async def start_all(self) -> None:
        """按依赖分层启动：同层并发、层间串行；任一失败则回滚已启动的服务。"""
        self._ensure_built()
        started: list[Service] = []

        for level in self._levels:
            pending = [
                service_type
                for service_type in level
                if self._instances[service_type].state is not ServiceState.RUNNING
            ]
            if not pending:
                continue

            outcomes = await asyncio.gather(
                *(self._start_one(self._instances[service_type]) for service_type in pending),
                return_exceptions=True,
            )

            failures: list[BaseException] = []
            for outcome in outcomes:  # gather 保序 → 与 pending 同序
                if isinstance(outcome, ServiceStartError):
                    failures.append(outcome)
                elif isinstance(outcome, BaseException):
                    raise outcome  # 只可能是取消等非 Exception，直接冒泡
                else:
                    started.append(outcome)

            if failures:
                await self._rollback(started)
                raise failures[0]  # 注册顺序最靠前的失败者，报错稳定

        log.info("全部服务启动完成", 服务数=len(started))
```

③ 新增 `_start_one`（放在 `start_all` 与 `stop_all` 之间）：

```python
    async def _start_one(self, service: Service) -> Service:
        """启动单个服务；失败收敛成 ServiceStartError，成功返回实例供 gather 收集。

        不取消兄弟任务：同层任一失败也要等整层跑完，取消会把服务停在
        半启动状态，回滚更难判断；超时兜底在 Task 7 加上。
        返回实例而不是 None，是为了让 asyncio.gather 的结果能直接分成
        「实例」与「异常」两类。
        """
        service.state = ServiceState.STARTING
        try:
            await service.start()
        except Exception as exc:
            service.state = ServiceState.FAILED
            service.log_error("服务启动失败", exc)
            raise ServiceStartError(service.label, exc) from exc

        service.state = ServiceState.RUNNING
        log.info("服务已启动", face=faces.START, 服务=service.label)
        return service
```

> 注意：原 `start_all` 里那行 `service = self._instances[service_type]` 与 `已启动=len(started)` 字段一并消失（后者是旧串行实现的产物）。

**Step 4: 运行测试确认通过**

```bash
uv run pytest tests/test_service_lifecycle.py -v
uv run pytest | tail -3
```

Expected: 全绿，120 → 123 项。既有 `test_启动失败时回滚已启动服务`（`excinfo.value.service_name == "api"`）必须仍然通过。

**Step 5: 收尾检查与提交**

```bash
uv run ruff check . && uv run ruff format --check .
uvx basedpyright core tests
git add core/service/manager.py tests/test_service_lifecycle.py
git commit -m "feat(service): 同层服务并发启动"
```

---

## Task 7: 启停超时保护

**Files:**

- Modify: `core/service/manager.py`
- Modify: `tests/test_service_lifecycle.py`

**Step 1: 写失败的测试**

在 `tests/test_service_lifecycle.py` 的 import 区追加：

```python
from core.config import ServiceSettings, Settings
from core.service.base import Service, ServiceState, UNSET, Unset
```

（`from core.service.base import Service` 那行替换为上面这行。）

在文件末尾追加：

```python
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
    start_timeout: ClassVar[float | None | Unset] = None

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
```

> 最后一条用例只验证不抛错 + 状态；日志字段的断言放到 Task 8 的冒烟里人工看一眼即可，避免为读日志再引入 sink 夹具。

**Step 2: 运行测试确认失败**

```bash
uv run pytest tests/test_service_lifecycle.py -k "超时" -v
```

Expected: `test_启动超时判定为失败` FAIL（没有超时机制 → 服务真的睡 10 秒后才成功，`ServiceStartError` 不会抛出；不写超时机制的实现里这条用例会跑满 10 秒，注意观察耗时）。`test_服务级_none_覆盖全局超时` PASS（无关超时机制）。`test_关闭超时不阻断其他服务` FAIL 或跑满 10 秒。

**Step 3: 实现**

`core/service/manager.py`：

① import 区改为：

```python
from collections.abc import AsyncGenerator, Callable, Coroutine, Sequence
```

并加上：

```python
from core.config import ServiceSettings, Settings, get_settings
from core.service.base import UNSET, HealthStatus, Service, ServiceState, Unset
```

> `UNSET`/`Unset` 只在 `_resolve_timeout` 的签名与 `isinstance` 里用到。ruff 若报 `F401` 未使用，说明 `_resolve_timeout` 还没写全，检查第 ③ 步。

② `__init__` 里在 `self._settings` 声明之后插入：

```python
        #: 生命周期超时配置。装配前是默认值占位：启停路径必然经过 _ensure_built()，
        #: 占位值不会被读到。反过来不能在 __init__ 里调 get_settings()——
        #: 那会让模块级 default_manager = build_manager() 在 import 期读配置文件。
        self._service_settings: ServiceSettings = ServiceSettings()
```

③ `_build` 末尾固化时补一行：

```python
        self._service_settings = settings.service
```

（放在 `self._instances = instances` 之前均可，只要在 `self._built = True` 之前。）

④ 新增模块级两个函数（放在 `_ensure_service_subclass` 之后、`ServiceManager` 之前）：

```python
def _resolve_timeout(override: float | None | Unset, default: float | None) -> float | None:
    """三态解析：UNSET 跟随全局默认，None 表示不限制，数字即该值。"""
    return default if isinstance(override, Unset) else override


async def _call_with_timeout(
    action: Callable[[], Coroutine[object, object, None]], timeout: float | None
) -> None:
    """按超时调用服务钩子。异常不在这里吞，由调用方决定语义。"""
    if timeout is None:
        await action()
        return
    async with asyncio.timeout(timeout):
        await action()
```

⑤ `_start_one` 改为：

```python
    async def _start_one(self, service: Service) -> Service:
        """启动单个服务；超时与失败都收敛成 ServiceStartError。"""
        service.state = ServiceState.STARTING
        timeout = _resolve_timeout(service.start_timeout, self._service_settings.start_timeout)
        try:
            await _call_with_timeout(service.start, timeout)
        except TimeoutError as exc:
            service.state = ServiceState.FAILED
            # 裸 TimeoutError 的 str() 是空的，会让 ServiceStartError 的消息
            # 变成"服务 X 启动失败：TimeoutError: "，这里换成带说明的 cause。
            cause = TimeoutError(f"启动超时，超过 {timeout}s")
            service.log_error("服务启动超时", cause, 超时秒数=timeout)
            raise ServiceStartError(service.label, cause) from exc
        except Exception as exc:
            service.state = ServiceState.FAILED
            service.log_error("服务启动失败", exc)
            raise ServiceStartError(service.label, exc) from exc

        service.state = ServiceState.RUNNING
        log.info("服务已启动", face=faces.START, 服务=service.label)
        return service
```

⑥ `start_all` 的循环体改为记录整层耗时并打层日志：

```python
        for index, level in enumerate(self._levels):
            pending = [...同 Task 6...]
            if not pending:
                continue

            begin = time.perf_counter()
            outcomes = await asyncio.gather(...)
            elapsed_ms = round((time.perf_counter() - begin) * 1000, 1)

            failures: list[BaseException] = []
            for outcome in outcomes:
                ...
            if failures:
                await self._rollback(started)
                raise failures[0]

            log.info(
                "同层服务启动完成", 层=index, 服务数=len(pending), 耗时=f"{elapsed_ms}ms"
            )
```

> 单个服务的 `耗时=` 字段随之取消（并发下"这个服务的耗时"没有意义）；状态变更仍由 `_start_one` 里的 `服务已启动` 记录。

⑦ `stop_all` 整体替换为：

```python
    async def stop_all(self) -> None:
        """按启动的逆序**串行**关闭所有服务，单个超时或出错都不阻断其余服务。

        顺序在装配期就已固定为 reversed(self._order)，因此这里不需要记录
        "本次启动了哪些"，重复调用天然幂等。
        未装配过的容器没有实例可停，直接返回，不为"停止"去构造服务。
        """
        if not self._built:
            return
        for service_type in reversed(self._order):
            service = self._instances[service_type]
            if service.state in (ServiceState.STOPPED, ServiceState.CREATED):
                continue

            service.state = ServiceState.STOPPING
            timeout = _resolve_timeout(service.stop_timeout, self._service_settings.stop_timeout)
            try:
                await _call_with_timeout(service.stop, timeout)
            except TimeoutError as exc:
                service.state = ServiceState.FAILED
                service.log_error("服务关闭超时", exc, 超时秒数=timeout)
                continue
            except Exception as exc:
                # 关闭阶段不阻断其他服务，仅记录
                service.state = ServiceState.FAILED
                service.log_error("服务关闭异常", exc)
                continue

            service.state = ServiceState.STOPPED
            log.info("服务已停止", face=faces.BYE, 服务=service.label)

        log.info("全部服务已停止")
```

⑧ `_rollback` 整体替换为：

```python
    async def _rollback(self, started: Sequence[Service]) -> None:
        """逆序回滚本次已启动的服务；同样套 stop 超时，但不阻断其余回滚。"""
        for service in reversed(started):
            timeout = _resolve_timeout(service.stop_timeout, self._service_settings.stop_timeout)
            try:
                await _call_with_timeout(service.stop, timeout)
            except TimeoutError as exc:
                service.state = ServiceState.FAILED
                service.log_error("回滚时服务关闭超时", exc, 超时秒数=timeout)
            except Exception as exc:
                service.state = ServiceState.FAILED
                service.log_error("回滚时服务关闭异常", exc)
            else:
                service.state = ServiceState.STOPPED
```

**Step 4: 运行测试确认通过**

```bash
uv run pytest tests/test_service_lifecycle.py -v
uv run pytest | tail -3
```

Expected: 全绿，123 → 129 项。注意整体耗时应仍在 1 秒级（超时都是 0.05 秒，`sleep(10)` 会被取消掉）。

**Step 5: 收尾检查与提交**

```bash
uv run ruff check . && uv run ruff format --check .
uvx basedpyright core tests
git add core/service/manager.py tests/test_service_lifecycle.py
git commit -m "feat(service): 服务启停加超时保护"
```

---

## Task 8: README 更新与全量验收

**Files:**

- Modify: `README.md`

**Step 1: 配置项表格补三行**

README 的 `### 配置项` 表格末尾追加：

```markdown
| `service.start_timeout` | `30` | 单个服务 `start()` 的超时秒数；写 `null` 表示不限制 |
| `service.stop_timeout` | `30` | 单个服务 `stop()` 的超时秒数；同上 |
| `clock.tz` | `UTC` | 时钟服务的时区，如 `Asia/Shanghai` |
```

**Step 2: 服务章节的示例与约定表**

把 `## 如何新增一个服务` 里第 1 步的代码示例替换为：

```python
from typing import ClassVar

from core.service.base import Service
from core.config import CacheSettings


class CacheService(Service):
    # 依赖的服务类型；容器据此排序并在构造时注入
    dependencies: ClassVar[tuple[type[Service], ...]] = (DbService,)

    def __init__(self, db: DbService, config: CacheSettings) -> None:
        super().__init__()  # 必须调用：负责设置 state 与 self.log
        self._db = db
        self._config = config

    async def start(self) -> None: ...  # 建连接
    async def stop(self) -> None: ...   # 关连接
```

并把约定表末尾那行：

```markdown
| 配置注入 | 需要配置时在构造器声明 `settings: Settings`，容器会注入应用持有的配置 |
```

替换为：

```markdown
| 配置节点注入 | 需要局部配置时，把配置类定义在 `core/config/settings.py` 并挂成 `Settings` 的**顶层字段**，构造器声明该类型即可（如 `config: CacheSettings`），容器按类型注入 |
| 整份配置注入 | 需要全局视野时在构造器声明 `settings: Settings`，容器注入应用持有的那份实例 |
| 超时覆盖 | 默认走 `service.start_timeout` / `service.stop_timeout`；单独调整时写 `start_timeout: ClassVar[float \| None \| Unset] = 300.0`，`None` 表示该服务不限制，不写即跟随全局 |
```

**Step 3: 启停语义段落改写**

把 `启停语义：...` 那一整段替换为：

```markdown
启停语义：`start` / `stop` 需幂等（重复调用不应报错）。启动按依赖**分层**：同层并发、层间串行；单个服务超时或抛错都判该服务失败，并逆序回滚本次已启动的服务，随后抛出 `ServiceStartError`。关闭按启动的逆序**串行**执行，单个服务超时或出错只记日志（状态置 `FAILED`），不影响其余服务停下。

超时靠 `asyncio.timeout` 的取消实现，因此服务的 `start` / `stop` **不要吞掉 `CancelledError`**（写 `except Exception` 是安全的，它不会捕获 `CancelledError`）。
```

**Step 4: 全量验收**

```bash
uv run pytest | tail -3
uv run ruff check . && uv run ruff format --check .
uvx basedpyright core tests
```

Expected: 129 项全绿；ruff 无输出；basedpyright `0 errors, 0 warnings, 0 notes`。

**Step 5: 冒烟（人眼看日志）**

```bash
uv run python -m core.main &
sleep 2
kill -INT %1
wait
```

Expected:

- `服务健康 服务=clock 状态=running`
- `[clock] 服务已启动` 与 `同层服务启动完成 层=0 服务数=1 耗时=...ms`
- `kill -INT` 后 `[clock] 服务已停止` 与 `全部服务已停止`、`应用已关闭`
- 日志里能同时看到 `服务: clock` 字段与 `[clock]` 消息前缀

再验一次配置覆盖：把 `data/config/app.yaml` 的 `clock.tz` 临时改成 `Asia/Shanghai`，跑一次冒烟，确认没有报错（时区合法性由 Task 4 的测试覆盖），改回 `UTC`。

**Step 6: 提交**

```bash
git add README.md
git commit -m "docs: README 补充配置节点注入与生命周期约定"
git --no-pager log --oneline -9
```

---

## 完成标准

- `uv run pytest` 全绿：基线 104 → 129 项，无回归。
- `uv run ruff check .` 与 `uv run ruff format --check .` 无输出。
- `uvx basedpyright core tests` 报 `0 errors, 0 warnings, 0 notes`。
- `uv run python -m core.main` 冒烟通过（健康、启动日志、SIGINT 优雅关闭）。
- 依赖图仍可 grep：`dependencies: ClassVar[tuple[type[Service], ...]]` 在类顶端一眼可见。
- README 的配置项表格与约定表覆盖新增能力。

## 交付后

按 superpowers:finishing-a-development-branch 处理：`feature/service-lifecycle` 以 `--no-ff` 并入 `main`，合并后在主工作区**重跑一遍全量校验**（含 `uvx basedpyright core tests`），再清理隔离工作区（删 worktree 前先清掉其中的 `logs/` 等未跟踪产物）。
