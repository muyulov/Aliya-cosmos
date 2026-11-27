# 快速开始

## 安装与启动

```bash
# 安装依赖
uv sync

# 启动基础服务（Ctrl-C 优雅关闭）
uv run python -m core.main

# 启动主 agent（core 4 个服务 + cosmos 12 个服务）
# 控制面令牌只从进程环境变量读（.env 不会注入 os.environ），服务端与客户端必须同一个值
ALIYA_CONTROL_TOKEN=dev uv run python -m agent.cosmos.app

# 另开终端，启动 Aliya（先 cosmos 后 Aliya）
ALIYA_CONTROL_TOKEN=dev uv run python -m agent.aliya.app

# 或者：用终端通道直接对话（CLI 即 cosmos，替代上面第一条命令）
ALIYA_CONTROL_TOKEN=dev uv run python -m agent.cosmos.cli
```

`agent.cosmos.cli` 与 `agent.cosmos.app` 二选一：两者都装配同一套服务并监听同一个控制面 socket，前者额外把 stdin 接成用户通道、把 Aliya 的回复打印到 stdout。

无需任何配置即可启动，所有配置项都有默认值。要自定义时改 `data/config/cosmos.yaml`；密钥类配置写在 `.env`（参考 `.env.example`）。详见[配置](config.md)。

进程常驻，`SIGINT` / `SIGTERM` 触发优雅关闭。信号注册优先交给事件循环（POSIX 的标准做法）；Windows 的事件循环不实现 `add_signal_handler`，会自动退回 `signal.signal`，并经 `call_soon_threadsafe` 置位。

## 浏览器 WebSocket 终端通道

`agent.cosmos.app` 额外把用户通道开成 WebSocket，供浏览器直接接入（默认开启，只监听回环地址）：

```yaml
agent:
  cosmos:
    terminal:
      ws:
        enabled: true
        host: 127.0.0.1      # 默认只监听回环地址
        port: 8780
        path: /ws
        token_env: ""        # 绑定非回环地址时必须配置，如 ALIYA_WEB_TOKEN
        default_session_id: web
        history_limit: 20    # 握手后补发的最近对话条数；0 关闭
```

协议是一帧一条 JSON：先发 `hello` 建立会话，之后收发 `message`；`typing` 上报输入状态，`ping` / `pong` 维持链路。服务端在 `ready` 之后补发 `history`，Aliya 的回复以 `message` 帧推送。

```js
const ws = new WebSocket("ws://127.0.0.1:8780/ws");
ws.onopen = () => ws.send(JSON.stringify({type: "hello", session_id: "web"}));
ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  if (data.type === "message") console.log(data.message.text);
};
ws.send(JSON.stringify({type: "message", text: "在吗"}));
```

`token_env` 配置令牌后，`hello` 需带上同值 `token`；绑定非回环地址却未配令牌会拒绝启动。CLI 与 WS 都占用同一个用户出口，只能二选一：`agent.cosmos.cli` 启动时会自动关闭 WS 通道，由 stdin 接管。

启动 `agent.cosmos.app` 后，直接用浏览器打开 `http://127.0.0.1:8780/` 即可使用自带测试页（对话、typing 上报、原始帧日志）；页面源码在 `agent/cosmos/web/ws-test.html`，也可以单独用 `file://` 打开。

## 常用命令

| 目的 | 命令 |
| --- | --- |
| 安装依赖 | `uv sync` |
| 启动基础服务 | `uv run python -m core.main` |
| 启动主 agent | `ALIYA_CONTROL_TOKEN=dev uv run python -m agent.cosmos.app` |
| 终端通道对话 | `ALIYA_CONTROL_TOKEN=dev uv run python -m agent.cosmos.cli` |
| 启动 Aliya | `ALIYA_CONTROL_TOKEN=dev uv run python -m agent.aliya.app` |
| 跑测试 | `uv run pytest` |
| 覆盖率 | `uv run pytest --cov=core --cov=agent` |
| 代码检查 | `uv run ruff check .` |
| 代码格式化 | `uv run ruff format .` |
| 本地预览文档 | `uv run mkdocs serve` |
| 构建文档（严格校验） | `uv run mkdocs build --strict` |

## 目录结构

```text
core/
  main.py        入口：装配服务容器并驱动生命周期
  service/       业务层：服务基类、容器、注册表、具体服务实现
  logger/        日志层：颜文字、结构化上下文、单行/树形/JSON 格式化、sink 装配
  config/        配置层：YAML 骨架加载与占位符插值
  embedding/     向量层：单条 / 批量文本向量化
  llm/           LLM 层：对话 / 流式 / 结构化 / 工具调用 / 多模态
  decision/      判断层：批量提问与 choice / score / noul 三原语
  agent/         Agent 工具层：工具声明与执行、Skill、MCP、系统提示拼装
agent/
  cosmos/        主 agent：契约、12 个常驻服务、进程内总线、装配、进程入口与终端通道
  aliya/         上层控制者：独立进程、UDS 客户端、回合循环、记忆与自主行动
data/           配置与运行数据：config/cosmos.yaml 为配置骨架，可安全提交
  Aliya/        Aliya 的数据根：记忆文件与工具区（运行时产物，不入库）
    workspace/  文件与命令工具区（不存在时自动创建）
  skills/       技能目录：<name>/SKILL.md
  mcp.json      MCP server 清单（标准 mcpServers 结构）
tests/           测试
docs/wiki/       本文档站点源文件
mkdocs.yml       文档站点配置
```

依赖方向与代码边界的详细约定见[代码边界](architecture/boundaries.md)。
