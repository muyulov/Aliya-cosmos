# 工具开发

## 1. 用 `@tool` 声明

工具的唯一声明入口是装饰器：

```python
from core.agent import tool


@tool
def search_orders(user_id: str, status: str = "all") -> list[dict[str, object]]:
    """按用户查询订单。

    Args:
        user_id: 用户 ID。
        status: 订单状态，all 表示全部。
    """
    return [{"id": "A001", "status": "paid"}]
```

装饰后得到 `Tool`，同时仍可直接调用原函数，便于单元测试：

```python
assert search_orders("u1") == [{"id": "A001", "status": "paid"}]
```

`Tool` 的字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `name` | `str` | 函数名，不支持改名 |
| `description` | `str` | docstring 正文，模型选工具的主要依据 |
| `parameters` | `Mapping[str, object]` | 完整 JSON Schema（object） |
| `invoke` | `Callable[[str], Awaitable[str]]` | 原始 arguments JSON 进，回填文本出 |
| `func` | `Callable \| None` | 本地工具的原函数；MCP 工具为 `None` |

## 2. docstring 规范

描述与参数说明都来自 docstring：

```python
@tool
def translate(text: str, target: str) -> str:
    """把文本翻译成目标语言。

    Args:
        text: 待翻译文本。
        target: 目标语言代码，如 en、ja。

    Returns:
        翻译结果。
    """
    ...
```

| 项 | 规则 |
| --- | --- |
| 工具描述 | docstring 中 `Args:` / `参数:` 段之前的正文 |
| 参数描述 | `Args:` / `参数:` 段内逐行 `name: 描述` |
| 续行 | 比参数行缩进更深的行并入上一条描述 |
| 段落终止 | 遇到 `Returns` / `Raises` / `示例` 等标题即停止解析 |
| 缺描述 | 没有 docstring 或正文为空，定义期抛 `ToolDefinitionError` |

描述缺失必须在定义期失败：模型选工具时只看描述，空描述等于让它盲猜。

## 3. 类型与 schema

参数 schema 由 pydantic 从函数签名推导，以下类型均可使用：

- 基础类型：`str`、`int`、`float`、`bool`、`None`
- 容器：`list[T]`、`dict[K, V]`、`tuple[...]`
- 字面量与枚举：`Literal[...]`、`Enum`
- 组合：`Optional[T]`、`Union[...]`
- 结构化：pydantic `BaseModel` 及其嵌套

参数有默认值即为可选，没有默认值即为必填。以下签名会直接报 `ToolDefinitionError`：

| 写法 | 原因 |
| --- | --- |
| 参数缺少类型注解 | 会退化成 `Any`，schema 无约束 |
| `*args` | 模型无法稳定表达可变位置参数 |
| `**kwargs` | 模型无法枚举动态键 |
| 注解无法解析 | 前向引用找不到对应类型 |
| 类型不受 pydantic 支持 | 生成不出 schema |

参数模型开启 `extra="forbid"`，多传字段会被判为参数校验失败并回填，模型可据此纠正。

## 4. 执行与返回值

调用路径固定为「原始 JSON 字符串 → 参数校验 → 执行 → 文本」。返回值按下表归一：

| handler 返回 | 回填文本 |
| --- | --- |
| `None` | `ok` |
| `str` | 原样返回 |
| pydantic `BaseModel` | `model_dump_json()` |
| 其它对象 | `json.dumps(..., ensure_ascii=False, default=str)` |

参数校验失败不抛异常，回填单行文本：

```text
参数校验失败：city: Input should be a valid string
```

handler 抛异常同样不抛，回填：

```text
工具执行失败：[RuntimeError] 下游超时
```

堆栈只进日志，不回填给模型。

## 5. 同步与异步

```python
@tool
async def fetch_page(url: str) -> str:
    """异步抓取页面。"""
    return await client.get(url)
```

- `async def` handler 直接 `await`。
- 普通 `def` handler 走 `asyncio.to_thread`，不会阻塞事件循环。
- 工具内部可以放心使用同步 IO、文件读写与 CPU 计算，但要记得它们运行在线程池中。

## 6. 定义期错误清单

| 场景 | 异常 |
| --- | --- |
| 函数没有名字 | `ToolDefinitionError` |
| 缺少 docstring 或正文为空 | `ToolDefinitionError` |
| 参数缺注解 | `ToolDefinitionError` |
| 使用 `*args` / `**kwargs` | `ToolDefinitionError` |
| 注解无法解析 | `ToolDefinitionError` |
| 参数类型生成不出 schema | `ToolDefinitionError` |

## 7. ToolRegistry

### 7.1 API

```python
registry = ToolRegistry()
registry.register(get_weather, translate)   # 可一次传多个
registry.names                              # ("get_weather", "translate")
len(registry)                               # 2
registry.declarations()                     # 给 LLM 的工具声明列表
await registry.execute(call)                # 执行一次 ToolCall，返回回填文本
```

| 特性 | 约定 |
| --- | --- |
| 显式实例 | 没有全局注册表，测试之间不会互相污染 |
| 注册顺序 | 即声明顺序，`declarations()` 按此输出 |
| 重名 | 注册期抛 `ToolDefinitionError`，不覆盖 |
| 运行期只读 | 不加锁；约定装配期注册完、运行期不再写 |
| 声明结构 | 复用 `core.llm.tool()`，不在本层另拼 dict |

### 7.2 执行语义

`execute(call: ToolCall) -> str` 的规则：

| 环节 | 行为 |
| --- | --- |
| 未知工具名 | 抛 `ToolNotFoundError`，不静默回填 |
| 参数校验失败 | 回填 `参数校验失败：字段: 原因；...` |
| handler 异常 | 回填 `工具执行失败：[异常类型] 消息`，不含堆栈 |
| 多个 `tool_calls` | 由调用方逐个执行，层内不并发 |
| 有副作用工具 | 顺序执行，回填顺序与 `tool_calls` 对齐 |

为什么未知工具名要抛：模型编造一个不存在的工具名属于调用方契约破坏，应尽早暴露；参数错误与执行失败则属于模型可自行纠正的输入问题，回填更省一次往返。

### 7.3 执行日志

每次执行记一条 DEBUG 日志：

```text
工具=get_weather 耗时毫秒=12.3 成功=true
```

失败时改为 `成功=false` 并带 `错误类型`。参数与返回值一律不记，避免把手机号、密钥、文件内容复制进日志。
