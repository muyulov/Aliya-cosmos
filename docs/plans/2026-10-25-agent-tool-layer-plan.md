# Agent 工具层实施计划

> **给执行者：** 本计划按 TDD 逐任务执行。设计依据是 `docs/plans/2026-10-25-agent-tool-layer-design.md`；
> 实施中若与设计冲突，以设计文档为准，并在本文末尾「实施补记」记下偏差。

**目标：** 新增 `core/agent/`：工具注册表 + system 提示拼装 + Skill 渐进式披露 + MCP 工具接入，
循环仍由调用方写。

**架构：** `Tool` 把「原始 arguments JSON → 回填文本」收成一个 `invoke` 入口，本地工具（`@tool`
从签名推导 schema）与 MCP 工具（`inputSchema` 原样透传）共用同一条执行路径；`ToolRegistry` 负责
声明与执行，`SkillLibrary` 与 `MCPManager` 只是两种 Tool 来源。

**技术栈：** Python 3.12+ / uv / pydantic / PyYAML / mcp 2.3 / pytest + pytest-asyncio / basedpyright / ruff。

**动工前基线（已在 main 分支执行，不用 worktree）：**

```bash
cd /home/cosmos/项目/Aliya-cosmos
uv sync --no-install-project
.venv/bin/python -m pytest | tail -3
```

**每个任务结束都跑这三条判据，全绿才提交：**

```bash
uv run ruff check . && uv run ruff format --check .
uvx basedpyright core tests
.venv/bin/python -m pytest | tail -3
```

---

### Task 1: 错误树 + 工具核心（`errors.py` / `tool.py`）

**Files:**
- Create: `core/agent/errors.py`
- Create: `core/agent/tool.py`
- Test: `tests/agent/test_tool.py`

**Step 1: 写失败测试**

关键用例（详细断言见测试文件）：

- `@tool` 从签名生成 schema：必填、默认值可选、`Literal` 枚举、嵌套 `BaseModel`、`Optional`
- docstring 描述取正文；`Args:` 段解析成参数 `description`；无 `Args:` 段时参数无描述
- 同步 handler 在非主线程执行（`threading.current_thread() is not threading.main_thread()`）
- 返回值归一：`None` → `ok`、`str` 原样、`BaseModel` → JSON、`dict` → JSON（中文不转义）
- 参数校验失败 → `参数校验失败：…`（单行、含字段名），不抛
- handler 抛异常 → 异常原样冒泡（由注册表回填）
- 四类定义错误：无 docstring、`*args`、`**kwargs`、参数缺注解

**Step 2: 跑测试确认失败**

```bash
.venv/bin/python -m pytest tests/agent/test_tool.py -q
```

**Step 3: 最小实现**

`errors.py`：`AgentError` + `ToolDefinitionError` / `ToolNotFoundError` / `PromptError` /
`SkillError` / `SkillNotFoundError` / `MCPConfigError` / `MCPConnectionError` / `MCPTimeoutError`。

`tool.py` 的核心结构：

```python
@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    parameters: Mapping[str, object]
    invoke: Callable[[str], Awaitable[str]]
    func: Callable[..., object] | None = None
```

- `_split_docstring(doc)`：`inspect.cleandoc` 后按 `Args:` / `参数:` 切段，逐行 `名字: 描述`，续行拼接。
- `_build_model(func, param_docs)`：`get_type_hints(func)` 取真实注解，`create_model`
  （`extra="forbid"`），默认值原样保留；无参数返回 `None`。
- `_invoke_for(...)`：校验 → 异步 `await` / 同步 `asyncio.to_thread` → 归一。
- `tool(func)`：名称取 `__name__`，描述必填，包装成 `Tool`。

**Step 4: 跑测试确认通过**

```bash
.venv/bin/python -m pytest tests/agent/test_tool.py -q
```

**Step 5: 提交**

```bash
git add core/agent/errors.py core/agent/tool.py tests/agent/test_tool.py
git commit -m "feat(agent): 新增工具模型与签名推导 schema"
```

---

### Task 2: 注册表 + 提示拼装（`registry.py` / `prompt.py`）

**Files:**
- Create: `core/agent/registry.py`
- Create: `core/agent/prompt.py`
- Create: `core/agent/__init__.py`
- Test: `tests/agent/test_registry.py`、`tests/agent/test_prompt.py`

关键用例：

- `register` 顺序即 `declarations()` 顺序；声明结构复用 `core.llm.tool()`
- 重名注册抛 `ToolDefinitionError`；未知工具名抛 `ToolNotFoundError`
- `execute` 成功返回文本；handler 异常回填 `工具执行失败：[类型] 消息` 且不含 `Traceback`
- 无参工具忽略空串 / `{}` / 任意内容
- `system_prompt` 跳过 `None` 与空白段、空行连接；全空抛 `PromptError`

