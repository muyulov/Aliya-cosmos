# 测试与质量门禁

项目统一使用 uv + pytest：

```bash
# 全量测试
uv run pytest

# 覆盖率
uv run pytest --cov=core --cov=agent

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

## 扩展清单

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

## 常见坑

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 模型选错工具 | docstring 太笼统 | 写清适用场景与参数含义 |
| 模型传错参数 | 参数缺注解或描述为空 | 补类型注解与 `Args:` 段 |
| 工具调用卡住其他请求 | 同步 handler 里做了长时间阻塞 | 改成 `async def` 或拆小粒度 |
| 工具名重复 | 两个函数同名，或 MCP 前缀撞名 | 注册期会报错，重命名后再注册 |
| Skill 没出现在提示里 | frontmatter 缺字段或目录层级不对 | 确认 `data/skills/<name>/SKILL.md` |
| MCP 工具消失 | 某个 server 建连失败 | 本层会抛错回滚，检查 `data/mcp.json` 与网络 |
| 循环停不下来 | 没设轮数上限 | 设置 `MAX_ROUNDS` 并在无 `tool_calls` 时退出 |
