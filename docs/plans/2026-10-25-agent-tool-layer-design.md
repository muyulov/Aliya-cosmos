# Agent 工具层（工具 / Skill / MCP）设计

- 日期：2026-10-25（v2，增补 Skill 与 MCP）
- 状态：设计已定（brainstorming 逐节确认）
- 目标：在现有 `service / logger / config / llm / embedding / decision` 之外，新增 `core/agent/`，
  把「工具怎么声明、怎么交给模型、怎么执行回填」「Skill 怎么按需披露」「MCP server 怎么接入」
  以及「system 提示怎么拼」收成一个小库；ReAct 循环仍由调用方写。

---

## 0. 决策速览

| # | 议题 | 决策 |
| --- | --- | --- |
| 1 | 能力边界 | **只做工具层**：工具注册表 + 提示拼装 + Skill + MCP；循环由调用方写 |
| 2 | 工具声明 | **装饰器 + 类型注解**（`@tool`），schema 由 pydantic 从签名推导 |
| 3 | 提示拼装 | **分段拼 system**：去空白、跳空段、`\n\n` 连接，无模板引擎 |
| 4 | 失败语义 | **全部回填**：校验失败与 handler 异常都转文本回给模型；只有编程错误才抛 |
| 5 | 形态 | **纯库**，不继承 `Service`、不进 `ServiceManager` |
| 6 | 位置 | `core/agent/`（`pyproject` 的打包范围是 `core`） |
| 7 | 注册表作用域 | **显式实例**，无模块级全局注册表 |
| 8 | 并发 | 一次多个 `tool_calls` 由调用方逐个执行；层内不并发 |
| 9 | Skill 形态 | **`data/skills/<name>/SKILL.md` + 渐进式披露**（索引进 system，正文由 `load_skill` 工具读） |
| 10 | Skill 路径 | **固定 `data/skills`**，不开放配置 |
| 11 | MCP 生命周期 | **`MCPManager` + `async with`**，工具并入同一个 `ToolRegistry` |
| 12 | MCP 传输 | **stdio + streamable HTTP**（不做旧 SSE） |
| 13 | MCP 配置 | **`data/mcp.json`**（标准 `mcpServers` 结构），不进 YAML |
| 14 | 日志口径 | DEBUG 级记 工具 / 耗时 / 成败 / 错误类型，**不记参数与返回值** |

---

## 1. 定位与边界

这个层解决一件事：**把「工具」从普通函数变成模型能选、能调、结果能回填的东西，把系统提示从散落的字符串变成一条消息。**

它**是**：

- 工具声明层：`@tool` 把带类型注解的函数推导成 `Tool`（名称 / 描述 / JSON Schema / 执行入口）。
- 工具注册表：按注册顺序导出 `ChatCompletionToolParam[]`，按 `ToolCall` 执行并归一成文本。
- Skill 库：扫描固定目录，把「有哪些 skill」注入 system，正文按需由模型取用。
- MCP 客户端：连接 stdio / HTTP server，把远端工具映射成本地 `Tool`。
- 提示拼装器：把若干段文本合成一条 system 消息。

它**不是**：

- Agent 循环（「模型要工具 → 执行 → 回填 → 再问」的 while）——由调用方写。
- 会话管理（历史消息、裁剪、记忆）——本次不做。
- 配置层——Skill 与 MCP 各自读固定路径的文件，不进 `Settings`。
- 服务层——不接 `ServiceManager`，生命周期由调用方用 `async with` 掌握。

---

## 2. 目录与依赖方向

```text
core/agent/
  __init__.py    出口（Tool / ToolRegistry / MCPManager / SkillLibrary / tool / system_prompt / 错误）
  errors.py      错误树
  tool.py        Tool + @tool（签名 → pydantic 模型 → JSON Schema）+ 返回值归一
  registry.py    ToolRegistry：register / declarations / execute
  prompt.py      system_prompt(*sections)
  skill.py       Skill / SkillLibrary（扫描 data/skills、索引、load_skill）
  mcp.py         MCPConfig / MCPServerSettings / MCPManager（stdio + http）

data/skills/<name>/SKILL.md   技能目录（可提交）
data/mcp.json                 MCP server 清单（可提交，密钥走 ${VAR}）
```