**Step 5: 提交**

```bash
git add core/agent/registry.py core/agent/prompt.py core/agent/__init__.py tests/agent/
git commit -m "feat(agent): 新增工具注册表与系统提示拼装"
```

---

### Task 3: Skill（`skill.py` + `data/skills`）

**Files:**
- Create: `core/agent/skill.py`
- Create: `data/skills/example/SKILL.md`（示例，可提交）
- Modify: `core/agent/__init__.py`
- Test: `tests/agent/test_skill.py`

关键用例：

- 扫描 `data/skills/*/SKILL.md`，`index()` 文案正确；空库 `index() == ""` 且 `tools() == []`
- `load_skill` 命中返回正文；未命中抛 `SkillNotFoundError`（由注册表回填）
- frontmatter 缺失 / 缺 `name` / 缺 `description` / YAML 非法 / 重名 → `SkillError`
- 目录不存在 → 空库 + WARNING（`caplog`）
- 目录里没有 `SKILL.md` 的子目录被忽略

**Step 5: 提交**

```bash
git add core/agent/skill.py data/skills tests/agent/test_skill.py
git commit -m "feat(agent): 新增 Skill 库与按需披露工具"
```

---

### Task 4: MCP（`mcp.py` + `data/mcp.json`）

**Files:**
- Create: `core/agent/mcp.py`
- Create: `data/mcp.json`（`{"mcpServers": {}}`）
- Modify: `core/agent/__init__.py`
- Modify: `pyproject.toml`（已 `uv add mcp>=2.3`）
- Test: `tests/agent/test_mcp.py`

关键实现点：

- `MCPConfig.load(path, env_file)`：`json.loads` → `interpolate`（复用 `core.config.loader`）→
  `model_validate`；`mcpServers` 映射在 `mode="before"` 的校验器里展开成带 `name` 的列表。
- `MCPManager`：每个 server 一个任务持有 `async with` 传输；`ready` 事件 + 失败字典；
  `start()` 逐个等待，超时抛 `MCPTimeoutError`，失败抛 `MCPConnectionError`，两者都先关闭已连的。
- `connector` 可注入：默认 `_connect` 按 `transport` 建 stdio / streamable HTTP 并 `initialize()`。
- 工具映射：`{server}__{tool}`、`inputSchema` 原样、结果按 `TextContent` / `ImageContent` /
  `EmbeddedResource` / `structured_content` 归一，`is_error` 加前缀。

关键测试（用 `create_client_server_memory_streams` + `MCPServer._lowlevel_server` 真跑协议）：

- `list_tools` → 前缀命名、描述回退、schema 透传
- `call_tool` → 文本 / 图片 / 资源 / `structured_content` / 空结果
- `is_error` 前缀回填；调用异常回填；arguments 非 JSON 或非对象 → `参数校验失败`
- 建连失败 → `MCPConnectionError` 且已连 server 被关闭；超时 → `MCPTimeoutError`
- `stop` 幂等、空 server 列表可直接 start/stop
- 配置：文件缺失（空 + WARNING）、JSON 非法、顶层非对象、占位符缺失、字段非法 → `MCPConfigError`

**Step 5: 提交**

```bash
git add core/agent/mcp.py data/mcp.json tests/agent/test_mcp.py pyproject.toml uv.lock
git commit -m "feat(agent): 接入 MCP stdio / streamable HTTP 工具"
```

---

### Task 5: README 与收尾

**Files:**
- Modify: `README.md`

- 目录结构加 `agent/`；依赖方向加 `agent → (config, llm, logger)`
- 新增「Agent 层」章节：工具、Skill、MCP 三段用法 + 约定表 + 错误表
- 配置表补 `data/mcp.json` 字段说明；说明 `data/skills` 目录约定

```bash
git add README.md
git commit -m "docs(agent): README 补 Agent 层用法与配置"
```

---

## 实施补记

- MCP SDK 实际为 2.3.0（v2）：`CallToolResult` 字段是 `structured_content` / `is_error`，
  工具字段是 `input_schema`；v2 没有 `create_connected_server_and_client_session`，
  测试改用 `create_client_server_memory_streams` + `MCPServer._lowlevel_server` 真跑协议。
- `stdio_client` 的 `env` 是 `get_default_environment() | server.env`（SDK 自己合并），
  因此配置里只需写增量变量。
- 配置文件里的传输字段按生态惯例叫 `type`，代码里是 `transport`，用 pydantic alias 对应。
- `data/skills` 下只提交 `README.md` 说明格式，没有放示例技能：示例会被真的扫进生产提示。
- MCP 工具名会把非 ASCII 字符替换成 `_`，server 名建议用英文（写进 README 与设计文档）。
