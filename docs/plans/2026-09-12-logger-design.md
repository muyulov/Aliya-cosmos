# 日志层完善设计

日期：2026-09-12

## 背景

`core/logger/` 目前已有 `faces.py`、`formatters.py`、`types.py`、`setup.py` 四个文件，能输出树形/JSON 两种形态。但在实际使用中暴露出三个短板：

1. **请求上下文不自动携带。** `request_id` 由每个调用点手写字段传入，漏写即断链；`Log.bind_request` 用 `logger.configure` 全局改 `extra`，多请求并发时互相覆盖。
2. **异常堆栈格式混乱。** `log.exception` 的 traceback 被 loguru 拼在 message 之后，与 `├─ 字段` 树块糊在一起，多行结构失去可读性。
3. **文件 sink 配置不足。** 只有 `logs/app.log` 一个文件，按 `10 MB` 轮转，错误日志淹在普通日志里；`setup_logging` 末尾的"日志已就绪"打印时机早于标准库接管。

本设计解决上述三点。另有第四项（日志体积控制/截断）经评估后移出本轮范围，见文末"本轮不做"。

## 设计目标

- 请求链路日志自动携带 `request_id`，调用点零手写。
- 协程/并发安全，杜绝上下文串号。
- 异常堆栈独立成块，与结构化字段互不干扰；JSON 模式下作为独立键。
- 主日志与错误日志分离，均按天轮转。
- 不新增任何第三方依赖，只用 loguru 与标准库能力。

## 模块结构

`core/logger/` 变为六个文件，依赖严格单向无环：

```
types.py       类型别名，不依赖任何同级模块
faces.py       颜文字常量表，不依赖任何同级模块
context.py     ← 新增。上下文存储，只依赖标准库 contextvars
formatters.py  ← 增强。依赖 faces / context / types
sinks.py       ← 新增。sink 装配与 filter 工厂，依赖 formatters / types
setup.py       ← 瘦身。门面 Log + 装配编排 + 标准库桥接，依赖以上全部
```

`__init__.py` 的导入顺序需同步调整：先 `faces`、`context`，再 `formatters`，最后 `setup`，并把新符号补进 `__all__`。

依赖方向上的关键约定：`context.py` **不依赖 loguru**，`formatters.py` 只读上下文、不写。`api` 层只依赖 `context`，`service` 层完全不碰日志上下文，维持"service 不引用框架层"的既有约定。

## 一、请求上下文自动携带

### `context.py` 接口

```python
def bind(**kv: object) -> ContextToken        # 写入当前上下文
def reset(token: ContextToken) -> None        # 还原
def current() -> dict[str, str]               # 读出快照
def request_id() -> str                       # 取 request_id，缺失返回 "-"
def new_request_id() -> str                   # uuid4().hex[:16]

@contextmanager
def request_scope(request_id: str | None = None) -> Iterator[str]
    # 生成或沿用 request_id，进入时 bind，退出时 reset，yield 最终 id
```

### 实现要点

存储用 `contextvars.ContextVar[dict[str, str]]`，默认值为空字典。`bind` 生成新 dict 后 `set` 并返回 token，`reset(token)` 还原。`contextvars` 天然按协程隔离，配合 `reset` 保证嵌套场景正确回退，彻底消除并发串号。

**值类型限制为 `str`。** 上下文值会跨 await 传播并写入日志，限制成字符串可避免往上下文塞大对象导致引用滞留。需要传数字或布尔时，调用方自行转字符串。

### 键名约定

上下文快照里约定保留键 `request_id`。其余键由调用方自定义，渲染时统一并入字段。

### 优先级规则

调用点显式传入的 kwargs **覆盖**上下文值。理由：显式传值通常是刻意为之（例如打印另一条链路的 id），上下文是兜底。

合并逻辑：`fields = {**context.current(), **调用点 kwargs}`。`request_id` 不特殊处理，同样遵循"显式覆盖"规则。

### `Log` 门面改动

- 新增 `log.context(**kv)`，返回上下文管理器，供 service 层临时附加业务维度，例如 `with log.context(会话="xxx"):`。
- **删除 `Log.bind_request`。** 它用 `logger.configure(extra=...)` 全局改写 extra，是并发串号的元凶，由 `request_scope` 取代。

### `api` 层改动

- `core/api/middleware.py`：`RequestContextMiddleware.dispatch` 改用 `with request_scope(request.headers.get(REQUEST_ID_HEADER)) as rid:` 包住 `call_next`；删除日志里手写的 `request_id=request_id` 字段。
- `core/api/errors.py`：删除各处理器日志里手写的 `request_id=request_id` 字段。`_request_id(request)` **保留**，它仍负责填充对外响应信封的 `request_id`（属于 HTTP 契约，不是日志字段）。
- 常规路径下 `request_id` 由渲染层自动从上下文取值。

### 500 路径的例外

`Exception` 处理器由 Starlette 的 `ServerErrorMiddleware` 在最外层执行，此时
`RequestContextMiddleware` 的上下文已退出，从上下文取 `request_id` 只会得到
占位符 `"-"`。因此该处理器必须显式从 `request.state` 取值并作为字段传入：

```python
request_id = _request_id(request)  # 读 request.state，唯一可靠来源
log.exception("未捕获异常", request_id=request_id, 路径=..., 异常=...)
```

