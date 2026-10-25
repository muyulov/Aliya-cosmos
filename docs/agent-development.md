# Cosmos Agent 开发文档

- 适用版本：`core/agent`（工具 / Skill / MCP）
- 面向读者：在 aliya-cosmos 上开发 Agent 能力的工程师
- 相关设计：[Agent 工具层设计](plans/2026-10-25-agent-tool-layer-design.md)

本文说明 `core/agent` 的定位、接口、约定与开发流程，帮助你在不读源码的情况下正确扩展工具、Skill 与 MCP 接入，并写出可控的 ReAct 循环。

---

## 1. 定位与能力边界

`core/agent` 解决一件事：把普通函数变成模型能选、能调、结果能回填的工具，并把系统提示、Skill 索引与 MCP 工具收进同一套注册表。

| 本层负责 | 本层不负责 |
| --- | --- |
| `@tool` 声明与参数 schema 推导 | ReAct 循环（何时继续、最多几轮） |
| 工具注册、声明导出、执行回填 | 会话历史、记忆、上下文裁剪 |
| `system_prompt()` 分段拼装 | 提示词模板引擎与长度裁剪 |
| Skill 扫描、索引、按需读取 | 执行 Skill 附带的脚本 |
| MCP 建连、工具映射、结果归一 | MCP resources / prompts / sampling / OAuth |
| 统一的 `AgentError` 错误树 | 工具权限、人工审批、危险操作拦截 |

一句话：本层提供「零件」，Agent 的「循环」由调用方组装。

---

## 2. 目录与依赖方向

```text
core/agent/
  __init__.py    对外出口
  errors.py      错误树
  tool.py        Tool + @tool + 返回值归一
  registry.py    ToolRegistry：register / declarations / execute
  prompt.py      system_prompt(*sections)
  skill.py       SkillLibrary：扫描 data/skills、索引、load_skill
  mcp.py         MCPConfig / MCPServerSettings / MCPManager

data/skills/<name>/SKILL.md   技能目录（可提交）
data/mcp.json                 MCP server 清单（可提交，密钥用占位符）
```

依赖方向：

```text
agent → (config, llm, logger)
```

- 依赖 `config` 只为复用占位符插值（`read_env` / `interpolate`），不新增配置模型。
- `llm` 不知道 `Tool` 的存在，只接受 `ChatCompletionToolParam`。
- 本层不进 `ServiceManager`，生命周期由调用方用 `async with` 掌握。
- `data/skills` 与 `data/mcp.json` 的路径相对当前工作目录解析。

---

## 3. 快速上手

最小可运行示例：

```python
from core.agent import SkillLibrary, ToolRegistry, system_prompt, tool
from core.llm import assistant, tool_result, user


@tool
def get_weather(city: str, unit: str = "celsius") -> str:
    """查询指定城市的当前天气。

    Args:
        city: 城市名，如「上海」。
        unit: 温度单位，celsius 或 fahrenheit。
    """
    return f"{city} 晴 26℃"


library = SkillLibrary.load()
registry = ToolRegistry()
registry.register(get_weather, *library.tools())

messages = [system_prompt("你是行程助手。", library.index()), user("上海天气？")]
reply = await llm.chat_tools(messages, registry.declarations())

for call in reply.tool_calls:
    messages.append(tool_result(call.id, await registry.execute(call)))
```

这段代码只完成了一轮工具调用。要让 Agent 继续推理，需要把 assistant 消息与工具结果追加回历史，再发起下一轮，见第 9 节。

---

## 4. 工具开发

### 4.1 用 `@tool` 声明

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

### 4.2 docstring 规范

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

### 4.3 类型与 schema

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

### 4.4 执行与返回值

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

### 4.5 同步与异步

```python
@tool
async def fetch_page(url: str) -> str:
    """异步抓取页面。"""
    return await client.get(url)
```

- `async def` handler 直接 `await`。
- 普通 `def` handler 走 `asyncio.to_thread`，不会阻塞事件循环。
- 工具内部可以放心使用同步 IO、文件读写与 CPU 计算，但要记得它们运行在线程池中。

### 4.6 定义期错误清单

| 场景 | 异常 |
| --- | --- |
| 函数没有名字 | `ToolDefinitionError` |
| 缺少 docstring 或正文为空 | `ToolDefinitionError` |
| 参数缺注解 | `ToolDefinitionError` |
| 使用 `*args` / `**kwargs` | `ToolDefinitionError` |
| 注解无法解析 | `ToolDefinitionError` |
| 参数类型生成不出 schema | `ToolDefinitionError` |

---

## 5. ToolRegistry

### 5.1 API

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

### 5.2 执行语义

`execute(call: ToolCall) -> str` 的规则：