- 依赖方向：`agent → (config, llm, logger)`。用到 `config` 只为复用占位符插值
  （`read_env` / `interpolate`），不新增配置模型。
- 不被 `llm` 依赖：`llm` 仍只认 `ChatCompletionToolParam`，不知道 `Tool` 的存在。
- `core/service/registry.py` **不改**：本层不是服务。

---

## 3. 工具模型（`tool.py`）

### 3.1 `Tool`

```python
@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    parameters: Mapping[str, object]              # 完整 JSON Schema（object）
    invoke: Callable[[str], Awaitable[str]]       # 原始 arguments JSON → 回填文本
    func: Callable[..., object] | None = None     # 仅本地工具；__call__ 透传
```

为什么执行入口收成 `invoke(str) -> str`：本地工具来自 pydantic 签名，MCP 工具来自第三方
`inputSchema`，两者的校验与归一方式不同；统一到「原始 JSON 进、文本出」后，注册表只认一条执行路径。

`__call__` 透传 `func`（MCP 工具没有，调用即 `TypeError`），本地工具装饰后仍能直接调用，便于测试。

### 3.2 `@tool` 的推导规则

| 项 | 规则 |
| --- | --- |
| 名字 | 取函数 `__name__`，不支持改名（重名在注册期报错） |
| 描述 | docstring 去掉 `Args:` / `参数:` 段后的正文，归一化缩进与首尾空白；**没有 docstring 直接抛 `ToolDefinitionError`** |
| 参数模型 | `pydantic.create_model` 按签名建；默认值原样保留（有默认值即可选） |
| 参数描述 | 解析 docstring 的 `Args:` / `参数:` 段，逐行 `名字: 描述`，支持续行缩进；解析不到就省略 `description` |
| 支持注解 | pydantic 能处理的即可：`str` / `int` / `float` / `bool` / `None` / `list` / `dict` / `Literal` / `Enum` / `BaseModel` / `Optional` / `Union` |
| 非法签名 | `*args` / `**kwargs` / 参数缺注解 → `ToolDefinitionError` |

缺注解即报错：pydantic 会把无注解参数当 `Any`，生成的 schema 没有约束，模型只能靠猜；
在定义期失败比上线后调试「模型老传错参数」便宜得多。

描述来自 docstring：它是模型选工具的唯一依据，放在函数定义旁边与服务代码一起演进，
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

| 约定 | 说明 |
| --- | --- |
| 只收 `Tool` | 不收裸函数，避免「这里推导、那里不推导」的歧义；装饰器与 MCP manager 是仅有的两个来源 |
| 重名即错 | 同名覆盖会让模型看到两个同名工具或悄悄换掉实现，注册期直接抛 `ToolDefinitionError` |
| 顺序即声明顺序 | `declarations()` 按注册顺序输出，便于预测提示里的工具排列 |
| 复用 `core.llm.tool()` | 声明结构只由 LLM 层定义一次，本层不自己拼 dict |
| 显式实例 | 无模块级全局注册表：隐式共享状态会让测试互相污染 |
| 不加锁 | 约定装配期注册完、运行期只读（写进模块 docstring） |
| 未知工具名 | `execute()` 抛 `ToolNotFoundError`：模型编一个不存在的工具名属调用方契约破坏，不静默回填 |

---

## 5. 提示拼装（`prompt.py`）

```python
def system_prompt(*sections: str | None) -> ChatCompletionSystemMessageParam
```

- 每段去首尾空白；`None` 与纯空白段一律跳过（调用方可以放心写
  `system_prompt(角色, library.index(), 上下文)`）。