`_request_id` 因此不设"回退到上下文"的分支——那在 500 路径下必然失效，在其它
路径下又走不到，属于会掩盖问题的死代码。

## 二、异常堆栈独立渲染

### 问题定位

`_render_filter` 把 `record["message"]` 整体预渲染成树形文本，而 loguru 的堆栈在 message 之后自行拼接，导致 traceback 与字段块混杂。

### 改法

在 filter 里自行渲染堆栈，并关闭 loguru 自身的堆栈输出：

1. 渲染完树形正文后，若 `record["exception"]` 非空，用 `traceback.format_exception` 生成堆栈文本，作为独立块接在字段块之后。
2. 将 `record["exception"]` 置为 `None`，阻止 loguru 再追加一份。

### 输出形态（树形模式）

```
2026-09-12 10:00:00 [E] (x_x) 未捕获异常
    ├─ 路径: /api/v1/items
    └─ 异常: KeyError
    └─ 堆栈
       File "core/api/app.py", line 42, in create_app
         ...
       KeyError: 'item_id'
```

### JSON 模式

堆栈不拼进 message，而是作为独立键 `"exception"` 存放完整堆栈字符串。`\n` 由 `json.dumps` 转义，仍为单行 JSON，采集友好。

### 新增函数

`formatters.py` 新增 `format_exception(record) -> str | None`，只负责生成堆栈文本，从 `TracebackException` 提取。

### 顺带修复

`setup.py` 的 `logger.add(...)` 已传 `backtrace=False`，但未传 `diagnose=False`。loguru 默认 `diagnose=True` 会把局部变量值打进堆栈，prod 下既易泄露又拖慢性能。本轮一并补上 `diagnose=False`。

## 三、文件轮转与错误独立文件

### 三个 sink

在 `sinks.py` 中统一装配：

| sink | 目标 | 级别 | 轮转 | 着色 |
| --- | --- | --- | --- | --- |
| 控制台 | stderr | `cfg.level` | — | 仅 TTY |
| 主日志 | `logs/app.log` | `cfg.level` | `00:00`（按天） | 否 |
| 错误日志 | `logs/error.log` | 固定 `ERROR` | `00:00`（按天） | 否 |

三个 sink 共用同一个 `_render_filter`，仅 `color` 参数不同。

### 配置改动

`LogSettings` 变更：

- 新增 `error_file_name: str = "error.log"`。
- `rotation` 默认值由 `"10 MB"` 改为 `"00:00"`。

这是本轮唯一新增的配置项，理由是错误日志路径属于运维会主动排查的对象，值得可配置。

### 时序修正

`setup_logging` 结尾的"日志已就绪"目前打印在 `logger.remove()` 之后、`_intercept_stdlib()` 之前，此时标准库尚未接管。改为在装配全部完成后打印，并额外输出各 sink 的实际路径，便于排查"日志写到了哪里"。

### 多 sink 共享 record 的约束

loguru 会把**同一个 record 对象**按 sink 注册顺序依次喂给各 sink 的 filter，
前一个 sink 对 `record["message"]` 的改写后一个 sink 会原样看到。这带来两个必须
规避的坑：

1. 若用"只渲染一次"的标记位，先执行的控制台 sink 着色后会污染后续文件 sink，
   把 ANSI 转义序列写进日志文件。
2. 若谁先渲染谁就清空 `record["exception"]`，后渲染的 sink 会丢失堆栈。

对策是两条：每个 sink **无条件重新渲染**自己的 message（不设渲染标记，不依赖
执行顺序）；原始消息与堆栈文本**各缓存一份到 extra**，供所有 sink 复用。
`formatters` 的渲染函数因此改为接受 `stack` 关键字参数，不再自己从 record 取值。

## 测试计划

`tests/test_logger.py` 现有测试覆盖格式化纯函数，全部保持通过。新增覆盖：

1. `context.py`：`bind` / `reset` 嵌套还原；`request_id()` 缺失时返回 `"-"`；`request_scope` 生成与非生成两种路径；并发隔离（两个 `asyncio.Task` 各自绑定互不污染）。
2. `formatters.py`：`format_tree` 带堆栈时输出独立堆栈块且字段块完整；`format_json` 带堆栈时含 `"exception"` 键且整体为单行 JSON。
3. `sinks.py`：三个 sink 装配后 `logger` 的 handler 数量与级别过滤符合预期；`ERROR` 级别同时落入主日志与错误日志，`INFO` 只落主日志。

测试用 `tmp_path` 承接文件 sink，避免污染仓库根目录的 `logs/`。

## 影响面

修改文件：`core/logger/__init__.py`、`core/logger/formatters.py`、`core/logger/setup.py`、`core/config/settings.py`、`core/api/middleware.py`、`core/api/errors.py`、`tests/test_logger.py`、`README.md`。

新增文件：`core/logger/context.py`、`core/logger/sinks.py`。

## 本轮不做

- **日志体积控制（截断）。** 原计划的单值长度上限与字段数量上限移出本轮，`formatters.py` 本轮只做堆栈渲染相关改动，不改 `render_value` 的截断行为。
- **敏感字段脱敏。** 已明确不做。脱敏放在日志层本就不可靠——调用方一旦把密钥拼进 message 字符串，键名匹配即失效。
