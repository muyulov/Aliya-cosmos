# service 层依赖注入改造 设计与开发计划

日期：2026-09-12
状态：设计已确认，待实施

## 一、目标与范围

把 `core/service` 从"只会按依赖排序启停的注册表"升级为"能真正组装依赖的服务容器"。

现状的缺口很具体：`dependencies` 只参与拓扑排序，不注入。`CacheService` 即便声明依赖 `db`，`start()` 里也拿不到 `db` 实例，只能自己想办法（全局查表、模块级单例），结果是依赖关系不可见、测试无法替换依赖、服务内部出现"`start` 之前引用是 `None`"的半初始化状态。服务同样拿不到 `Settings`，也拿不到带自身维度的 logger。

本次做四件事：

1. 依赖改为按**类型**声明，由容器在构造服务时注入。
2. `Settings` 作为容器内置依赖注入，服务需要时在构造器里声明即可。
3. 服务自带 `self.log`，自动带 `服务=` 字段与 `[服务名]` 消息前缀，并提供一个统一错误格式化的 `log_error`。
4. 顺带修掉关闭顺序退化的 bug：`_start_order` 只记录"本次调用新启动的服务"，重复 `start_all()` 后 `stop_all()` 会退化成按注册顺序关闭。

非目标（本轮不做）：

- `start` / `stop` 超时控制、manager 重入保护、`state` 只读封装。这些属于"生命周期健壮性"，是本轮之后的独立改造。
- 服务的自动发现与扫描。保持 `registry.py` 里手写注册。
- 可选依赖、`Protocol` 依赖、依赖的懒加载与运行期解构。
- 依赖失败的重试。装配期失败一律 fail fast。

## 二、决策记录

**注入机制选构造器注入，否决 `start(ctx)` 钩子注入与属性注入。** 构造器注入与既有的"service 层是服务容器"定位一致：组装是容器的职责，不该甩给服务自己在 `start()` 里到处 `manager.get()`。依赖在 `__init__` 里就是普通参数，basedpyright 能全程推理，不存在半初始化状态。`start(ctx)` 方案改动最小，但依赖关系要到运行期才暴露；属性注入需要描述符或 `getattr` 魔法，静态检查基本失效。

**保留 `dependencies` 声明，并与构造器签名交叉校验，否决纯内省签名推断。** 内省方案少一处声明、不可能漂移，但本仓库到处是 `from __future__ import annotations`，内省要走 `get_type_hints`，依赖类型必须能在模块全局解析，遇到 `Protocol`、字符串注解或可选依赖容易翻车，而且依赖图不再在类顶端一眼可见。保留声明 + 装配期对账，既让依赖图可 grep，又用 fail fast 消除漂移风险——漂移的代价从"运行期谜之错误"变成"启动即报错"。

**`name` 从唯一标识降级为展示标签。** 依赖解析改按类型后，`name` 不再参与任何解析逻辑，只用于日志与健康检查展示。默认取类名，因此绝大多数服务不用写它；重复注册的判定交给类型。手动传入的名字重复不再报错（仅影响日志可读性）。

**`self.log` 同时提供字段与消息前缀，两者拆成两个门面方法。** 字段走 `bind`（结构化，符合本项目"看字段不看拼字符串"的日志哲学），前缀走新增的 `prefix`（换取 grep 直观）。拆成两个方法而不是给 `bind` 加一个"既当字段又当前缀"的魔法参数，是为了让两个能力各自独立、可单独复用，例如中间件只想要 `[GET /items]` 前缀时不必被迫加字段。

**不加生命周期日志糖。** 不把 `log_started` / `log_stopped` / 耗时下沉到基类。这些是编排视角的日志（含"已启动 N 个"这类跨服务信息），属于 manager 的职责，下沉会让基类与容器耦合。也不把门面的六个方法转发到 `Service` 上，避免 `Service.info` 这类命名空间污染。

## 三、契约层：`core/service/base.py`

```python
class Service:
    #: 展示名：仅用于日志与健康检查展示，缺省取类名
    name: ClassVar[str] = ""

    #: 依赖的服务类型；容器据此排序并在构造时注入
    dependencies: ClassVar[tuple[type[Service], ...]] = ()

    def __init__(self) -> None:
        self.state: ServiceState = ServiceState.CREATED
        self.log: Log = log.bind(服务=self.label).prefix(self.label)

    @property
    def label(self) -> str:
        """展示名：显式 name 优先，否则取类名。"""
        return self.name or type(self).__name__

    def log_error(
        self,
        message: str,
        exc: BaseException | None = None,
        **fields: object,
    ) -> None:
        """统一错误日志：自动拼「错误=类型: 消息」，可选带异常对象。"""
```

