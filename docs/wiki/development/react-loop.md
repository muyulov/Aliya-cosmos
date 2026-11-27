# ReAct 循环

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

## 错误处理

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

## 日志与可观测性

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