| 环节 | 行为 |
| --- | --- |
| 未知工具名 | 抛 `ToolNotFoundError`，不静默回填 |
| 参数校验失败 | 回填 `参数校验失败：字段: 原因；...` |
| handler 异常 | 回填 `工具执行失败：[异常类型] 消息`，不含堆栈 |
| 多个 `tool_calls` | 由调用方逐个执行，层内不并发 |
| 有副作用工具 | 顺序执行，回填顺序与 `tool_calls` 对齐 |

为什么未知工具名要抛：模型编造一个不存在的工具名属于调用方契约破坏，应尽早暴露；参数错误与执行失败则属于模型可自行纠正的输入问题，回填更省一次往返。

### 5.3 执行日志

每次执行记一条 DEBUG 日志：

```text
工具=get_weather 耗时毫秒=12.3 成功=true
```

失败时改为 `成功=false` 并带 `错误类型`。参数与返回值一律不记，避免把手机号、密钥、文件内容复制进日志。

---

## 6. 系统提示拼装

```python
from core.agent import system_prompt

message = system_prompt("你是行程助手。", library.index(), "当前用户在上海。")
```

| 规则 | 说明 |
| --- | --- |
| 去空白 | 每段去掉首尾空白 |
| 跳过空段 | `None` 与纯空白段直接略过 |
| 空行连接 | 非空段之间插入一个空行 |
| 全空报错 | 所有段都为空时抛 `PromptError` |
| 不做模板 | 变量由调用方 f-string 填好再传 |
| 不裁剪 | 上下文长度属会话层 |

返回值就是 SDK 的 system 消息，可直接放进 `messages`。

---

## 7. Skill

### 7.1 目录与格式

固定目录 `data/skills`，只扫描一级子目录：

```text
data/skills/
  pdf/
    SKILL.md
  docx/
    SKILL.md
```

`SKILL.md` 必须以 YAML frontmatter 开头，`name` 与 `description` 必填：

```markdown
---
name: pdf
description: 处理 PDF 文件：抽取文本、合并拆分、填表单
---

# 使用说明

1. 先读取文件元信息。
2. 再按页抽取文本。
```

| 项 | 规则 |
| --- | --- |
| 必填字段 | `name`、`description`；其它字段忽略 |
| 正文 | frontmatter 之后的全部内容，加载时读进内存 |
| 缺失或非法 frontmatter | 加载期抛 `SkillError`，带文件路径 |
| 重名 | 加载期抛 `SkillError` |
| 目录不存在 | 按空库处理，仅 WARNING |
| 脚本与附件 | 不读取、不执行，只认 `SKILL.md` 正文 |

### 7.2 渐进式披露

`SkillLibrary` 只把「名称 + 一句话描述」放进系统提示，正文由模型调用内置工具 `load_skill(name)` 按需读取：

```python
library = SkillLibrary.load()

library.index()
# 可用 skill：需要时调用 load_skill 读取完整说明。
# - docx：编辑 Word 文档
# - pdf：处理 PDF 文件：抽取文本、合并拆分、填表单

library.tools()   # [load_skill]
```

空库时 `index()` 返回空串，`tools()` 返回空列表，`system_prompt` 会自动跳过空段，因此零配置也能启动。

### 7.3 使用方式

```python
registry = ToolRegistry()
registry.register(*library.tools())
```

模型读到索引后调用 `load_skill`，注册表把正文回填给它。名字不存在时回填：

```text
工具执行失败：[SkillNotFoundError] 未找到 skill：nope
```

模型可据此改成正确名字，无需中断整轮对话。

---

## 8. MCP

### 8.1 配置文件

固定读取 `data/mcp.json`，沿用生态通用的 `mcpServers` 结构：

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "./"]
    },
    "remote": {
      "type": "http",
      "url": "http://127.0.0.1:3001/mcp",
      "headers": {"Authorization": "Bearer ${MCP_TOKEN:}"},
      "timeout": 60
    }
  }
}
```

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `type` | `stdio` | `stdio` 或 `http`（streamable HTTP） |
| `command` / `args` / `env` | 空 | `stdio` 必填 `command`；`env` 与进程环境合并 |
| `url` / `headers` | 空 | `http` 必填 `url` |
| `timeout` | `60` | 建连 + initialize + list_tools 的总上限（秒） |

- 占位符复用 YAML 规则：`${VAR}`、`${VAR:默认}`、`$$`，取值来自进程环境与 `.env`。
- 文件不存在按未配置处理，仅 WARNING。
- JSON 非法、顶层不是对象、缺必填字段、占位符缺值时抛 `MCPConfigError`。

### 8.2 生命周期

```python
from core.agent import MCPManager, ToolRegistry

registry = ToolRegistry()

async with MCPManager.from_file() as mcp:
    registry.register(*mcp.tools)
    # 在这里跑 Agent 循环
