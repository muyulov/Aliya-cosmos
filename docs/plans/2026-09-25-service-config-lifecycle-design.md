# service 层配置注入与生命周期健壮性 设计

日期：2026-09-25
状态：设计已确认，待实施
基线：`main @ f3102eb`（注意 `e341b0a` 已移除 api 层与 `ItemService`，本设计不再依赖它们）

## 一、目标与范围

给 `core/service` 补两块能力：

- **A 服务级配置注入**：服务可以声明自己的配置节点（如 `ClockSettings`），由容器按类型注入，不必每次拿整份 `Settings` 再自己往里挖。
- **B 生命周期健壮性**：`start` / `stop` 加超时保护，启动改为**按依赖分层、同层并发**，避免一个卡死的服务拖垮整条启动链路。

非目标（本次不做）：服务级**并发停止**（停服务并发风险高于启动，收益低）、启动失败重试（启动失败多是配置 / 契约错误，重试会掩盖问题且与 fail fast 回滚语义相冲）、配置热重载、跨服务的配置校验。

## 二、决策记录

| # | 议题 | 结论 | 被否决的方案与原因 |
| --- | --- | --- | --- |
| 1 | 配置节点放哪 | 集中在 `core/config/settings.py`，作为 `Settings` 顶层字段，容器按构造器注解类型查找注入 | 服务自持 `BaseSettings` + 自定 `env_prefix`（env 前缀散落各处，且 `get_settings()` 不再覆盖服务配置，配置来源分叉）；容器新开 overrides 通道（多一条旁路） |
| 2 | 是否用 `ClassVar` 显式声明配置类型 | 不声明，**注解即声明** | 加 `config: ClassVar[type[BaseModel]]`（与注解是重复信息，还要再写一层对账；`Settings` 的注入本来就是注解推断，风格会分裂） |
| 3 | 配置索引是否递归 | 只认 `Settings` **顶层**字段 | 递归嵌套（某个深处的同类节点被静默注入，排查困难） |
| 4 | 类型歧义怎么处理 | 同类型出现多次 → `ServiceContractError` fail fast | 取第一个（静默行为，出错点离病因太远） |
| 5 | 超时默认值放哪 | 新增 `ServiceSettings` 节点（`Settings.service`） | 塞进 `AppSettings`（生命周期关注点混进 app 节点）；模块常量（运维无法调） |
| 6 | 服务级超时覆盖怎么表达 | `ClassVar` **三态** + `UNSET` 哨兵：`UNSET` 跟随全局 / `None` 不限制 / 数字即该值 | 只用 `None` 表"不限时"（无法表达"未覆盖"）；只用默认值（无法表达"不限时"） |
| 7 | B 做到哪一步 | 超时 + 同层并发启动 | 再加失败重试（掩盖配置错误，与回滚语义冲突）；并发停止（风险高收益低） |
| 8 | 启动失败时同层其余服务怎么处理 | 等**整层跑完**再判成败，不取消已在跑的启动 | 立即取消兄弟任务（会把服务留在半启动状态，回滚更难判断） |
| 9 | 失败时抛哪个异常 | 取注册顺序**最靠前**的失败者，保证多次运行报错稳定 | 抛"第一个完成的失败者"（并发下不确定，测试与排查都难受） |
| 10 | A 的演示载体 | `ClockSettings(tz)` 挂在 `ClockService` | 只做能力不加消费者（能力无真实消费者，README 只能伪代码演示）；另建示例服务（与刚"移除示例服务"的方向相反） |

## 三、A：服务级配置注入

### 3.1 配置层新增两个节点

`core/config/settings.py` 新增两个纯 `pydantic.BaseModel`，与 `AppSettings` / `LogSettings` 同级：

```python
class ServiceSettings(BaseModel):
    """服务生命周期配置。"""

    start_timeout: float | None = 30.0  # 秒；None 表示不限制
    stop_timeout: float | None = 30.0


class ClockSettings(BaseModel):
    """时钟服务配置。"""

    tz: str = "UTC"
```

`Settings` 增加两个顶层字段：

```python
    service: ServiceSettings = ServiceSettings()
    clock: ClockSettings = ClockSettings()
```

覆盖方式沿用 2026-09-25 的 YAML 骨架约定，`data/config/app.yaml` 补两段（值等于默认，保持"骨架即全貌"）：