`log_error` 是本次唯一新增的日志方法。它的价值是消灭重复：`manager.py` 里 `错误=f"{type(exc).__name__}: {exc}"` 这个格式化当前手写了三处（启动失败、关闭异常、回滚异常），改造后三处都改调 `service.log_error(...)`，格式收敛成一处。

新增一条写进 README 的约定：**`__init__` 只做赋值与依赖接收，连接、预热、加载这类动资源的活一律留到 `start()`。** 因为构造发生在装配期（lifespan 之前），在 `__init__` 里连数据库会让"装配失败"和"启动失败"混成一锅，回滚逻辑也会失去意义。

`start` / `stop` / `health` / `running` / `HealthStatus` 的签名与行为都不动。

## 四、日志门面增强：`core/logger/setup.py`

`log` 不是 loguru 的 logger，是项目自己的门面类 `Log`（只暴露 `debug/info/success/warning/error/critical/exception/context`），没有 `bind`。需要补两个方法：

```python
class Log:
    def __init__(self, base: dict[str, object] | None = None, prefix: str = "") -> None:
        self._base = base or {}
        self._prefix = prefix

    def bind(self, **kv: object) -> Log:
        """返回带基础字段的派生门面，字段优先级低于上下文与调用点。"""
        return Log({**self._base, **kv}, self._prefix)

    def prefix(self, text: str) -> Log:
        """返回带消息前缀的派生门面，渲染为 [text] 消息。"""
        return Log(self._base, text)

    def _emit(self, level: str, message: str, face: str | None = None, **fields: object) -> None:
        resolved = face or faces_module.DEFAULT_BY_LEVEL.get(level.upper(), "")
        merged = {**self._base, **context_module.current(), **fields}
        shown = f"[{self._prefix}] {message}" if self._prefix else message
        logger.bind(face=resolved, fields=merged).log(level.upper(), shown)
```

字段合并顺序为"基础字段 → 上下文 → 调用点"，调用点优先，这样服务里临时覆盖 `服务=` 是可能的。`exception` 方法同样按此处理前缀。

服务内输出效果：

```text
2026-09-12 10:00:00 [I] (^_^) [clock] 时钟服务已启动
    ├─ 服务: clock
    └─ 时区: UTC
```

这层改动对日志层是纯增量：既有的全局 `log` 是一个 `Log()` 空实例，行为完全不变，`log.context()` 也照旧走全局 contextvars。

## 五、容器层：`core/service/manager.py`

```python
class ServiceManager:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings                  # None 则在装配时取 get_settings()
        self._types: list[type[Service]] = []      # 注册顺序
        self._instances: dict[type[Service], Service] = {}
        self._order: list[type[Service]] = []      # 装配后固定的启动序
        self._built = False
```

装配是**惰性**的：`get` / `services` / `names` / `health` / `start_all` 首次被调用时触发 `_ensure_built()`。这样 `health()` 在未启动时仍能返回状态为 `CREATED` 的服务列表，既有语义不破。

`_build()` 分四步：

1. **契约校验**。遍历已注册类型，用 `get_type_hints(cls.__init__)` + `inspect.signature` 解析构造参数（排除 `self`），把参数按注解分成三类：`Service` 子类、`Settings` 子类、其余。以下任一情况抛 `ServiceContractError`：
   - `dependencies` 里声明的类型在构造器签名里找不到对应参数；
   - 签名里有 `Service` 子类参数但未在 `dependencies` 中声明；
   - 注解既不是 `Service` 子类也不是 `Settings` 子类（容器无法解释该参数）；
   - 出现 `*args` / `**kwargs` 或没有注解的参数。
   错误消息里带上服务类名、参数名与具体原因。
2. **拓扑排序**。边来自 `dependencies`。依赖类型未注册抛 `MissingDependencyError`，检测到环抛 `CircularDependencyError`。
3. **按序构造**。`Service` 类型参数从 `self._instances` 取（拓扑序保证此时已构造完），`Settings` 参数注入 `self._settings or get_settings()`，其余参数已在校验阶段被拒。构造后写入 `_instances`。
4. **固化顺序**。`self._order = order`，`self._built = True`。

