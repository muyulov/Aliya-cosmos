# LLM 层实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 新增 `core/llm/` 层，把「调大模型」收成一个 `LLMService`，覆盖对话 / 流式 / 结构化 / 工具调用（单轮）/ embedding / 多模态六项能力。

**Architecture:** 官方 `openai` SDK 3.x 的 `AsyncOpenAI` + Chat Completions，`base_url` 可配即覆盖兼容端点，不做 Provider 抽象。配置 `LLMSettings` 挂在 `Settings.llm`，由容器按类型注入。依赖方向 `llm → (config, logger, service)`。

**Tech Stack:** Python 3.12、openai>=3.0（3.19.2）、pydantic 2、pytest、ruff、basedpyright。

**基线**：`154 passed`。

**工作区**：`.worktrees/llm`（分支 `feature/llm`）。所有命令在**该目录**下执行。

**设计文档**：`docs/plans/2026-09-26-llm-layer-design.md`（决策记录与验收标准以它为准，本计划只列步骤）。

---

## Task 1: 配置层 `LLMSettings`

**Files:**
- Modify: `core/config/settings.py`
- Modify: `core/config/__init__.py`
- Modify: `data/config/app.yaml`

**Step 1: 加配置模型**

`settings.py` 里与 `ClockSettings` 同级新增 `LLMSettings`（字段见设计文档 §三），`Settings` 加顶层字段 `llm: LLMSettings = LLMSettings()`。

**Step 2: 门面导出**

`core/config/__init__.py` 导入并 `__all__` 加 `LLMSettings`（RUF022 要求 ASCII 字母序：`ClockSettings` 之后、`LogSettings` 之前）。

**Step 3: 补 YAML 骨架**

`data/config/app.yaml` 末尾补 `llm:` 段（值等于默认，保持「骨架即全貌」），`api_key: ${OPENAI_API_KEY:}` 走空默认值通路。

**Step 4: 验证**

Run: `uv run python -c "from core.config import LLMSettings; print(LLMSettings())"`
Expected: 打印 `LLMSettings(...)`，且 `api_key=''`。

Run: `uv run pytest | tail -3`
Expected: `154 passed`（无新增用例，确认无回归）。

**Step 5: 提交**

```bash
git add core/config/settings.py core/config/__init__.py data/config/app.yaml
git commit -m "feat(config): 新增 LLM 层配置节点"
```

---

## Task 2: 错误树、消息构造器、客户端工厂

**Files:**
- Create: `core/llm/errors.py`
- Create: `core/llm/messages.py`
- Create: `core/llm/client.py`

**Step 1: `errors.py`**

`LLMError` 为根，子树：`LLMConfigError` / `LLMRequestError`（带 `status_code` / `endpoint` / `model` / `request_id`）/ `LLMTimeoutError` / `LLMConnectionError` / `LLMSchemaError` / `LLMResponseError`。原始 SDK 异常挂 `__cause__`。

**Step 2: `messages.py`**

构造器：`system` / `user` / `assistant` / `tool_result` / `image_url` / `image_base64` / `user_with_images` / `tool`。返回 SDK 的具体 TypedDict（不用 Union 联合类型），`tool()` 接受 pydantic 模型或 JSON Schema 字典。

**Step 3: `client.py`**

`build_client(config) -> AsyncOpenAI`，显式给 `timeout` / `max_retries`。

**Step 4: 验证**

Run: `uv run ruff check core/llm && uv run ruff format --check core/llm`
Expected: 无输出。

Run: `uv run python -c "from core.llm.messages import user_with_images, image_url; print(user_with_images('hi', image_url('u')))"`
Expected: `{'role': 'user', 'content': [{'type': 'text', 'text': 'hi'}, {'type': 'image_url', 'image_url': {'url': 'u'}}]}`

**Step 5: 提交**

```bash
git add core/llm/errors.py core/llm/messages.py core/llm/client.py
git commit -m "feat(llm): 新增错误树、消息构造器与客户端工厂"
```

---

## Task 3: `LLMService` 与层门面

**Files:**
- Create: `core/llm/service.py`
- Create: `core/llm/__init__.py`

**Step 1: `service.py`**

- `from core.service.base import Service`（子模块绝对路径，不碰父包门面）。
- `__init__(self, config: LLMSettings)` 只赋值；`start()` 里建客户端（无 key 只警告）；`stop()` 幂等；`health()` 不发网络请求。
- 五个方法：`chat` / `stream` / `chat_structured` / `chat_tools` / `embed`；`temperature` / `max_tokens` 的 `None` 在调用边界换成 `NOT_GIVEN`。
- 统一 `try/except openai.APIError` 转换错误（先 `APITimeoutError` 再 `APIConnectionError`），成功打 `LLM 调用完成`（模型 / 端点 / 耗时 / token 用量），失败走 `self.log_error`。
- 只读属性 `chat_model` / `embed_model` / `vision_model`。

**Step 2: `core/llm/__init__.py`**

导出 `LLMService`、`AssistantReply` / `ToolCall`、错误树、构造器。

**Step 3: 验证**

Run: `uv run ruff check core/llm && uv run ruff format --check core/llm`
Expected: 无输出。

**Step 4: 提交**