```yaml
service:
  start_timeout: 30
  stop_timeout: 30

clock:
  tz: UTC          # 部署侧要覆盖时写成占位符：${CLOCK_TZ:UTC}
```

`core/config/__init__.py` 的导出与 `__all__` 补 `ClockSettings`、`ServiceSettings`。

### 3.2 容器侧的配置索引

`ServiceManager._build()` 先建一张 `{节点类型: 节点实例}` 索引，再校验契约：

```python
    @staticmethod
    def _index_config_nodes(settings: Settings) -> dict[type[BaseModel], BaseModel]:
        """按类型索引 Settings 的顶层配置节点，供构造器注解命中。

        只认顶层字段、不递归：服务要拿的配置写在哪一眼可见，也避免"某个嵌套
        深处的同类节点被静默注入"。
        """
        nodes: dict[type[BaseModel], BaseModel] = {}
        for field_name, field_info in type(settings).model_fields.items():
            annotation = field_info.annotation
            if not (isinstance(annotation, type) and issubclass(annotation, BaseModel)):
                continue
            if annotation is Settings:  # 整份配置由构造器直接声明 Settings 承接
                continue
            if annotation in nodes:
                raise ServiceContractError(
                    f"配置节点类型 {annotation.__name__} 在 Settings 中出现多次，"
                    f"容器无法确定注入哪个"
                )
            # model_fields 是运行期字典，静态类型已丢失，用 cast 收敛避免 Any 扩散
            nodes[annotation] = cast("BaseModel", getattr(settings, field_name))
        return nodes
```

### 3.3 契约校验与构造计划

`_Plan` 从二元组升级为 dataclass，三类注入参数按来源分组，`_build` 里不再靠位置猜含义：

```python
@dataclass(slots=True)
class _Plan:
    """单个服务的构造计划。"""

    services: dict[str, type[Service]]      # 参数名 → 依赖服务类型
    nodes: dict[str, type[BaseModel]]       # 参数名 → 配置节点类型
    whole_settings: list[str]               # 声明整份 Settings 的参数名
```

`_validate_contract(service_type, nodes)` 的判定顺序与报错：

| 注解 | 处理 |
| --- | --- |
| `Settings` | 记进 `whole_settings` |
| `Service` 子类 | 记进 `services`，并参与 `dependencies` 对账（对账逻辑不变） |
| `BaseModel` 子类且在索引里 | 记进 `nodes` |
| `BaseModel` 子类但**不在**索引里 | `ServiceContractError`：提示"不是 `Settings` 的顶层字段，请在 `core/config/settings.py` 中注册" |
| 其余 | `ServiceContractError`：`容器只支持 Service 与配置节点`（沿用原有措辞，既有测试的 `match="容器只支持"` 不破） |

`_build` 装配循环相应改为：

```python
            for param_name in plan.whole_settings:
                kwargs[param_name] = settings
            for param_name, node_type in plan.nodes.items():
                kwargs[param_name] = nodes[node_type]
            for param_name, dependency_type in plan.services.items():
                kwargs[param_name] = instances[dependency_type]
```

### 3.4 演示载体：`ClockService`

```python
class ClockService(Service):
    """约定：时间一律从这里取，时区由配置决定，测试可注入固定时间的替身。"""

    name: ClassVar[str] = "clock"

    def __init__(self, config: ClockSettings) -> None:
        super().__init__()
        self._config: ClockSettings = config

    @override
    async def start(self) -> None:
        """校验时区可解析：非法时区在启动期 fail fast，而不是等第一次取值。"""
        if self.state is ServiceState.RUNNING:
            return
        ZoneInfo(self._config.tz)

    def now(self) -> datetime:
        """当前时间（配置时区）。"""
        return datetime.now(ZoneInfo(self._config.tz))
```

`now()` 每次按配置取时区：`zoneinfo` 内部有缓存，重复构造开销可忽略；这样 `now()` 不依赖 `start()` 是否跑过，语义与现在一致（默认 `UTC`，返回值不变）。

## 四、B：生命周期（分层并发 + 超时）

### 4.1 分层

`_resolve_order()` 换成 `_resolve_levels()`：DFS 记忆化算 `depth = max(依赖 depth) + 1`，同层内按注册顺序排列，环检测与未注册检测沿用既有异常类型。