`register(service_type)` 只接受 `Service` 子类，拒绝重复注册，拒绝在装配完成后继续注册（三者都抛 `ServiceContractError`）。

`get(service_type)` 改为按**精确类型**查字典，不再 `isinstance` 线性扫描——后者在"注册的是子类、查的是基类"时会静默返回第一个匹配，属于隐式行为。未注册抛新增的 `ServiceNotRegisteredError`，替代现在的裸 `KeyError`。这带来一条必须写进文档的约束：**依赖声明要写被注册的那个具体类型**，不能拿抽象基类当占位。

`start_all()`：`_ensure_built()` → 按 `_order` 逐个启动，跳过已 `RUNNING` 的，记录耗时，失败则 `_rollback(本次已启动的)` 并抛 `ServiceStartError`。

`stop_all()`：**关闭顺序在装配期就固定为 `reversed(self._order)`，删除 `_start_order`。** 这是本次修掉的 bug：原实现里 `_start_order` 只装"本次调用新启动的服务"，所以 `start_all()` 被调用两次（第二次全部跳过）后它变成空列表，`stop_all()` 就退化成按注册顺序关闭，可能违反依赖逆序。改法不需要可变状态，`stop_all()` 因此天然幂等。已 `STOPPED` / `CREATED` 的服务跳过，单个服务抛错只记录不阻断其余服务。

`health()` / `lifespan()` 逻辑不变。

错误类型收敛到 `ServiceError` 一棵树，不再混用裸 `ValueError` / `KeyError`：

| 类型 | 触发条件 |
| --- | --- |
| `ServiceError` | 基类 |
| `ServiceContractError` | 非 `Service` 子类、重复注册、装配后注册、签名不可解释、声明与签名不一致 |
| `ServiceNotRegisteredError` | `get()` 查的类型未注册 |
| `MissingDependencyError` | `dependencies` 里的类型没注册 |
| `CircularDependencyError` | 依赖成环 |
| `ServiceStartError` | 某个服务 `start()` 抛错（携带 `service_name` 与 `cause`） |

`ServiceError` 不是 `AppError`，所以不会变成 4xx，只会让 lifespan 启动失败、进程退出。这是预期的 fail fast。

## 六、装配入口：`core/service/registry.py` 与 `core/api/app.py`

```python
def build_manager(settings: Settings | None = None) -> ServiceManager:
    manager = ServiceManager(settings)
    _ = manager.register(ClockService)
    _ = manager.register(ItemService)
    return manager


#: 全局默认 manager。Web 场景请使用 app.state.services，避免多实例互相干扰。
default_manager = build_manager()
```

`default_manager` 保留。因为装配改成惰性、`Settings` 也延迟到装配时才解析，模块级构造一个 manager 现在只往列表里塞两个类型，既不读配置也不实例化服务，原本的 import 副作用随之消失。

`create_app` 里改为 `services = manager or build_manager(cfg)`，把工厂持有的配置交给容器。`core/api/deps.py` 不动，`manager_dep` 的 fallback 仍是 `default_manager`。

本轮不做自动发现，`registry.py` 保持手写注册，新增服务仍是"加一个类 + 在这里 register 一行"。

## 七、示例服务改造

新增 `core/service/clock_service.py`：

```python
class ClockService(Service):
    """时钟服务：为其他服务提供可替换的时间来源。"""

    name: ClassVar[str] = "clock"

    @override
    async def start(self) -> None:
        if self.state is ServiceState.RUNNING:
            return

    def now(self) -> datetime:
        return datetime.now(UTC)
```

`now()` 刻意保持**同步**：它不涉及 I/O，服务方法不必一律 `async`。这样 `ItemService._create` 的调用链不用改成异步，改动面最小。

`ItemService` 改为：

```python
class ItemService(Service):
    name: ClassVar[str] = "item"
    dependencies: ClassVar[tuple[type[Service], ...]] = (ClockService,)

    def __init__(self, clock: ClockService) -> None:
        super().__init__()
        self._clock = clock
        self._items: dict[int, Item] = {}
        self._counter: count[int] = count(1)
```

`_create` 里 `created_at` 来源从 `datetime.now(UTC)` 换成 `self._clock.now()`。