```

| 阶段 | 行为 |
| --- | --- |
| `start()` | 并发建立全部 server 连接并等待就绪 |
| `tools` | 返回启动时缓存的 `tuple[Tool, ...]` |
| `stop()` | 断开全部连接，幂等 |
| 建连失败 | 关闭已连 server，抛 `MCPConnectionError` |
| 建连超时 | 抛 `MCPTimeoutError`（`MCPConnectionError` 子类） |

任一 server 失败不会静默跳过，避免工具无声消失、模型反复调用不存在的工具。

### 8.3 工具映射

| 维度 | 规则 |
| --- | --- |
| 名称 | `{server}__{tool}`，非法字符替换为 `_`，超过 64 字符截断并 WARNING |
| 描述 | 取 server 返回的 `description`，为空时回退到工具名 |
| 参数 schema | `inputSchema` 原样透传 |
| 参数校验 | 只要求是合法 JSON 对象，schema 校验交给 server |
| 调用异常 | 交给注册表统一回填 |

工具名建议使用英文，中文 server 名会被整段替换成下划线。

### 8.4 结果归一

| MCP 内容 | 回填文本 |
| --- | --- |
| `TextContent` | 正文 |
| `EmbeddedResource` 文本 | 资源正文 |
| `ImageContent` | `[图片 mime]` |
| 其它资源 | `[资源 uri]` |
| 无文本但有 `structuredContent` | JSON |
| 全部为空 | `ok` |
| `isError` | 前缀 `工具执行失败：` |

### 8.5 测试注入

`MCPManager.from_file(connector=...)` 支持注入内存连接工厂，测试无需真实子进程或远端端点：

```python
manager = MCPManager.from_file(connector=fake_connector)
```

生产代码不要传 `connector`，保持默认实现。

---

## 9. 编写 ReAct 循环

本层只负责单轮工具执行，循环由调用方写。一个带轮数上限的实现：

```python
from core.agent import MCPManager, SkillLibrary, ToolRegistry, system_prompt, tool
from core.llm import assistant, tool_result, user

MAX_ROUNDS = 8


async def run_agent(llm, question: str) -> str:
    library = SkillLibrary.load()
    registry = ToolRegistry()
    registry.register(get_weather, *library.tools())

    async with MCPManager.from_file() as mcp:
        registry.register(*mcp.tools)

        messages = [
            system_prompt("你是行程助手。", library.index()),
            user(question),
        ]

        for _ in range(MAX_ROUNDS):
            reply = await llm.chat_tools(messages, registry.declarations())
            if not reply.tool_calls:
                return reply.content or ""

            messages.append(assistant(reply.content, tool_calls=reply.tool_calls))
            for call in reply.tool_calls:
                result = await registry.execute(call)
                messages.append(tool_result(call.id, result))

        return "达到最大轮数，未得到最终答案。"
```

要点：

| 关注点 | 做法 |
| --- | --- |
| 继续条件 | `reply.tool_calls` 非空才继续，否则返回正文 |
| 历史回填 | 先追加携带 `tool_calls` 的 assistant 消息，再逐条追加 `tool_result` |
| 顺序执行 | 按 `reply.tool_calls` 顺序调用 `execute` |
| 轮数上限 | 必须设置，防止模型无限调用工具 |
| 失败处理 | 参数错误与执行失败已回填，通常无需中断；未知工具名会抛 `ToolNotFoundError` |
| 资源释放 | MCP 用 `async with`，确保循环结束后连接关闭 |

需要在工具之间做并发时，由调用方自己 `asyncio.gather`，并自行承担副作用顺序与回填顺序的风险。

---

## 10. 错误处理

所有异常继承 `AgentError`，调用方一次 `except AgentError` 即可兜住整层：

```python
from core.agent import AgentError

try:
    result = await registry.execute(call)
except AgentError as exc:
    log.error("Agent 层错误", 错误类型=type(exc).__name__, 消息=str(exc))
