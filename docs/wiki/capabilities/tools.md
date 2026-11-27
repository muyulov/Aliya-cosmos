# Agent 工具层

`core/agent/` 把「工具怎么声明、怎么交给模型、结果怎么回填」「Skill 怎么按需披露」「MCP server 怎么接入」收成一个小库。**ReAct 循环仍由调用方写**：拿 `reply.tool_calls` 判断要不要继续、把 `assistant(tool_calls=...)` 与各条 `tool_result` 追加回 `messages`、决定最多几轮。

扩展流程（工具 / Skill / MCP 的接入步骤、ReAct 循环写法、错误与日志约定、测试门禁）见[开发指南](../development/index.md)。

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

## 约定

| 约定 | 说明 |
| --- | --- |
| 描述来自 docstring | 工具描述取 docstring 正文，参数描述取 `Args:` / `参数:` 段；没有描述在定义期报 `ToolDefinitionError` |
| schema 由签名推导 | 参数缺注解、`*args` / `**kwargs` 直接报错——无注解会退化成 `Any`，模型只能盲猜 |
| 同步工具下线程 | 同步 handler 走 `asyncio.to_thread`，不会卡住事件循环 |
| 失败全部回填 | 参数校验失败回 `参数校验失败：字段: 原因`，handler 异常回 `工具执行失败：[类型] 消息`（不附堆栈）；只有「调了不存在的工具名」抛 `ToolNotFoundError` |
| 一次多个调用逐个执行 | 层内不并发：工具可能有副作用，回填顺序要与 `tool_calls` 对齐 |
| 注册表是显式实例 | 没有全局表；重名注册报错，顺序即声明顺序，运行期只读 |
| 不记正文 | 日志只记工具名 / 耗时 / 成败 / 错误类型，不记参数与返回值 |

## Skill

技能放在 `data/skills/<name>/SKILL.md`（固定路径，不开放配置），每份以 YAML frontmatter 开头：

```markdown
---
name: pdf
description: 处理 PDF 文件：抽取文本、合并拆分、填表单
---

# 使用说明
具体步骤……
```

- 渐进式披露：`library.index()` 只把「名称 + 一句话描述」拼进 system，正文由内置 `load_skill(name)` 工具按需读取；空库时 `index()` 返回空串、不导出工具。
- frontmatter 缺字段、语法错、重名在加载期报 `SkillError`；目录不存在按空库处理（只警告）。
- 目录里的其它脚本与附件不会被读取或执行。

## MCP

配置固定读 `data/mcp.json`，沿用生态通用的 `mcpServers` 结构（键即 server 名）：

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
| `type` | `stdio` | `stdio` / `http`（streamable HTTP） |
| `command` / `args` / `env` | 空 | `stdio` 必填 `command`；`env` 由 SDK 与进程默认环境合并 |
| `url` / `headers` | 空 | `http` 必填 `url` |
| `timeout` | `60` | 建连 + initialize + list_tools 的上限（秒）；`npx` 首次下载可能很慢 |

- 占位符复用 YAML 那套规则（`${VAR}` / `${VAR:默认}` / `$$`，取值来自进程环境与 `.env`）。
- 生命周期由 `async with` 掌握：`MCPManager.from_file()` 读配置，`start()` 并发建连，`tools` 返回映射后的工具，`stop()` 断开；都幂等。
- 工具名映射为 `{server}__{tool}`（非法字符换 `_`，超 64 字符截断）；`inputSchema` 原样透传，参数只校验「是 JSON 对象」，schema 由 server 校验。
- 任一 server 建连失败即关闭已连的并抛 `MCPConnectionError`（超时是它的子类 `MCPTimeoutError`），不静默跳过。
- 结果归一：文本与资源取正文，图片显示为 `[图片 mime]`，无文本但有 `structuredContent` 时转 JSON，`isError` 加 `工具执行失败：` 前缀；调用期异常由注册表回填。

## 错误

错误全部继承 `AgentError`，调用方一次 `except` 兜住整层：

| 类型 | 触发条件 |
| --- | --- |
| `ToolDefinitionError` | 无 docstring / 参数缺注解 / `*args`、`**kwargs` / 重名注册 |
| `ToolNotFoundError` | 执行了未注册的工具名 |
| `PromptError` | `system_prompt()` 的所有分段都为空 |
| `SkillError` | frontmatter 缺失或非法、缺字段、重名、文件读不动 |
| `SkillNotFoundError` | `load_skill()` 传了不存在的名字（`SkillError` 子类，由注册表回填） |
| `MCPConfigError` | `data/mcp.json` 读不动、JSON 非法、字段不合法、占位符缺值 |
| `MCPConnectionError` | 建连 / initialize / list_tools 失败 |
| `MCPTimeoutError` | 上述三步超时（`MCPConnectionError` 子类） |
