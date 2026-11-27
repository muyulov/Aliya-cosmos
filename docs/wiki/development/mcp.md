# MCP 接入

## 1. 配置文件

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

## 2. 生命周期

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

## 3. 工具映射

| 维度 | 规则 |
| --- | --- |
| 名称 | `{server}__{tool}`，非法字符替换为 `_`，超过 64 字符截断并 WARNING |
| 描述 | 取 server 返回的 `description`，为空时回退到工具名 |
| 参数 schema | `inputSchema` 原样透传 |
| 参数校验 | 只要求是合法 JSON 对象，schema 校验交给 server |
| 调用异常 | 交给注册表统一回填 |

工具名建议使用英文，中文 server 名会被整段替换成下划线。

## 4. 结果归一

| MCP 内容 | 回填文本 |
| --- | --- |
| `TextContent` | 正文 |
| `EmbeddedResource` 文本 | 资源正文 |
| `ImageContent` | `[图片 mime]` |
| 其它资源 | `[资源 uri]` |
| 无文本但有 `structuredContent` | JSON |
| 全部为空 | `ok` |
| `isError` | 前缀 `工具执行失败：` |

## 5. 测试注入

`MCPManager.from_file(connector=...)` 支持注入内存连接工厂，测试无需真实子进程或远端端点：

```python
manager = MCPManager.from_file(connector=fake_connector)
```

生产代码不要传 `connector`，保持默认实现。

## 6. 接入一个 MCP server

1. 在 `data/mcp.json` 的 `mcpServers` 下增加条目。
2. 密钥写占位符，真实值放 `.env`。
3. 用 `async with MCPManager.from_file() as mcp` 启动并注册 `mcp.tools`。
4. 确认工具名前缀符合预期。
