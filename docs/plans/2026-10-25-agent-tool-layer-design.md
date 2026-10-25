# Agent 工具层（工具注册表 + 提示拼装）设计

- 日期：2026-10-25
- 状态：设计已定（brainstorming 四节逐节确认）
- 目标：在现有 `service / logger / config / llm / embedding / decision` 之外，新增 `core/agent/`，
  把「工具怎么声明、怎么交给模型、怎么执行回填」以及「system 提示怎么拼」收成一个小库，
  让业务代码不必每次手写这三件事。

---

## 0. 决策速览

| # | 议题 | 决策 |
| --- | --- | --- |
| 1 | 能力边界 | **只做工具层**：工具注册表 + 提示拼装；ReAct 循环仍由调用方写 |
| 2 | 工具声明 | **装饰器 + 类型注解**（`@tool`），schema 由 pydantic 从签名推导 |
| 3 | 提示拼装 | **分段拼 system**：去空白、跳空段、`\n\n` 连接，无模板引擎 |
| 4 | 失败语义 | **全部回填**：校验失败与 handler 异常都转文本回给模型；只有编程错误才抛 |
| 5 | 形态 | **纯库**，不继承 `Service`、不进 `ServiceManager`、无配置项 |
| 6 | 位置 | `core/agent/`（`pyproject` 的打包范围是 `core`） |
| 7 | 注册表作用域 | **显式实例**，无模块级全局注册表 |
| 8 | 并发 | 一次多个 `tool_calls` 由调用方逐个执行；层内不并发 |
| 9 | 日志口径 | DEBUG 级记 工具 / 耗时 / 成败 / 错误类型，**不记参数与返回值** |

---

## 1. 定位与边界

这个层解决一件事：**把「工具」从普通函数变成模型能选、能调、结果能回填的东西，把系统提示从散落的字符串变成一条消息。**

它**是**：

- 工具声明层：一个 `@tool` 装饰器，把带类型注解的函数推导成 `Tool`（名称 / 描述 / 参数模型 / handler）。
- 工具注册表：按注册顺序导出 `ChatCompletionToolParam[]`，按 `ToolCall` 执行并归一成文本。
- 提示拼装器：把若干段文本合成一条 system 消息。

它**不是**：

- Agent 循环（「模型要工具 → 执行 → 回填 → 再问」的 while）——由调用方写。
- 会话管理（历史消息、裁剪、记忆）——本次不做。
- 配置层——本层没有旋钮，不需要 `AgentSettings`。
- 服务层——没有连接、没有生命周期，不进 `ServiceManager`。

---

## 2. 目录与依赖方向

```text
core/agent/
  __init__.py    出口（Tool / ToolRegistry / tool / system_prompt / 错误类型）
  errors.py      错误树
  tool.py        Tool 数据类 + @tool 装饰器（签名 → pydantic 模型 → JSON Schema）
  registry.py    ToolRegistry：register / declarations / execute
  prompt.py      system_prompt(*sections)
```

- 依赖方向：`agent → (llm, logger)`。
- 不依赖 `config`：本层没有任何可配置项，加一层配置只会多一处要维护的默认值。
- 不被 `llm` 依赖：`llm` 仍然只认 `ChatCompletionToolParam`，不知道 `Tool` 的存在。
- `core/service/registry.py` **不改**：本层不是服务。

---

## 3. 工具模型（`tool.py`）

### 3.1 `Tool`

```python
@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    args_model: type[BaseModel] | None   # 无参函数为 None
    handler: Callable[..., object]
    is_async: bool
```

- `__call__` 透传给 handler：被装饰后仍可直接调用，单测与脚本里不必绕过装饰器。
- 不可变：注册后不允许改名改描述，避免注册表里出现「名字与内容不一致」的半状态。

### 3.2 `@tool` 的推导规则