这个示例兑现了注入的实际收益：单测里 `ItemService(FakeClock(...))` 就能锁死时间，断言 `created_at` 精确值，完全不需要容器参与。

## 八、影响面清单

| 文件 | 改动 |
| --- | --- |
| `core/logger/setup.py` | `Log` 新增 `bind` / `prefix`，`_emit` 与 `exception` 应用前缀 |
| `core/service/base.py` | 契约改造（`name` 语义、`dependencies` 类型化）、新增 `label`、`self.log`、`log_error` |
| `core/service/manager.py` | 类型注册、惰性装配、契约校验、精确查找、顺序固化、错误类型收敛、日志改调 `service.log_error` |
| `core/service/clock_service.py` | 新增 |
| `core/service/item_service.py` | 声明 `ClockService` 依赖，构造器接收并用于生成 `created_at` |
| `core/service/registry.py` | 注册类型；`build_manager(settings=None)` |
| `core/service/__init__.py` | 导出 `Log` 相关不导出、新增服务与错误类型导出 |
| `core/api/app.py` | `build_manager(cfg)` |
| `tests/conftest.py` | 一行：`register(ItemService)` 改注册类型 |
| `tests/test_logger_sinks.py` | 一行：`register(ItemService())` 改注册类型 |
| `tests/test_service_manager.py` | 约六成重写：`make_service` 改为返回类，断言从事件列表改从 `mgr.get(Cls)` 取 |
| `tests/test_items.py` | 若 fixture 依赖 `ItemService()` 直接构造，改为传入 `ClockService()` |
| `tests/test_service_injection.py` | 新增 |
| `README.md` | 服务章节重写：依赖声明与注入、`__init__` 只赋值、`self.log` 用法、错误类型表 |

`core/api/deps.py`、`core/api/v1/*`、`core/config/*` 不动。

## 九、测试计划

`tests/test_service_injection.py` 覆盖：

- 按类型注入正确：`cache` 拿到的是注册的那个 `db` 实例（`is` 断言）。
- `Settings` 注入：构造器声明 `settings: Settings` 的服务拿到容器持有的实例。
- 契约校验四类报错：声明了签名没有、签名有但没声明、注解类型容器不认识、`*args`。
- 未注册类型 `get()` 抛 `ServiceNotRegisteredError`；依赖未注册抛 `MissingDependencyError`。
- 装配惰性：`register` 之后、首次访问之前不构造任何实例（用计数器或构造副作用断言）。
- 装配后 `register` 报错。
- 关闭顺序：重复调用 `start_all()` 两次后再 `stop_all()`，关闭仍严格逆序（这条正是原 bug 的回归测试）。
- `ItemService(FakeClock(...))` 的时间注入：绕过容器直接构造，`created_at` 等于假时钟时间。

`test_service_manager.py` 保留原有场景（拓扑启动、失败回滚、逆序关闭、循环依赖、缺失依赖、启停幂等、关闭异常不阻断、健康检查聚合），适配为新 API。

## 十、实施阶段

| 阶段 | 内容 | 验收标准 |
| --- | --- | --- |
| P0 | `Log.bind` / `Log.prefix` | 既有日志测试全绿，新增前缀与字段叠加的断言通过 |
| P1 | `base.py` 契约改造 | `log_error` 输出格式断言通过，`ruff` 与 basedpyright 无错 |
| P2 | `manager.py` 容器改造 | `test_service_injection.py` 全绿，关闭顺序回归测试通过 |
| P3 | `registry` / `app` / `ClockService` / `ItemService` | `uv run python -m core.main` 能起，`/health` 返回两个服务且均健康 |
| P4 | 既有测试适配 + README | `uv run pytest` 全绿，`uv run ruff check .` 通过，README 示例照抄可跑 |

## 十一、扩展位

后续需要时再做的落点：启动/关闭超时与重入保护在 `manager.start_all` / `stop_all` 外层包 `asyncio.timeout`；`state` 只读化给 `Service.state` 加 property 与私有字段；服务自动发现改 `registry.build_manager` 扫描 `core/service/` 下的 `Service` 子类；可选依赖把注解改 `DbService | None` 并在校验阶段识别 `Union`；服务级配置在 `Settings` 下加子模型，容器按类型注入对应子模型而不必再掰 `dependencies`。
