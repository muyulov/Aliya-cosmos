# 向量层实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 新增 `core/embedding/` 层，把「文本转向量」收成一个 `EmbeddingService`，覆盖单条与批量向量化，业务代码不直接接触 SDK。

**Architecture:** 复用已有 `openai>=3.0` 的 `embeddings.create`，不新增依赖；内核只有 `RemoteEncoder` 一个实现，服务在 `start()` 里直接建它（容器的 `__init__` 契约只认 `Service` 与配置节点，内核注不进去，也不留覆盖点）。配置 `EmbeddingSettings` 挂在 `Settings.embedding`，容器按类型注入。

**Tech Stack:** Python 3.12、openai>=3.0（3.19.2）、pydantic 2、pytest、ruff、basedpyright。

**基线**：`183 passed`。

**工作区**：`.worktrees/embedding`（分支 `feature/embedding`）。所有命令在**该目录**下执行。

**设计文档**：`docs/plans/2026-09-26-embedding-layer-design.md`（15 条决策与验收标准以它为准，本计划只列步骤）。

---

## Task 1: 配置层 `EmbeddingSettings`

**Files:**
- Modify: `core/config/settings.py`
- Modify: `core/config/__init__.py`
- Modify: `data/config/app.yaml`

**Step 1: 加配置模型**

`settings.py` 里与 `ClockSettings` 同级新增 `EmbeddingSettings`（字段见设计文档 §三）：`base_url` / `api_key` / `model` / `timeout(gt=0)` / `retries(ge=0)` / `batch_size(default=10, gt=0)` / `dimensions: int | None = None`。

**Step 2: 挂顶层字段**

`Settings` 加 `embedding: EmbeddingSettings = EmbeddingSettings()`。

**Step 3: 门面导出**

`core/config/__init__.py` 导入并 `__all__` 加 `EmbeddingSettings`（RUF022 要求 ASCII 字母序：`ConfigError` 之后、`LLMEndpointSettings` 之前）。

**Step 4: 补 YAML 骨架**

`data/config/app.yaml` 末尾补 `embedding:` 段（值等于默认，带说明注释，风格与 `llm:` 段一致），`api_key: ${DASHSCOPE_API_KEY:}` 走空默认值通路。

**Step 5: 验证**

Run: `uv run python -c "from core.config import load_settings; print(load_settings().embedding)"`
Expected: 打印 `EmbeddingSettings(...)`，且有 `api_key=''` 与 `batch_size=10`（实测防 `extra="ignore"` 静默失效）。

Run: `uv run pytest | tail -3`
Expected: `183 passed`（无新增用例，确认无回归）。

**Step 6: 提交**

```bash
git add core/config/settings.py core/config/__init__.py data/config/app.yaml
git commit -m "feat(config): 新增向量层配置节点"
```

---

## Task 2: 错误树与内核

**Files:**
- Create: `core/embedding/errors.py`
- Create: `core/embedding/encoder.py`

**Step 1: `errors.py`**

`EmbeddingError` 为根，子树：`EmbeddingConfigError` / `EmbeddingInputError` / `EmbeddingRequestError`（带 `status_code` / `endpoint` / `model` / `request_id`）/ `EmbeddingTimeoutError` / `EmbeddingConnectionError` / `EmbeddingResponseError`。原始 SDK 异常挂 `__cause__`；不进 `ServiceError` 树。

**Step 2: `encoder.py`**

- `EncodedVector(index, vector)` / `EncodeResult(items, prompt_tokens=None)` 两个 frozen dataclass：出口要带结构，归位责任才落得到调用方。
- `RemoteEncoder(client, model)`：`encode(texts, *, dimensions=None) -> EncodeResult` + `aclose()`；三条约定写进 docstring（一次调用一批、条数相等、每条带 `index` 由服务层归位、`dimensions` 做不到必须抛错不得静默忽略）。
- `encode()` 里 `dimensions` 用 `omit` 表达「不传」（显式 `None` 会被 SDK 序列化成 JSON `null`）；`usage` 按可空处理（SDK 类型里必填，兼容端点不返回时被 `construct_type` 填成 `None`，实测）。
- `build_encoder(config) -> RemoteEncoder`：全仓库除 `core/llm/client.py` 外唯一 `AsyncOpenAI(...)` 的地方，`timeout` / `max_retries` 显式给值。
- 不做协议抽象与覆盖点：只有一个实现，抽象就是预留（2026-09-26 清掉）。

**Step 3: 验证**

Run: `uv run ruff check core/embedding && uv run ruff format --check core/embedding`
Expected: 无输出。

Run: `uv run python -c "import core.embedding.encoder; import core.embedding.errors; print('ok')"`
Expected: `ok`。

**Step 4: 提交**

```bash
git add core/embedding/errors.py core/embedding/encoder.py
git commit -m "feat(embedding): 新增错误树与向量化内核"
```

---

## Task 3: `EmbeddingService` 与包门面

**Files:**
- Create: `core/embedding/service.py`
- Create: `core/embedding/__init__.py`

**Step 1: `service.py`**