| 项 | 规则 |
| --- | --- |
| 名字 | 取函数 `__name__`，不支持改名（重名在注册期就报错，改名属 YAGNI） |
| 描述 | docstring 去掉 `Args:` / `参数:` 段后的正文，归一化缩进与首尾空白；**没有 docstring 直接抛 `ToolDefinitionError`** |
| 参数模型 | `pydantic.create_model` 按签名建；参数默认值原样保留（有默认值即可选） |
| 参数描述 | 解析 docstring 的 `Args:` / `参数:` 段，逐行 `名字: 描述`，支持续行缩进；解析不到就省略 `description` 字段 |
| 支持的注解 | pydantic 能处理的即可：`str` / `int` / `float` / `bool` / `None` / `list` / `dict` / `Literal` / `Enum` / `BaseModel` / `Optional` / `Union` |
| 非法签名 | `*args` / `**kwargs` / 参数缺注解 → `ToolDefinitionError` |

为什么缺注解要报错：pydantic 会把无注解参数当 `Any`，生成的 schema 等于没有约束，模型只能靠猜；
在定义期直接失败，比上线后调试「模型老传错参数」便宜得多。

为什么描述来自 docstring：描述是模型选工具的唯一依据，让它在函数定义旁边与服务代码一起演进，
比在注册表里另写一份说明更不容易失同步。

---

## 4. 注册表（`registry.py`）

```python
class ToolRegistry:
    def register(self, *tools: Tool) -> None: ...
    @property
    def names(self) -> tuple[str, ...]: ...
    def __len__(self) -> int: ...
    def declarations(self) -> list[ChatCompletionToolParam]: ...
    async def execute(self, call: ToolCall) -> str: ...
```

约定：

| 约定 | 说明 |
| --- | --- |
| 只收 `Tool` | 不收裸函数，避免「这里推导、那里不推导」的歧义；装饰器是唯一入口 |
| 重名即错 | 同名覆盖会让模型看到两个同名工具或悄悄换掉实现，注册期直接抛 `ToolDefinitionError` |
| 顺序即声明顺序 | `declarations()` 按注册顺序输出，便于调用方预测提示里的工具排列 |
| 复用 `core.llm.tool()` | 声明结构只由 LLM 层定义一次，本层不自己拼 dict |
| 显式实例 | 不提供模块级全局注册表：隐式共享状态会让测试互相污染，也让「哪些工具对哪个 agent 可见」说不清 |
| 不加锁 | 约定装配期注册完、运行期只读；运行期并发注册不受支持（写进模块 docstring） |
| 未知工具名 | `execute()` 抛 `ToolNotFoundError`：模型编出一个不存在的工具名属于调用方契约破坏，不应静默回填 |

---

## 5. 提示拼装（`prompt.py`）

```python
def system_prompt(*sections: str | None) -> ChatCompletionSystemMessageParam
```

- 每段去首尾空白；`None` 与纯空白段一律跳过（调用方可以放心写
  `system_prompt(角色, 规则 if 有规则 else None, 上下文)`）。
- 非空段用空行（`\n\n`）连接——段落之间留白，模型对分段的感知更好。
- 全部为空抛 `PromptError`：没有内容的 system 消息只会白占 token。
- **不做模板插值**：变量由调用方 f-string 填好再传进来，本层不引入第二套字符串语法。
- **不做长度裁剪**：裁剪与会话历史绑定，属会话层的事（本次不做）。
- 返回值直接是 SDK 的 system 消息，调用方拿到就能放进 `messages`。

---

## 6. 执行语义

```python
async def execute(self, call: ToolCall) -> str
```

流程：查表 → 解析参数 → 执行 → 归一成文本。

| 环节 | 规则 |
| --- | --- |
| 查表 | 未注册 → 抛 `ToolNotFoundError` |
| 解析参数 | `args_model.model_validate_json(call.arguments)`；失败 → 回填 `参数校验失败：<pydantic 摘要>` |
| 无参工具 | `args_model is None` 时忽略 `arguments`（模型常传 `{}` 或空串）；传了非空内容也不报错 |
| 调用 | 异步 handler 直接 `await`；同步 handler 走 `asyncio.to_thread`，避免阻塞事件循环 |
| 异常 | 回填 `工具执行失败：[<异常类型>] <消息>`，**不附堆栈** |
| 返回值 | `None` → `"ok"`；`BaseModel` → `model_dump_json()`；`str` → 原样；其余 → `json.dumps(ensure_ascii=False, default=str)` |

为什么两类失败都回填：模型看到「参数校验失败」或「工具执行失败」后通常能自己改参数或换一条路，
这比整轮中断、由调用方重试更省一次往返；堆栈对模型没有信息量，只进日志。