```

| 类型 | 触发条件 |
| --- | --- |
| `AgentError` | 基类 |
| `ToolDefinitionError` | docstring 缺失、参数缺注解、`*args` / `**kwargs`、重名注册 |
| `ToolNotFoundError` | 执行了未注册的工具名 |
| `PromptError` | `system_prompt()` 的所有分段都为空 |
| `SkillError` | frontmatter 缺失或非法、缺字段、重名、文件读不动 |
| `SkillNotFoundError` | `load_skill()` 传了不存在的名字（`SkillError` 子类） |
| `MCPConfigError` | `data/mcp.json` 读不动、JSON 非法、字段不合法、占位符缺值 |
| `MCPConnectionError` | 建连、initialize 或 list_tools 失败 |
| `MCPTimeoutError` | 上述三步超时（`MCPConnectionError` 子类） |

区分两类失败：

| 类别 | 处理方式 | 例子 |
| --- | --- | --- |
| 输入问题 | 回填文本，让模型自我纠正 | 参数类型不对、skill 名写错、工具内部报错 |
| 编程或配置问题 | 抛异常，fail fast | 工具重名、缺 docstring、MCP 配置非法、调用未知工具 |

本层没有 `ToolExecutionError`：工具执行失败属于可回填的输入问题，不上报到调用方。

---

## 11. 日志与可观测性

| 事件 | 级别 | 字段 |
| --- | --- | --- |
| 工具执行完成 | DEBUG | `工具`、`耗时毫秒`、`成功` / `错误类型` |
| Skill 加载完成 | DEBUG | `数量` |
| Skill 目录不存在 | WARNING | `目录` |
| MCP 连接完成 | INFO | `数量`、`工具数` |
| MCP 工具名截断 | WARNING | `工具` |
| MCP 配置缺失 | WARNING | `文件` |

约定：

- 不记录工具参数与返回值。
- 不打印 Skill 正文与 MCP 返回内容。
- 本层不是 `Service`，没有 `self.log` 前缀；需要业务维度时由调用方通过 `log.context(...)` 补充。

---

## 12. 测试与质量门禁

项目统一使用 uv + pytest：

```bash
# 全量测试
uv run pytest

# 覆盖率
uv run pytest --cov=core

# 代码检查
uv run ruff check .

# 格式检查
uv run ruff format --check .
```

Agent 层测试位于 `tests/agent/`：

| 文件 | 覆盖内容 |
| --- | --- |
| `test_tool.py` | schema 推导、docstring 解析、同步/异步执行、返回值归一、定义期错误 |
| `test_registry.py` | 注册顺序、重名、未知工具、校验失败与异常回填 |
| `test_prompt.py` | 空段跳过、空行连接、全空报错 |
| `test_skill.py` | 扫描、frontmatter 校验、索引、按需读取、目录不存在 |
| `test_mcp.py` | 配置解析、连接生命周期、工具映射、结果归一、超时与回滚 |

写新工具时建议同时补一条测试，至少覆盖：

1. `declarations()` 里的名称、描述与必填字段是否正确。
2. 正常输入的回填文本。
3. 参数非法时的回填文本。
4. handler 抛异常时不含堆栈。

MCP 相关测试优先注入 `connector` 或假会话，避免依赖 `npx` 与网络；真实 stdio 端到端测试标记为手动验收。

---

## 13. 扩展点与常见坑

### 新增一个本地工具

1. 在业务模块里写带完整类型注解与 docstring 的普通函数。
2. 加上 `@tool`。
3. 在装配处 `registry.register(my_tool)`。
4. 补测试。

### 新增一个 Skill

1. 新建 `data/skills/<name>/SKILL.md`。
2. 写 frontmatter 的 `name` 与 `description`。
3. 正文写操作步骤，模型通过 `load_skill` 读取。
4. 重启后 `SkillLibrary.load()` 自动发现。

### 接入一个 MCP server

1. 在 `data/mcp.json` 的 `mcpServers` 下增加条目。
2. 密钥写占位符，真实值放 `.env`。
3. 用 `async with MCPManager.from_file() as mcp` 启动并注册 `mcp.tools`。
4. 确认工具名前缀符合预期。

### 常见坑

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 模型选错工具 | docstring 太笼统 | 写清适用场景与参数含义 |
| 模型传错参数 | 参数缺注解或描述为空 | 补类型注解与 `Args:` 段 |
| 工具调用卡住其他请求 | 同步 handler 里做了长时间阻塞 | 改成 `async def` 或拆小粒度 |
| 工具名重复 | 两个函数同名，或 MCP 前缀撞名 | 注册期会报错，重命名后再注册 |
| Skill 没出现在提示里 | frontmatter 缺字段或目录层级不对 | 确认 `data/skills/<name>/SKILL.md` |
| MCP 工具消失 | 某个 server 建连失败 | 本层会抛错回滚，检查 `data/mcp.json` 与网络 |
| 循环停不下来 | 没设轮数上限 | 设置 `MAX_ROUNDS` 并在无 `tool_calls` 时退出 |

---

## 14. 明确不做的事

以下能力不在本层范围内，需要时由调用方按需叠加：

- ReAct 循环本体与最大轮数策略
- 会话历史、记忆、上下文裁剪
- 流式工具执行与流式输出
- 多工具并行执行与单工具超时
- 自动重试、降级与熔断
- MCP 的 resources / prompts / sampling / roots、动态工具刷新、OAuth、旧 SSE
- Skill 附带脚本执行与 Skill 相互引用
- 工具权限、人工审批、危险操作拦截

这些能力的取舍各不相同，放在调用方能保持本层最小且可预测。