- `from core.service.base import Service`（子模块绝对路径，不碰父包门面）。
- `__init__(self, config: EmbeddingSettings)` 只赋值；`start()` 幂等看 `_encoder is not None`，无 key 只警告；`stop()` 幂等、可作用在从未启动过的实例上；`health()` 不发请求。
- `embed` / `embed_many`：`_pick(dimensions, config.dimensions)` 三级优先；先全量校验非空再发请求；`embed_many([])` 早退返回 `[]`；按 `batch_size` 串行切片；`_reorder` 按 `item.index` 归位（越界 / 重复 / 条数不符抛错）、`_check_vectors` 校验空向量与声明维度。
- `start()` 里直接 `build_encoder(self._config)`（没有覆盖点）；只读属性 `model`。
- `_wrap_errors` 上下文管理器统一映射 SDK 异常（先 `APITimeoutError` 再 `APIConnectionError`）；成功打 `向量化完成`（模型 / 端点 / 文本数 / 维度 / 耗时毫秒，`prompt_tokens` 非 None 时补 `输入token`），批数 > 1 再补 `批量向量化完成`；失败走 `self.log_error`。

**Step 2: `core/embedding/__init__.py`**

导出 `EmbeddingService`、`Encoder` / `RemoteEncoder` / `build_encoder`、错误树。

**Step 3: 验证**

Run: `uv run ruff check core/embedding && uv run ruff format --check core/embedding`
Expected: 无输出。

Run: `uv run python -c "import core.embedding; print('ok')"`
Expected: `ok`（无导入环）。

**Step 4: 提交**

```bash
git add core/embedding/service.py core/embedding/__init__.py
git commit -m "feat(embedding): 新增 EmbeddingService 单条与批量向量化"
```

---

## Task 4: 接入容器

**Files:**
- Modify: `core/service/registry.py`
- Modify: `tests/test_service_injection.py`

**Step 1: registry 注册**

`build_manager()` 里在 clock 与 llm **之间** `_ = manager.register(EmbeddingService)`（注册顺序 = clock → embedding → llm）。

**Step 2: 改测试断点**

`tests/test_service_injection.py` 的注册表顺序断言 `["clock", "llm"]` → `["clock", "embedding", "llm"]`。

**Step 3: 验证（导入环回归 + 全量）**

Run: `uv run python -c "import core.embedding; print('ok')"`
Run: `uv run python -c "import core.service; print('ok')"`
Expected: 都是 `ok`，无 ImportError。

Run: `uv run pytest | tail -3`
Expected: `183 passed`。

**Step 4: 提交**

```bash
git add core/service/registry.py tests/test_service_injection.py
git commit -m "feat(service): 注册向量服务"
```

---

## Task 5: 测试 `tests/test_embedding_service.py`

**Files:**
- Create: `tests/test_embedding_service.py`

**Step 1: 写假内核**

实现 `encode()`（记录每次收到的 `texts` 与 `dimensions`，返回 `EncodeResult`）与 `aclose()`；可配置返回值、乱序 / 越界 / 重复的 `index`、`prompt_tokens`、抛错。用 `cast("RemoteEncoder", cast("object", fake))` 白盒塞进 `service._encoder`（`# pyright: ignore[reportPrivateUsage]`）。

**Step 2: 用例**

按设计文档 §十的 15 类断言逐条落地（单条 / `dimensions` 三级优先 / 批量切分次数 / 乱序归位 / `index` 越界与重复与条数不符 / 空输入零调用 / 空串与全空白串 / 未配 key / 有内核时 health / 声明维度不符 / start-stop 幂等 / SDK 异常映射 / 配置校验 / 日志含 `输入token`）。

**Step 3: 验证**

Run: `uv run pytest tests/test_embedding_service.py | tail -3`
Expected: 全部通过。

Run: `uv run pytest | tail -3`
Expected: `183 + N passed`（无回归）。

**Step 4: 提交**

```bash
git add tests/test_embedding_service.py
git commit -m "test(embedding): 新增向量服务单元测试"
```

---

## Task 6: 文档与收尾校验

**Files:**
- Modify: `README.md`

**Step 1: 改 README**

配置项表格补 `embedding.*` 各字段；新增「向量层」章节：两条调用示例 + 三条约定（原样透传不归一化 / 空序列早退」/ 缺密钥不 fail fast）。

**Step 2: 全量校验**

Run: `uv run pytest | tail -3`
Run: `uv run ruff check .`
Run: `uv run ruff format --check .`
Run: `uvx basedpyright core tests`（需 `0 errors, 0 warnings, 0 notes`）
Expected: 全绿 / 无输出。

Run: `rm -rf logs; timeout -s TERM 3 uv run python -m core.main; grep -E "embedding" logs/app.log`
Expected: 出现 `embedding` 的 `健康=False 详情=未配置 api_key…`，进程不报错退出。

**Step 3: 提交**

```bash
git add README.md
git commit -m "docs(embedding): 补充向量层文档与配置项"
```

---

## 完成后的合并流程

```bash
cd /home/cosmos/项目/Aliya-cosmos
git merge --no-ff feature/embedding -m "merge: 向量层"
uv run pytest | tail -3
uv run ruff check .
uvx basedpyright core tests   # 需 0 errors, 0 warnings, 0 notes
git worktree remove .worktrees/embedding
git branch -d feature/embedding
```