为什么同步 handler 要下线程：工具里常见的 `requests`、文件读、CPU 计算都是阻塞调用，
直接在事件循环里跑会卡住所有并发请求；`to_thread` 的几十微秒开销换来的隔离是值得的。

为什么不做并发：`tool_calls` 里可能有写文件、发消息这类有副作用的调用，
顺序执行才能保证行为可预测，且回填顺序与 `tool_calls` 天然对齐。需要并发由调用方自己 `gather`。

---

## 7. 错误树（`errors.py`）

| 类型 | 触发条件 |
| --- | --- |
| `AgentError` | 基类，调用方一次 `except AgentError` 兜住整层 |
| `ToolDefinitionError` | 无 docstring / `*args`、`**kwargs` / 参数缺注解 / 重名注册 |
| `ToolNotFoundError` | `execute()` 查了没注册的工具名 |
| `PromptError` | `system_prompt()` 的所有分段都为空 |

- **没有 `ToolExecutionError`**：handler 异常与校验失败都被转成回填文本，不属于本层要上报的错误。
- 抛出的都是「调用方写错了」这一类编程错误，fail fast，不做兜底。

---

## 8. 日志

- DEBUG 级，一次执行一条：`工具` / `耗时毫秒` / `成功`（或 `错误类型`）。
- **不记参数与返回值**：工具参数里常有手机号、密钥、文件内容，日志不该是它们的第二份副本。
- 不做 `Service` 式 `self.log` 前缀：本层不是服务；需要区分调用方时由调用方在自己的日志上下文里补字段。

---

## 9. 测试策略

判据与既有各层一致：`uvx basedpyright core tests` 零告警；`pytest --cov=core` **100%**；
`ruff check` / `ruff format --check` 全绿。

- **不打桩 pydantic 与 asyncio**：schema 推导与参数校验都真跑。
- `tool.py`：必填 / 默认值 / `Literal` / 嵌套 `BaseModel` / `Optional` 的 schema；`Args:` 段解析（含无该段）；
  同步与异步 handler；同步 handler 在非主线程执行（断言 `threading.current_thread()` 不是主线程）；
  无 docstring、`*args`、`**kwargs`、缺注解四类定义错误。
- `registry.py`：声明顺序与结构；重名注册；未知工具名；四种返回值归一；
  校验失败回填且不抛；异常回填文本不含堆栈；无参工具忽略空 `arguments`；`names` / `__len__`。
- `prompt.py`：跳空段与 `None` 段；段落用空行连接；全空抛 `PromptError`。
- 每个用例自建注册表，不共享状态。

---

## 10. 非目标（本次明确不做）

- ReAct 循环本体（何时继续、何时停止、最大轮数）；
- 会话历史、记忆、上下文裁剪；
- 流式工具执行 / 流式输出；
- 多个工具并行执行与超时控制；
- 自动重试与降级；
- MCP、外部工具发现；
- 工具权限、人工审批、危险操作拦截；
- 配置项（`AgentSettings`）与服务装配。

这些都能在调用方按需叠加，且各自有独立的取舍，不适合塞进「最基本的能力」。

---

## 11. 用法示例

```python
from core.agent import ToolRegistry, system_prompt, tool
from core.llm import LLMService, assistant, tool_result, user


@tool
def get_weather(city: str, unit: str = "celsius") -> str:
    """查询指定城市的当前天气。

    Args:
        city: 城市名，如「上海」。
        unit: 温度单位，celsius 或 fahrenheit。
    """
    return f"{city} 晴 26℃"


registry = ToolRegistry()
registry.register(get_weather)

messages = [system_prompt("你是行程助手。", "答案只给结论。"), user("上海天气？")]
reply = await llm.chat_tools(messages, registry.declarations())
for call in reply.tool_calls:
    messages.append(tool_result(call.id, await registry.execute(call)))
```

循环仍由调用方写：拿 `reply.tool_calls` 判断是否继续、把 `assistant(tool_calls=reply.tool_calls)`
连同各条 `tool_result` 追加回 `messages`、决定最多几轮。本层只保证「声明对、执行稳、回填是文本」。

---

## 12. 阶段划分

单期交付：`errors.py` → `tool.py` → `registry.py` → `prompt.py` → `__init__.py`，
测试同步落地，README 补章节，一次做完。