- 非空段用空行（`\n\n`）连接——段落之间留白，模型对分段的感知更好。
- 全部为空抛 `PromptError`：没有内容的 system 消息只会白占 token。
- **不做模板插值**：变量由调用方 f-string 填好再传进来，不引入第二套字符串语法。
- **不做长度裁剪**：裁剪与会话历史绑定，属会话层。
- 返回值直接是 SDK 的 system 消息，拿到就能放进 `messages`。

---

## 6. Skill（`skill.py`）

### 6.1 目录与格式

固定根目录 `data/skills`（模块常量 `SKILLS_ROOT`，相对工作目录），只扫描**一级子目录**：

```text
data/skills/pdf/SKILL.md
data/skills/docx/SKILL.md
```

```markdown
---
name: pdf
description: 处理 PDF 文件：抽取文本、合并拆分、填表单
---

# 使用说明
具体步骤……
```

| 项 | 规则 |
| --- | --- |
| 必填字段 | `name`、`description`；其余字段（`license`、`allowed-tools` 等）忽略 |
| 正文 | frontmatter 之后的全部内容，扫描时读进内存（一个 skill 通常几 KB） |
| 缺失/非法 frontmatter | `SkillError`，带文件路径——配置错误不该静默变成「没有这个 skill」 |
| 重名 | `SkillError` |
| 目录不存在 | 空库 + WARNING（`data/skills` 是可选目录，零配置也能跑） |
| 目录里的脚本/附件 | **不执行、不读取**，只认 SKILL.md 正文（安全边界） |

路径固定而不做成配置：技能来源可预期，部署时不会因为某处写了个绝对路径就去扫描仓库外目录。

### 6.2 `SkillLibrary`

```python
library = SkillLibrary.load()     # 默认 data/skills；测试可显式传根目录
library.index()                   # 供拼进 system；空库返回 ""，system_prompt 自动跳过
library.tools()                   # [load_skill] 内置工具
```

`index()` 输出：

```text
可用 skill：需要时调用 load_skill 读取完整说明。
- pdf：处理 PDF 文件：抽取文本、合并拆分、填表单
- docx：编辑 Word 文档
```

`load_skill(name)` 的 handler 返回正文；名字不存在时抛 `SkillNotFoundError`，由注册表按既有规则
回填成 `工具执行失败：[SkillNotFoundError] 未找到 skill：xxx`，模型看到后能改成正确的名字。

---

## 7. MCP（`mcp.py`）

### 7.1 配置：`data/mcp.json`

标准 `mcpServers` 映射，server 名即键（天然去重）：

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "./"],
      "env": {}
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
| `type` | `stdio` | `stdio` / `http`（streamable HTTP） |
| `command` / `args` / `env` | 空 | `stdio` 必填 `command`；`env` 会与继承的进程环境合并后传给子进程 |
| `url` / `headers` | 空 | `http` 必填 `url` |
| `timeout` | `60` | **建连 + initialize + list_tools** 的上限（秒）；`npx` 首次下载可能很慢，给足 |

- 占位符走现有的 `read_env` + `interpolate`：`${VAR}` / `${VAR:默认}` / `$$` 语义与 YAML 完全一致。
- 文件不存在 → 空 server 列表 + WARNING（MCP 是可选能力）。
- JSON 语法错、顶层结构不对、缺必填字段 → `MCPConfigError`（带文件路径）。
- 仓库提交一份 `data/mcp.json`（`{"mcpServers": {}}`），避免「文件不在」的噪声警告，同时示范结构。

为什么用 JSON 而不是加进 `cosmos.yaml`：`mcpServers` 是生态里通用的交换格式（Claude Desktop 等），
直接放 JSON 才能把别人给的配置粘过来用；本层也不需要 YAML 的层级与注释。

### 7.2 连接模型