```python
    def _resolve_levels(self) -> list[list[type[Service]]]:
        """按 dependencies 分层：同层之间必无依赖，可并发启动。"""
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

扁平顺序仍要保留（它驱动 `services` / `names` / stop 的 `reversed`）：

```python
        self._order = [service_type for level in levels for service_type in level]
```

同层之间必无依赖，因此这个扁平序仍是合法拓扑序，`reversed(_order)` 仍是合法逆拓扑序 —— 关闭顺序的既有语义与测试断言全部不变。

### 4.2 同层并发启动

```python
    async def start_all(self) -> None:
        """按依赖分层启动：同层并发、层间串行；任一失败则回滚已启动的服务。"""
        self._ensure_built()
        started: list[Service] = []

        for index, level in enumerate(self._levels):
            pending = [
                service_type
                for service_type in level
                if self._instances[service_type].state is not ServiceState.RUNNING
            ]
            if not pending:
                continue

            begin = time.perf_counter()
            outcomes = await asyncio.gather(
                *(self._start_one(self._instances[service_type]) for service_type in pending),
                return_exceptions=True,
            )
            elapsed_ms = round((time.perf_counter() - begin) * 1000, 1)

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

            log.info(
                "同层服务启动完成", 层=index, 服务数=len(pending), 耗时=f"{elapsed_ms}ms"
            )

        log.info("全部服务启动完成", 服务数=len(started))
```

要点：

- `return_exceptions=True` 让**整层跑完**再判成败。不取消兄弟任务：取消会把服务停在半启动状态，回滚更难判断；有超时兜底，等它结束代价可控。
- `asyncio.gather` 的返回顺序等于入参顺序，因此 `failures[0]` 就是注册顺序最靠前的失败者，多次运行报错一致。
- 每层失败者已在 `_start_one` 里逐个记了日志，这里只负责回滚与抛出。

### 4.3 超时三态与解析

`core/service/base.py` 新增哨兵与两个 `ClassVar`：

```python
class _Unset:
    """「未覆盖」哨兵。

    ClassVar 的默认值无法同时表达「未覆盖，跟随全局」与「显式设为 None，
    即不限制」两件事，因此引入一个只有身份意义的哨兵。
    """

    __slots__ = ()

    @override
    def __repr__(self) -> str:
        return "UNSET"


#: 服务未覆盖该项超时，沿用 ServiceSettings 里的默认值
UNSET: Final[_Unset] = _Unset()
```

```python
class Service:
    #: start 超时（秒）覆盖；UNSET 跟随全局，None 不限制，数字即该值
    start_timeout: ClassVar[float | None | _Unset] = UNSET

    #: stop 超时（秒）覆盖；语义同 start_timeout
    stop_timeout: ClassVar[float | None | _Unset] = UNSET
```

解析收在一个模块级函数里（`manager.py`，私有），`isinstance` 收窄后类型干净：

```python
def _resolve_timeout(override: float | None | _Unset, default: float | None) -> float | None:
    """三态解析：UNSET 跟随全局默认，None 表示不限制，数字即该值。"""
    return default if isinstance(override, _Unset) else override
```

超时靠 `asyncio.timeout`（3.11+，项目要求 3.12）实现，抽出两个对称的小函数给启动 / 关闭两处复用：

```python
async def _call_with_timeout(action: Callable[[], Coroutine[object, object, None]],
                              timeout: float | None) -> None:
    """按超时调用服务钩子。异常不在这里吞，由调用方决定语义。"""
    if timeout is None:
        await action()
        return
    async with asyncio.timeout(timeout):
        await action()
```

调用处传 `service.start` / `service.stop` 这类无参协程函数即可。

### 4.4 启动路径

```python
    async def _start_one(self, service: Service) -> Service:
        """启动单个服务，失败一律收敛成 ServiceStartError，返回实例供 gather 收集。"""
        service.state = ServiceState.STARTING
        timeout = _resolve_timeout(service.start_timeout, self._service_settings.start_timeout)
        try:
            await _call_with_timeout(service.start, timeout)
        except TimeoutError as exc:
            service.state = ServiceState.FAILED
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

超时把 cause 换成带说明的 `TimeoutError("启动超时，超过 30s")`，是因为裸 `TimeoutError` 的 `str()` 为空，会让 `ServiceStartError` 的消息变成 `服务 api 启动失败：TimeoutError: `。`service_name` 仍保持纯 `label`（既有测试断言 `== "api"`）。