```bash
git add core/llm/service.py core/llm/__init__.py
git commit -m "feat(llm): 新增 LLMService 六项调用能力"
```

---

## Task 4: 接入容器（含断环）

**Files:**
- Modify: `core/service/registry.py`
- Modify: `core/service/__init__.py`

**Step 1: registry 注册**

`build_manager()` 里 `_ = manager.register(LLMService)`。

**Step 2: 摘除门面导出**

`core/service/__init__.py` 删掉 `from core.service.registry import build_manager, default_manager` 与 `__all__` 里同名两项（决策 11，断开 `service.__init__ → registry → llm.service → service.base` 的导入环）。

**Step 3: 验证（导入环回归）**

Run: `uv run python -c "import core.llm; print('ok')"`
Run: `uv run python -c "import core.service; print('ok')"`
Expected: 都是 `ok`，无 ImportError。

Run: `uv run pytest | tail -3`
Expected: `154 passed`。

**Step 4: 提交**

```bash
git add core/service/registry.py core/service/__init__.py
git commit -m "feat(service): 注册 LLM 服务并摘除门面里的 registry 导出"
```

---

## Task 5: 测试 `tests/test_llm_service.py`

**Files:**
- Create: `tests/test_llm_service.py`

**Step 1: 写假客户端**

实现 `chat.completions.create`（支持 `stream=True`）、`embeddings.create`、`close()`，记录每次调用的入参与次数；用 `cast("AsyncOpenAI", fake)` 白盒塞进 `service._client`（`# pyright: ignore[reportPrivateUsage]`）。

**Step 2: 用例**

按设计文档 §十的 17 条断言逐条落地（未配 key / embed_model 留空 / vision 回退 / omit 而非 NOT_GIVEN / content 为 None / 流式 / 结构化成功与失败 / json_object 模式 / 单轮工具调用 / 状态码与超时连接映射 / embedding 顺序 / stop 幂等 / 多模态结构）。

**Step 3: 验证**

Run: `uv run pytest tests/test_llm_service.py | tail -3`
Expected: 全部通过。

Run: `uv run pytest | tail -3`
Expected: `154 + N passed`（无回归）。

**Step 4: 提交**

```bash
git add tests/test_llm_service.py
git commit -m "test(llm): 新增 LLM 服务单元测试"
```

---

## Task 6: 文档与收尾校验

**Files:**
- Modify: `README.md`

**Step 1: 改 README**

配置项表格补 `llm.*` 各字段；新增「LLM 层」章节：六项能力调用示例 + 两条约定（不记正文 / 不自动多轮）+ `json_object` 需提示词含 json 字样。

**Step 2: 全量校验**

Run: `uv run pytest | tail -3`
Run: `uv run ruff check .`
Run: `uv run ruff format --check .`
Expected: 全绿 / 无输出。

Run: `rm -rf logs; timeout -s TERM 3 uv run python -m core.main; grep llm logs/app.log`
Expected: 出现 `llm` 的 `健康=False 详情=未配置 api_key…`，进程不报错退出。

**Step 3: 提交**

```bash
git add README.md
git commit -m "docs(llm): 补充 LLM 层文档与配置项"
```

---

## 实施补记（2026-09-26 审查后修正）

| 项 | 处置 |
| --- | --- |
| `NOT_GIVEN` 措辞 | 统一改成 `omit`：SDK 3.x 的签名只认 `Omit`，`LLMSettings` docstring 与本文档同步 |
| `llm.timeout` / `llm.retries` | 补 `Field(gt=0)` / `Field(ge=0)`，非法值不再静默传给 SDK |
| `chat_structured` / `chat_tools` | 补 `max_tokens`：此前配置的 `llm.max_tokens` 对这两项静默失效 |
| `start()` 幂等 | 改成看「有没有客户端」而非只看状态：手工重复调用不再建出第二个客户端 |
| `assistant()` | 支持 `tool_calls=`：写第二轮时不必 import SDK 类型 |
| 硬编码措辞 | 「未配置 api_key…」收成 `service.NO_API_KEY` |
| `data/config/app.yaml` | 删掉被 `extra="ignore"` 静默丢弃的 `llm.chat` / `llm.vision` / `embedding` 段 |

测试 19 → 26 例：补 model 覆盖 / 流式错误映射 / start 重复调用 / 有客户端时 health / 配置校验 / max_tokens / 回传消息结构。

### 后续变更：配置改双端点、移除 embedding

`llm` 下改为 `chat` / `vision` 两个端点（字段同 `LLMEndpointSettings`），`embed()` 删除。服务持有两个客户端，按 `model` 覆盖值挑端点（见 design 补记第 3 条）。影响：`app.yaml`、`LLMSettings` / `LLMEndpointSettings`、`client.build_client`、`LLMService` 的生命周期与全部调用方法、测试注入点由 `_client` 变 `_chat` / `_vision`、README 配置项表与示例。

---

## 完成后的合并流程

```bash
cd /home/cosmos/项目/Aliya-cosmos
git merge --no-ff feature/llm -m "merge: LLM 层"
uv run pytest | tail -3
uv run ruff check .
uvx basedpyright core tests   # 需 0 errors, 0 warnings, 0 notes
git worktree remove .worktrees/llm
git branch -d feature/llm
```