```python
async with MCPManager.from_file() as mcp:
    registry.register(*mcp.tools)
```

- **每个 server 一个后台任务**：任务内 `async with stdio_client(...)` 或 `streamable_http_client(...)`，
  再 `ClientSession` → `initialize()` → `list_tools()`，把 session 与工具摘要存起来，置位 ready 事件。
- `start()` 等全部 ready，受该 server 的 `timeout` 限制；任一失败则**关闭已连的**、取消其余任务，
  抛 `MCPConnectionError`——不静默跳过坏 server，否则工具消失得无声无息。
- `tools` 是同步属性，返回启动时缓存的 `tuple[Tool, ...]`；`stop()` 置停止事件 + 兜底取消，幂等。
- 为什么必须用任务包裹：SDK 的传输是 `async with` 作用域，作用域一退连接就断；
  把「连接存活期」变成显式生命周期，`start` / `stop` 才有落点。

### 7.3 工具映射

| 维度 | 规则 |
| --- | --- |
| 名称 | `{server}__{tool}`；非 `[A-Za-z0-9_-]` 的字符逐个替换为 `_`（server 名用中文会整段变下划线，建议配置里用英文名），超 64 字符截断并 WARNING；撞名由注册表抛 `ToolDefinitionError` |
| 描述 | server 给的 `description`，空则回退到工具名 |
| 参数 schema | `inputSchema` **原样**放进 `Tool.parameters`，不转 pydantic |
| 参数校验 | 只要求 JSON 可解析且是对象；**不引 `jsonschema`**，schema 校验交给 server |
| 结果归一 | 收集 `TextContent.text` 与 `EmbeddedResource` 文本；`ImageContent` → `[图片 mimeType]`；`BlobResourceContents` → `[资源 uri]`；无文本但有 `structuredContent` → JSON；全空 → `ok` |
| `isError` | 前缀 `工具执行失败：` 后回填 |
| 调用异常 | 交给注册表统一回填 `工具执行失败：[类型] 消息` |

---

## 8. 执行语义

```python
async def execute(self, call: ToolCall) -> str
```

流程：查表 → `await tool.invoke(call.arguments)` → 异常转文本。

| 环节 | 规则 |
| --- | --- |
| 查表 | 未注册 → 抛 `ToolNotFoundError` |
| 参数解析（本地工具） | `args_model.model_validate_json(arguments)`；失败 → 回填 `参数校验失败：<字段: 原因>; …`（单行摘要，不贴整段 pydantic 多行报告） |
| 无参工具 | 忽略 `arguments`（模型常传 `{}` 或空串）；传了非空内容也不报错 |
| 调用（本地工具） | 异步 handler 直接 `await`；同步 handler 在装饰期包成 `asyncio.to_thread`，不阻塞事件循环 |
| 异常 | 回填 `工具执行失败：[<异常类型>] <消息>`，**不附堆栈** |
| 返回值 | `None` → `"ok"`；`BaseModel` → `model_dump_json()`；`str` → 原样；其余 → `json.dumps(ensure_ascii=False, default=str)` |

为什么两类失败都回填：模型看到「参数校验失败」或「工具执行失败」后通常能自己改参数或换一条路，
比整轮中断、由调用方重试更省一次往返；堆栈对模型没有信息量，只进日志。

为什么同步 handler 要下线程：工具里常见的 `requests`、文件读、CPU 计算都是阻塞调用，
直接在事件循环里跑会卡住所有并发请求。

为什么不做并发：`tool_calls` 里可能有写文件、发消息这类有副作用的调用，顺序执行行为才可预测，
且回填顺序与 `tool_calls` 天然对齐。需要并发由调用方自己 `gather`。

---

## 9. 错误树（`errors.py`）