单个服务的耗时日志随并发合并进"同层服务启动完成"的整层耗时，避免并发时多行日志互相插队；`服务已启动` 保留一条状态变更记录。

### 4.5 关闭路径

`stop_all` 与 `_rollback` 仍**串行**、仍按 `reversed` 顺序，只增加超时：超时或异常都记 FAILED + 日志后继续，绝不阻断其余服务停下。

```python
            timeout = _resolve_timeout(service.stop_timeout, self._service_settings.stop_timeout)
            try:
                await _call_with_timeout(service.stop, timeout)
            except TimeoutError as exc:
                service.state = ServiceState.FAILED
                service.log_error("服务关闭超时", exc, 超时秒数=timeout)
                continue
```

### 4.6 超时默认值的固化

`_service_settings` 在 `__init__` 里用 `ServiceSettings()` 占位、`_build` 时用 `settings.service` 覆盖：

- 启停路径必然经过 `_ensure_built()`，占位值不会被读到；
- 反过来不能在 `__init__` 里就调 `get_settings()`——那会让模块级 `default_manager = build_manager()` 在 import 期读配置文件，破坏"惰性装配、无 import 副作用"这条既有约定。

## 五、错误处理与日志

**不新增任何错误类型**：

| 场景 | 异常 |
| --- | --- |
| 配置节点类型歧义 / 节点未注册 | `ServiceContractError` |
| 启动超时 | `ServiceStartError`（cause 为 `TimeoutError`） |
| 启动抛错 | `ServiceStartError`（cause 为原异常，行为不变） |
| 关闭超时 / 关闭抛错 | 只记日志 + `state=FAILED`，不向外抛（行为不变） |

`ServiceError` 仍不是 `AppError`，`ConfigError` 也仍只负责配置期，两者都不会被转成 4xx（项目已无 web 层，此处只是保持分层的语义边界）。

日志新增 / 变化：

- 新增 `同层服务启动完成`（`层=` / `服务数=` / `耗时=`），仅在该层有待启动服务时打，避免重复 `start_all` 刷屏。
- 单服务的 `耗时=` 字段去掉（并发下失去意义），改由整层耗时承担。
- 超时相关日志统一带 `超时秒数=` 字段，走 `service.log_error`，保留 `[label]` 前缀与 `错误=类型: 消息` 格式。

## 六、模块结构与改动清单

| 文件 | 动作 |
| --- | --- |
| `core/config/settings.py` | 新增 `ServiceSettings` / `ClockSettings`，`Settings` 加 `service` / `clock` 字段 |
| `core/config/__init__.py` | 导出两个新配置类 |
| `core/service/base.py` | 新增 `_Unset` / `UNSET` 与 `start_timeout` / `stop_timeout` 两个 `ClassVar` |
| `core/service/manager.py` | 改动主体：`_Plan` dataclass、配置索引、`_resolve_levels`、同层并发、超时 |
| `core/service/clock_service.py` | 构造器接收 `ClockSettings`，`now()` 走配置时区，`start()` 校验时区 |
| `core/service/__init__.py` | 导出 `UNSET` |
| `data/config/app.yaml` | 补 `service:` / `clock:` 两段（值等于默认） |
| `README.md` | 配置项表格补 3 行；服务章节约定表补配置节点注入、超时三态、同层并发 |
| `tests/test_service_injection.py` | 补 A 的用例；同步改 `ClockService()` 的直接构造 |
| `tests/test_service_lifecycle.py` | 新增，放 B 的用例 |

依赖方向不变：`service → (config, logger)`；`ClockSettings` 定义在 `core/config/`，**不能**挪进服务模块（那会造成 `config → service` 反向依赖）。

## 七、测试策略

### A（补进 `tests/test_service_injection.py`）

| 用例 | 断言 |
| --- | --- |
| 配置节点按类型注入 | `Settings(clock=ClockSettings(tz="Asia/Shanghai"))` → `mgr.get(ClockService).now().utcoffset() == timedelta(hours=8)`（行为断言，不白盒 `_config`） |
| 整份 `Settings` 注入仍可用 | 既有 `SettingsAwareService` 用例，作回归 |
| 配置节点未注册 | 注解是 `BaseModel` 但不在 `Settings` 顶层 → `ServiceContractError`，`match="顶层字段"` |
| 配置节点类型歧义 | 测试局部 `class DupSettings(Settings): extra_clock: ClockSettings = ClockSettings()` → `ServiceContractError`，`match="出现多次"` |
| 非法时区启动期失败 | `Settings(clock=ClockSettings(tz="Not/AZone"))` → `start_all()` 抛 `ServiceStartError` |

