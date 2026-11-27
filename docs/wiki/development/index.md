# 开发指南

本指南说明 `core/agent` 的定位、接口、约定与开发流程，帮助你在不读源码的情况下正确扩展工具、Skill 与 MCP 接入，并写出可控的 ReAct 循环。

面向读者：在 Aliya-cosmos 上开发 Agent 能力的工程师。适用版本：`core/agent`（工具 / Skill / MCP）。

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

这段代码只完成了一轮工具调用。要让 Agent 继续推理，需要把 assistant 消息与工具结果追加回历史，再发起下一轮，见[编写 ReAct 循环](react-loop.md)。

## 4. 明确不做的事

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

## 5. 主 agent 装配（agent/cosmos）

`agent/cosmos` 是 Aliya 的主 agent，本身不改 `core` 的任何行为，只在其上叠加服务：

```text
core 的 4 个服务（clock / embedding / llm / decision）
  + BusService
  + AgentRegistryService / AgentRuntimeService / DesktopService / PresenceService
  + ConversationService / DispatchService / WorldBridgeService / TerminalService / ControlService
  + ControlServerService / WebSocketChannelService
```

装配入口在 `agent/cosmos/assembly.py`，进程入口在 `agent/cosmos/app.py`：

```bash
uv run python -m agent.cosmos.app
```

### 5.1 与 core 的边界

| 约束 | 落地方式 |
| --- | --- |
| `core` 不 import `agent` | 装配、配置模型转换（`bootstrap.to_card`）都放在 `agent/cosmos` |
| 服务由容器统一启停 | 全部是 `core.service.Service` 子类，依赖按类型声明 |
| 配置只注入 `AgentSettings` | 容器只索引 `Settings` 的顶层节点，嵌套的 `AgentCosmosSettings` 不注入 |
| 时间统一来源 | 所有服务构造器注入 `ClockService`，不用 `datetime.now()` |

### 5.2 端口与 setter 注入

容器只接受 `Service` 与配置节点两类参数，Protocol 不能注入。因此：

- `BusService` 持有 `MessageBus` / `EventBus` 的进程内实现，其他服务按类型依赖它；
- `UserChannelPort` / `WorldReasoningPort` 由部署方在 `start()` 前用 `set_user_channel()` / `set_port()` 注入；
- 链路状态用 `set_link_provider()` 反向注册，避免 `WorldBridgeService ↔ TerminalService` 构造器循环依赖。

### 5.3 新增子 agent

1. 写 `async def handle(request, ctx) -> AgentResult`（参考 `agent/cosmos/registry/echo.py`）；
2. 在 `data/config/cosmos.yaml` 的 `agent.cosmos.agents[]` 登记 `id / module / handler / card`；
3. 重启进程，`AgentRegistryService.start()` 会自动导入并注册；卡片非法（id、能力、schema、超时、同步 handler）会在启动期 fail fast。

`agent/aliya` 与真实用户通道不在 cosmos 内实现：Aliya 只依赖 `agent.cosmos.contracts` 与 `ControlService`，用户通道由部署方实现 `UserChannelPort` 后注入。详见[扩展性](../agents/cosmos/extension.md)。

## 章节索引

| 主题 | 页面 |
| --- | --- |
| 继承 `Service`、注册、启停语义 | [新增一个服务](services.md) |
| 工具声明、docstring 规范、注册表 | [工具开发](tools.md) |
| 系统提示拼装与 Skill 渐进式披露 | [提示拼装与 Skill](prompt-skill.md) |
| MCP server 接入 | [MCP 接入](mcp.md) |
| ReAct 循环、错误处理、日志 | [ReAct 循环](react-loop.md) |
| 测试与质量门禁 | [测试与质量门禁](testing.md) |