| 类型 | 触发条件 |
| --- | --- |
| `AgentError` | 基类，调用方一次 `except` 兜住整层 |
| `ToolDefinitionError` | 无 docstring / `*args`、`**kwargs` / 参数缺注解 / 重名注册 |
| `ToolNotFoundError` | `execute()` 查了没注册的工具名 |
| `PromptError` | `system_prompt()` 的所有分段都为空 |
| `SkillError` | frontmatter 缺失或非法、缺 `name`/`description`、重名、文件读不动 |
| `SkillNotFoundError` | `load_skill()` 传了不存在的名字（由注册表回填，不向上抛） |
| `MCPConfigError` | `data/mcp.json` 读不动 / JSON 非法 / 结构或字段不合法 / 占位符缺失 |
| `MCPConnectionError` | 建连 / `initialize` / `list_tools` 失败 |
| `MCPTimeoutError` | 上述三步超时（`MCPConnectionError` 子类） |

- 没有 `ToolExecutionError`：handler 异常与校验失败都被转成回填文本，不属于本层上报的错误。
- 抛出的都是「调用方写错了」或「部署配置错了」这一类，fail fast，不做兜底。

---

## 10. 日志

- DEBUG 级，一次执行一条：`工具` / `耗时毫秒` / `成功`（或 `错误类型`）。
- **不记参数与返回值**：工具参数里常有手机号、密钥、文件内容，日志不该是它们的第二份副本。
- MCP 与 Skill 的装配日志：server 名、工具数、耗时；skill 数；不打印正文。
- 不做 `Service` 式 `self.log` 前缀：本层不是服务；需要区分调用方时由调用方在自己的上下文里补字段。

---

## 11. 测试策略

判据与既有各层一致：`uvx basedpyright core tests` 零告警；`pytest --cov=core` **100%**；
`ruff check` / `ruff format --check` 全绿。

- **不打桩 pydantic 与 asyncio**：schema 推导、参数校验、线程切换都真跑。
- `tool.py`：必填 / 默认值 / `Literal` / 嵌套 `BaseModel` / `Optional` 的 schema；`Args:` 段解析（含无该段）；
  同步与异步 handler；同步 handler 在非主线程执行（断言 `threading.current_thread()` 不是主线程）；
  无 docstring、`*args`、`**kwargs`、缺注解四类定义错误；四种返回值归一。
- `registry.py`：声明顺序与结构；重名注册；未知工具名；校验失败回填且不抛；异常回填不含堆栈；
  无参工具忽略空 `arguments`；`names` / `__len__`。
- `prompt.py`：跳空段与 `None` 段；空行连接；全空抛 `PromptError`。
- `skill.py`：`tmp_path` 作根目录。frontmatter 解析、`index()` 文案、`load_skill` 命中/未命中、
  缺字段、重名、目录不存在（空库 + WARNING）。
- `mcp.py`：`MCPManager` 接受可注入的 `connector`，测试用
  `mcp.shared.memory.create_connected_server_and_client_session` 接进程内 FastMCP server，**真跑**
  `initialize` / `list_tools` / `call_tool`。覆盖：前缀命名与 schema 透传、四类结果归一、
  `isError`、调用异常回填、建连失败后已连 server 被关闭、超时、`stop` 幂等、配置文件解析错误。
- 真实 stdio 端到端（`npx` 起 server）标 `skip`，只在手动验收时跑。

---

## 12. 非目标（本次明确不做）

- ReAct 循环本体（何时继续、何时停止、最大轮数）；
- 会话历史、记忆、上下文裁剪；
- 流式工具执行 / 流式输出；
- 多个工具并行执行与超时控制；
- 自动重试与降级；
- MCP 的 resources / prompts / sampling / roots、`tools/list_changed` 动态刷新、OAuth、旧 SSE；
- Skill 附带脚本的执行、skill 之间的相互引用；
- 工具权限、人工审批、危险操作拦截；
- 配置项与服务装配。

这些都能在调用方按需叠加，且各自有独立的取舍，不适合塞进「最基本的能力」。

---

## 13. 用法示例