### B（新建 `tests/test_service_lifecycle.py`）

| 用例 | 断言 |
| --- | --- |
| 同层并发启动 | 两个无依赖服务，各自 `start` 里先 `set(自己已进入)`，再 `await asyncio.wait_for(对端已进入, timeout=1)`。串行则对端事件永不置位 → 超时失败。**用事件互等判定并发，不用 sleep 断言耗时**，避免时间抖动 |
| 跨层不并发 | B 依赖 A，两边把 `enter` / `exit` 记进列表，断言 `exit:A` 早于 `enter:B` |
| 启动超时触发回滚 | 全局 `start_timeout=0.05`，服务 `start` 里 `sleep(10)` → `ServiceStartError`、该服务 FAILED、已启动的依赖被 stop |
| 服务级超时覆盖全局 | 全局 `0.05`，该服务 `start_timeout: ClassVar[float \| None] = None`，`sleep(0.1)` 仍成功 |
| `UNSET` 跟随全局 | 不写 `ClassVar`，全局 `0.05` 时同样超时 |
| 关闭超时不影响其他服务 | `stop_timeout=0.05`，坏服务 `stop` 里 `sleep(10)` → `stop_all()` 不抛错、该服务 FAILED、其余服务 STOPPED |
| 失败者取注册顺序最靠前的 | 同层两个失败服务 → `ServiceStartError.service_name` 等于先注册的那个 |

既有 `tests/test_service_manager.py` 的事件断言不受影响：`db` / `cache` / `api` 分属 0 / 1 / 2 层，同层内无顺序断言；`test_健康检查聚合` 的 `["db", "cache", "api"]` 仍等于扁平 `_order`。

## 八、破坏性变更与风险

**破坏性变更**：

- `ClockService.__init__` 新增必填参数 `config: ClockSettings`，直接 `ClockService()` 构造会断（仓库内仅 `tests/test_service_injection.py` 一处，随本设计同步改）。
- `data/config/app.yaml` 里 `service:` / `clock:` 两个新键，**必须先把代码升级上去**再写进 YAML：`Settings` 是 `extra="ignore"`，旧代码读到新键不报错也不生效，会形成静默失效。

**风险与已规避项**：

- **超时靠取消实现**：`asyncio.timeout` 是通过取消内层协程生效的，因此服务的 `start()` / `stop()` **不得吞掉 `CancelledError`**（写 `except Exception` 是安全的，`CancelledError` 在 3.8+ 已继承 `BaseException`）。
- **并发让日志顺序不再确定**：同层服务的启动日志顺序未定义，靠日志顺序推断依赖关系的做法会失灵。依赖关系仍可由 `dependencies` 声明直接读出。
- **`zoneinfo` 依赖系统时区库**：极简镜像（无 `tzdata`）下非 `UTC` 时区会解析失败，默认值 `UTC` 也走同一通路。这是引入 `ClockSettings.tz` 的唯一新增部署要求，目标环境缺时区库时需补 `tzdata` 依赖。
- **默认 30s 对慢启动服务可能不够**：属可配项（YAML 全局调，或服务级 `ClassVar` 覆盖）。
- **同层并发会放大瞬时资源压力**：多个服务同时建连接可能打爆下游。分层是显式可控的，需要串行时把它们放进不同层即可。

## 九、验收标准

1. `uv run pytest` 全绿（既有 8 个测试文件 + 新增用例），无回归。
2. `uv run ruff check .` 与 `uv run ruff format --check .` 无输出。
3. `uvx basedpyright core tests` 报 `0 errors, 0 warnings, 0 notes`。
4. `uv run python -m core.main` 启动时打印 `clock` 健康且 `running`，Ctrl-C 优雅关闭并打印"全部服务已停止"。
5. 配置覆盖生效：把 `app.yaml` 的 `clock.tz` 改成 `Asia/Shanghai` 后启动，`now()` 偏移为 +08:00（由测试覆盖，冒烟可选）。
6. README 配置项表格含 `service.start_timeout` / `service.stop_timeout` / `clock.tz`；服务章节含"配置节点注入""超时三态""同层并发启动"三条约定。