```python
from core.agent import MCPManager, SkillLibrary, ToolRegistry, system_prompt, tool
from core.llm import LLMService, assistant, tool_result, user


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

async with MCPManager.from_file() as mcp:
    registry.register(*mcp.tools)

    messages = [system_prompt("你是行程助手。", library.index()), user("上海天气？")]
    reply = await llm.chat_tools(messages, registry.declarations())
    for call in reply.tool_calls:
        messages.append(tool_result(call.id, await registry.execute(call)))
```

循环仍由调用方写：拿 `reply.tool_calls` 判断是否继续、把 `assistant(tool_calls=reply.tool_calls)`
连同各条 `tool_result` 追加回 `messages`、决定最多几轮。

---

## 14. 阶段划分

1. **期一（核心）**：`errors` → `tool` → `registry` → `prompt` → `__init__`，测试同步落地。
2. **期二（Skill）**：`skill.py` + `data/skills` 示例 + 测试。
3. **期三（MCP）**：`mcp.py` + `data/mcp.json` + `mcp` 依赖 + 内存传输测试。
4. **收尾**：README 补「Agent 层」章节（工具 / Skill / MCP、配置表、错误表）、目录结构与依赖方向。

---

## 附：事实查证记录（2026-10-25，按 mcp 2.3.0 实测订正）

**MCP Python SDK**

- PyPI 包名 `mcp`，实际安装 2.3.0（v2 稳定线），要求 Python ≥3.10；`FastMCP` 已改名为
  `mcp.server.mcpserver.MCPServer`。
- stdio：`from mcp import ClientSession, StdioServerParameters`、`from mcp.client.stdio import stdio_client`；
  `stdio_client(params)` 是异步上下文管理器，yield `(read, write)` 两个值。
- Streamable HTTP：`from mcp.client.streamable_http import streamable_http_client`；
  v2 只 yield `(read, write)` **两个值**（v1 的第三个 session id 已去掉）；
  自定义 header 先建 `httpx2.AsyncClient` 传 `http_client=`（v2 直接用 httpx2）。
- `ClientSession(read, write)` 是异步上下文管理器，必须先 `await session.initialize()`。
- `list_tools()` → `.tools`，每项字段是 snake_case：`name` / `description` / `input_schema`。
- `call_tool(name, arguments={...})` → `CallToolResult`，字段 `content` / `structured_content` / `is_error`。
- `content` 元素：`TextContent(.text)` / `ImageContent(.data, .mime_type)` /
  `EmbeddedResource(.resource → TextResourceContents | BlobResourceContents)`；另有 `AudioContent`。
- `stdio_client` 的 env 由 SDK 自己合并：`get_default_environment() | server.env`，因此配置里只写增量变量。
- v2 没有 `create_connected_server_and_client_session`；进程内测试用
  `create_client_server_memory_streams` + `MCPServer._lowlevel_server.run(...)` + `ClientSession`，
  可真跑 `initialize` / `list_tools` / `call_tool`。

**Skill 目录约定**

- `SKILL.md` 用 YAML frontmatter 声明 `name` / `description`，正文为 Markdown；
  渐进式披露即「system 只放索引、正文按需读取」。
- 仓库里只放 `data/skills/README.md` 说明格式，不塞示例技能，避免示例被真的注入生产提示。

---

## 附：实施期确认的点

1. `mcp` 2.3.0 与 `httpx2` 同栈（SDK 自己就用 httpx2），无依赖冲突；
2. 取消持有 `async with` 的 MCP 任务时可能有 `ExceptionGroup`，`_shutdown` 用
   `gather(return_exceptions=True)` 一并吞掉；
3. 配置文件里传输字段叫 `type`（生态通用），代码里字段名是 `transport`，用 pydantic alias 对应；
4. `MCPConfig.servers` 带 alias `mcpServers`，程序内构造请走 `model_validate` 或字段名 `servers=`。
