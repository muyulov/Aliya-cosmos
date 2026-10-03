# 记忆层（归忆）期一「地基」实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 让 `core/memory/` 能存能取——把原始回合与角色状态写进 Kùzu、按向量与结构化两种方式召回、失败可重试可死信，并且改动可落回 llm 层且不破坏既有 335 个用例。

**Architecture:** 新增 `core/memory/` 服务，依赖 `EmbeddingService` / `LLMService` / `DecisionService`。存储用 Kùzu 0.11.3 一体（图 + HNSW 向量 + 全文检索）。Kùzu 的 Python API 是同步的，全部经**单线程执行器**下沉（保证线程亲和），写入由**单协程 worker** 串行化。本期**不做** LLM 抽取与门控（期二）：角色与状态由显式接口写入，`observe` 只负责原文落库。

**Tech Stack:** Python 3.12+、uv、pydantic、Kùzu 0.11.3、pytest / pytest-asyncio、basedpyright、ruff。

**设计依据:** `docs/plans/2026-10-03-memory-layer-design.md`（下称「设计文档」，章节号直接引用）。

---

## 全局约束（每个任务都适用，违反即返工）

1. **全中文**：注释、docstring、日志文案、错误消息、测试名一律中文。
2. **禁止相对导入**；子模块引用基类写绝对路径（`from core.service.base import Service`）。
3. **覆写受保护方法要 `@override`**；实例属性必须写类型注解。
4. **禁止 Makefile / Docker / pre-commit / CI**；命令一律 `uv run`（worktree 内跑测试用 `.venv/bin/python -m pytest`，避免 uv 争锁）。
5. **`__all__` 排序两套**：RUF022 大小写敏感（ASCII 序）；ruff isort 的 from-import 大小写不敏感。
6. **测试名不能含断言关键词**（如 `pass`/`assert`/`is`/`not`/`in` 之类会落进日志文本的词），否则会被日志文件的 `tmp_path` 命中而假红。
7. **覆盖率必须保持 100%**（判据 `uv run pytest --cov=core`，`core/main.py` 维持 omit）。
8. **判据三条齐全才算完成**：`uvx basedpyright core tests` = `0 errors, 0 warnings, 0 notes`；`ruff check .` 与 `ruff format --check .` 全绿；覆盖率 100%。
9. **改配置必须实测 `load_settings()`**——本仓模型全是 `extra="ignore"`，段名写错会静默失效。
10. **`replace_in_file` / `write_to_file` 可能「报成功但不落盘」**。每次改完用 `wc -c` 或读回核验。

---

## Task 0: 环境确认

**Files:** 无

**Step 1: 确认在 worktree 且基线干净**

Run:
```bash
cd /home/cosmos/项目/Aliya-cosmos/.worktrees/memory
git status --porcelain
git --no-pager log --oneline -1
```

Expected: 工作区无输出（干净）；HEAD 是 `c74d74e docs(memory): 新增记忆层（归忆）设计文档`。

**Step 2: 确认依赖已装、基线绿**

Run:
```bash
.venv/bin/python -m pytest -q 2>&1 | tail -3
uvx basedpyright core tests 2>&1 | tail -2
```

Expected: 335 用例全绿；`0 errors, 0 warnings, 0 notes`。

> 若 `.venv` 不存在，先跑 `uv sync --no-install-project`（**不要**用裸 `uv sync`，会撞 uv 构建缓存里 hatchling/pluggy 的 `HookimplMarker` 报错）。

---

## Task 1: 依赖与记忆层配置

**Files:**
- Modify: `pyproject.toml`
- Modify: `core/config/settings.py`
- Modify: `data/config/cosmos.yaml`
- Test: `tests/test_config_loader.py`

**Step 1: 写失败的测试**

在 `tests/test_config_loader.py` 末尾追加：

```python
def test_记忆段能从骨架读回() -> None:
    """防 extra="ignore" 静默失效：骨架里的 memory 段必须真的被解析进模型。"""
    settings = load_settings()
    assert settings.memory.db_path == "data/memory"
    assert settings.memory.dimensions >= 1
    assert settings.memory.retry.deadletter_path.endswith(".jsonl")
    assert settings.memory.gate.backend in ("laya", "jev")


def test_记忆层默认值全部合法() -> None:
    """不读文件直接构造也要有一组合法默认值（进程无配置也能起）。"""
    memory = MemorySettings()
    assert memory.queue_size > 0
    assert 0.0 <= memory.gate.importance_threshold <= 1.0
    assert memory.recall.hops >= 1
    assert memory.consolidate.min_events >= 1
    assert 0.0 < memory.decay.floor <= 1.0
    assert memory.retry.max_attempts >= 1
```

同时在文件顶部 import 里加 `MemorySettings`（与既有 `from core.config import ...` 合并）。

**Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_config_loader.py -k 记忆 -v`
Expected: FAIL，`ImportError: cannot import name 'MemorySettings'`。

**Step 3: 写实现**

`pyproject.toml` 的 `dependencies` 增加 `kuzu>=0.11.3,<0.12`（与既有依赖并列，保持排序）。

`core/config/settings.py`：新增四个子配置与 `MemorySettings`（代码见设计文档 §3.1，一字不差照抄），并在 `Settings` 上追加字段：

```python
    memory: MemorySettings = MemorySettings()
```

`data/config/cosmos.yaml`：追加 `llm.memory` 段与 `memory` 段（**照抄设计文档 §3.2**）。注意 `memory.retry.deadletter_path` 不要加引号（无冒号歧义），`dimensions` 写整数。

`data/config/cosmos.yaml` 的 `llm` 段现在有三个子节点，注意缩进对齐。

**Step 4: 跑测试确认通过**

Run:
```bash
.venv/bin/python -m pytest tests/test_config_loader.py -q
uv run python -c "from core.config import load_settings; print(load_settings().memory.model_dump_json(indent=1))"
```

Expected: 全绿；打印出的 `memory` 节点与 YAML 逐字段一致（**这是防静默失效的关键一步**，不对就是段名或缩进写错了）。

**Step 5: 提交**

```bash
git add pyproject.toml core/config/settings.py data/config/cosmos.yaml tests/test_config_loader.py
git commit -m "feat(config): 新增记忆层配置并引入 kuzu 依赖"
```

---

## Task 2: `llm.memory` 第三端点与显式路由

**Files:**
- Modify: `core/config/settings.py`（`LLMSettings`）——**已于 Task 1 返工中提前完成**（`memory: LLMEndpointSettings` 字段与 docstring 都已就位），本任务**不再改这个文件**
- Modify: `core/llm/service.py`
- Test: `tests/test_llm_service.py`

**Step 1: 写失败的测试**

在 `tests/test_llm_service.py` 追加（沿用文件内既有的 `_endpoint(...)` / `_service(...)` 辅助函数）：

```python
async def test_第三端点建客户端与健康自报(tmp_path: Path) -> None:
    """llm.memory 与另两个端点同构：建客户端、进 health 的 extra、三头全无才不健康。"""
    service = _service(chat=_endpoint(), vision=None, memory=_endpoint(api_key="k"))
    with _logging(tmp_path):
        await service.start()
    status = await service.health()
    assert status.healthy is True
    assert "记忆模型" in status.extra and "记忆端点" in status.extra
    await _stop(service)


async def test_记忆端点只能显式选择() -> None:
    """按模型名匹配只在 chat/vision 之间发生；memory 永远不接管，避免同名模型被抢。"""
    memory = _endpoint(model="same-name", api_key="k")
    service = _service(chat=_endpoint(model="same-name", api_key="k"), vision=None, memory=memory)
    with _logging(...):
        await service.start()
    # 不传 endpoint：即便 model 与 memory 相同，也仍走 chat
    client, slot, resolved = service._endpoint("same-name")
    assert slot is service._config.chat
    # 显式传 endpoint="memory" 才走记忆端点
    assert service._resolve_endpoint(None, "memory")[1] is memory


async def test_三头全没客户端才不健康() -> None:
    service = _service(chat=None, vision=None, memory=None)
    with _logging(...):
        await service.start()
    assert (await service.health()).healthy is False
```

> 若既有辅助函数名不同，按文件内实际名字改写；**不要**为了计划好看去重命名既有辅助。

**Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_llm_service.py -k "记忆端点 or 第三端点 or 三头" -v`
Expected: FAIL（`LLMSettings` 没有 `memory` 字段 / `_resolve_endpoint` 不存在）。

**Step 3: 写实现**

1. `core/config/settings.py` 的 `LLMSettings` 加字段：

```python
    memory: LLMEndpointSettings = LLMEndpointSettings()
```

同时把类 docstring 从「chat 与 vision 各是一个端点」改为「chat / vision / memory 各是一个端点；memory 只接受显式 `endpoint=` 选择，不参与模型名匹配」。

> **已于 Task 1 返工中完成**（`LLMSettings.memory` 与 docstring 都已在 `17251d8` 落地，并有一条守护用例盯住「骨架 `llm.memory` 段必须被模型认识」）。本任务跳过此步。

2. `core/llm/service.py`：

- 加 `self._memory: AsyncOpenAI | None = None`（带类型注解）。
- `start()` 追加 `self._memory = self._build("memory", self._config.memory, self._memory)`。
- `stop()` 追加关闭 `self._memory`（先取出置 None 再 `await close()`，与另两个同形）。
- `health()` 的 `extra` 追加 `"记忆模型": self._config.memory.model, "记忆端点": self._config.memory.base_url`；健康判据改为 `if self._chat is not None or self._vision is not None or self._memory is not None:`。
- 加属性：

```python
    @property
    def memory_model(self) -> str:
        """记忆专用模型：抽取与记忆块渲染走它。"""
        return self._config.memory.model
```

- 新增解析方法，并把 `_endpoint()` 的**局部变量 `endpoint` 改名成 `slot`**（避免与新形参遮蔽）：

```python
type EndpointName = Literal["chat", "vision", "memory"]

    def _resolve_endpoint(
        self, model: str | None, name: EndpointName | None
    ) -> tuple[AsyncOpenAI, LLMEndpointSettings, str]:
        """挑端点：显式 name 优先；不给 name 时保持旧规则（只在 chat / vision 之间按模型名匹配）。"""
        if name is not None:
            slot = self._config.memory if name == "memory" else (
                self._config.vision if name == "vision" else self._config.chat
            )
            client = {"chat": self._chat, "vision": self._vision, "memory": self._memory}[name]
            return self._require(client, name), slot, model or slot.model
        return self._endpoint(model)

    def _endpoint(self, model: str | None) -> tuple[AsyncOpenAI, LLMEndpointSettings, str]:
        vision = self._config.vision
        if model is not None and model == vision.model and self._vision is not None:
            return self._vision, vision, model
        chat = self._config.chat
        return self._require(self._chat, "chat"), chat, model or chat.model
```

- 五个能力方法（`chat` / `stream` / `chat_structured` / `chat_tools`）各加 keyword-only 形参 `endpoint: EndpointName | None = None`，并把函数体里的 `client, endpoint, resolved = self._endpoint(model)` 改成：

```python
        client, slot, resolved = self._resolve_endpoint(model, endpoint)
```

随后把所有 `endpoint.` 改成 `slot.`（注意 `_log_done(begin, endpoint, resolved, ...)` 的实参也要换成 `slot`）。**这一步最容易漏改**——改完先跑一次全量 llm 测试再往下走。

**Step 4: 跑测试确认通过**

Run:
```bash
.venv/bin/python -m pytest tests/test_llm_service.py -q
uvx basedpyright core tests 2>&1 | tail -2
```

Expected: 全绿（含既有用例，一个都不能挂——这是「`endpoint=None` 行为不变」的回归锁）；basedpyright 0/0/0。

**Step 5: 提交**

```bash
git add core/config/settings.py core/llm/service.py tests/test_llm_service.py
git commit -m "feat(llm): 新增 memory 端点并支持显式端点选择"
```

---

## Task 3: 错误树与公开模型

**Files:**
- Create: `core/memory/__init__.py`
- Create: `core/memory/errors.py`
- Create: `core/memory/models.py`
- Test: `tests/test_memory_models.py`

**Step 1: 写失败的测试**

```python
def test_错误树同源() -> None:
    """四个子类都继承 MemoryError，调用方一次 except 就能兜住整层。"""
    for exc in (MemoryConfigError, MemoryStoreError, MemoryInputError, MemoryExtractError):
        assert issubclass(exc, MemoryError)


def test_观测拒绝空文本() -> None:
    """空观测没有意义，且会让后续逐条校验白白跑一遍。"""
    with pytest.raises(ValidationError):
        Observation(kind="dialogue", text="   ")


def test_观测默认值() -> None:
    obs = Observation(kind="narration", text="风起了")
    assert obs.speaker is None
    assert obs.participants == []
    assert obs.world_time is None


def test_记忆条目带溯源自段() -> None:
    item = MemoryItem(kind="utterance", text="风起了", source_id="u1", score=0.9)
    assert item.world_time is None
```

**Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_memory_models.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'core.memory'`。

**Step 3: 写实现**

`core/memory/errors.py`：`MemoryError` 基类 + 四个子类（字段见设计文档 §10.1），`__init__` 里把对账信息收成属性；docstring 写明「原始异常挂 `__cause__`」。

`core/memory/models.py`：

```python
class Observation(BaseModel):
    """一次被观察到的原始回合。不含抽取结果——抽取是模块自己的事。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    kind: Literal["dialogue", "narration", "scene"]
    text: str
    speaker: str | None = None
    participants: list[str] = []
    world_time: int | None = None

    @field_validator("text")
    @classmethod
    def _needs_content(cls, value: str) -> str:
        """全空白等于没内容，早退比让下游逐层校验便宜。"""
        if not value.strip():
            raise ValueError("观测文本不能为空或全空白")
        return value
```

再加 `MemoryItem`（`kind` / `text` / `source_id` / `score` / `world_time`）与 `MemoryContext`（`block: str` / `items: list[MemoryItem]` / `trace: list[TraceStep]`），字段与设计文档 §7.1 对齐。`TraceStep` 用 `Literal["向量", "结构化", "图"]` 作来源标记。

`core/memory/__init__.py` 的 `__all__` 导出：`MemoryError` 及四个子类、`MemoryContext`、`MemoryItem`、`Observation`、`TraceStep`。**按 RUF022 的 ASCII 序排列**（大写在前，`MemoryConfigError` < `MemoryContext` < `MemoryError` < `MemoryExtractError` < `MemoryInputError` < `MemoryItem` < `MemoryStoreError` < `Observation` < `TraceStep`）。

> 本任务**不要**导出 `MemoryService`——它到 Task 9 才存在，此时导出会 ImportError。

**Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_memory_models.py -q && uv run ruff check core/memory`
Expected: 全绿、ruff 无输出。

**Step 5: 提交**

```bash
git add core/memory/__init__.py core/memory/errors.py core/memory/models.py tests/test_memory_models.py
git commit -m "feat(memory): 新增错误树与公开模型"
```

---

## Task 4: 存储会话（单线程执行器 + 事务）

**Files:**
- Create: `core/memory/store/__init__.py`
- Create: `core/memory/store/session.py`
- Test: `tests/test_memory_store_session.py`

**Step 1: 写失败的测试**

```python
async def test_会话可打开与关闭(tmp_path: Path) -> None:
    """打开即建库目录；关闭后不得再执行语句。"""
    session = MemorySession(tmp_path / "db", dimensions=4)
    await session.open()
    assert (tmp_path / "db").exists()
    await session.close()


async def test_同步语句经执行器跑通(tmp_path: Path) -> None:
    session = MemorySession(tmp_path / "db", dimensions=4)
    await session.open()
    result = await session.run("RETURN 1 AS x")
    assert result == [[1]]
    await session.close()


async def test_事务可回滚(tmp_path: Path) -> None:
    """建表后插一行、回滚，计数不变——证明 BEGIN/ROLLBACK 真的生效。"""
    session = MemorySession(tmp_path / "db", dimensions=4)
    await session.open()
    await session.run("CREATE NODE TABLE T(id STRING, PRIMARY KEY(id))")
    await session.run("BEGIN TRANSACTION")
    await session.run("CREATE (:T {id:'a'})")
    await session.run("ROLLBACK")
    assert await session.run("MATCH (t:T) RETURN count(t)") == [[0]]
    await session.close()


async def test_查询出错包成存储错误(tmp_path: Path) -> None:
    """Kùzu 的 RuntimeError 不能裸抛，否则调用方兜不住。"""
    session = MemorySession(tmp_path / "db", dimensions=4)
    await session.open()
    with pytest.raises(MemoryStoreError):
        await session.run("这不是 Cypher")
    await session.close()


async def test_未打开就执行报存储错误(tmp_path: Path) -> None:
    session = MemorySession(tmp_path / "db", dimensions=4)
    with pytest.raises(MemoryStoreError):
        await session.run("RETURN 1")
```

**Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_memory_store_session.py -v`
Expected: FAIL，`ModuleNotFoundError`。

**Step 3: 写实现**

`core/memory/store/session.py`：

```python
class MemorySession:
    """Kùzu 会话：唯一接触图库的地方。

    Kùzu 的 Python API 是同步且**线程不亲和**的，因此所有调用都丢进一个
    `max_workers=1` 的执行器——既把阻塞移出事件循环，又保证语句始终在同一个
    线程上跑（Kùzu 的 Connection 不是线程安全的）。
    """

    def __init__(self, db_path: Path, *, dimensions: int) -> None:
        self._db_path: Path = db_path
        self._dimensions: int = dimensions
        self._db: Any = None
        self._conn: Any = None
        self._executor: ThreadPoolExecutor | None = None

    @property
    def dimensions(self) -> int:
        """建表用的向量维度。"""
        return self._dimensions

    @property
    def opened(self) -> bool:
        """连接是否已建立。"""
        return self._conn is not None

    async def open(self) -> None:
        """建目录、开库、开连接。幂等：已打开直接返回。"""
        if self._conn is not None:
            return
        self._db_path.mkdir(parents=True, exist_ok=True)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="memory-store")
        self._db, self._conn = await self._in_thread(self._open_sync)

    async def close(self) -> None:
        """关连接与执行器。幂等：回滚时会作用在从未打开的实例上。"""
        conn, self._conn = self._conn, None
        db, self._db = self._db, None
        executor, self._executor = self._executor, None
        if conn is not None:
            await self._in_thread(lambda: conn.close())
        if db is not None:
            await self._in_thread(lambda: db.close())
        if executor is not None:
            executor.shutdown(wait=False)

    async def run(self, query: str, params: dict[str, object] | None = None) -> list[list[object]]:
        """执行一条语句并取回全部行。

        本层不解析 Cypher，参数按 Kùzu 的位置参数传入即可（本层内部一律用
        字面量拼接已转义的值，语句全部是模块内常量，不接受外部输入）。
        """
        conn = self._require()
        return await self._in_thread(lambda: _fetch(conn, query))
```

关键细节（必须照做）：

- `_fetch(conn, query)`：`result = conn.execute(query)`；用 `while result.has_next(): rows.append(result.get_next())` 取行（**不要用 `get_as_df()`**——它需要 numpy，本仓没装）。
- `_require()` 在 `self._conn is None` 时抛 `MemoryStoreError("会话尚未打开", operation=query[:40])`。
- `_in_thread(fn)`：

```python
    async def _in_thread[V](self, fn: Callable[[], V]) -> V:
        executor = self._executor
        if executor is None:
            raise MemoryStoreError("会话尚未打开，执行器不可用")
        return await asyncio.get_running_loop().run_in_executor(executor, fn)
```

- `_open_sync()` 里的 `import kuzu` **放在模块顶层**（本仓禁止在函数里躲 import），`kuzu.Database(str(self._db_path))`；同时执行 `INSTALL VECTOR` / `LOAD VECTOR` / `INSTALL FTS` / `LOAD FTS`（实测离线可用）。
- 所有 Kùzu 抛出的 `RuntimeError` 在 `run()` 里包成 `MemoryStoreError` 并挂 `__cause__`。
- `_wrap(...)` 内部用 `cast` 消掉 Kùzu 无类型标注带来的 `Any`（本仓 basedpyright 全 strict）。

`core/memory/store/__init__.py` 导出 `MemorySession`。

**Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_memory_store_session.py -q && uvx basedpyright core tests 2>&1 | tail -2`
Expected: 全绿、0/0/0。

**Step 5: 提交**

```bash
git add core/memory/store tests/test_memory_store_session.py
git commit -m "feat(memory): 新增 Kùzu 会话（单线程执行器与事务）"
```

---

## Task 5: 建表、索引与版本

**Files:**
- Create: `core/memory/store/schema.py`
- Test: `tests/test_memory_store_schema.py`

**Step 1: 写失败的测试**

```python
async def test_建表幂等(tmp_path: Path) -> None:
    """连建两次不报错——初始化在每次 start() 都会跑一遍。"""
    session = MemorySession(tmp_path / "db", dimensions=4)
    await session.open()
    await ensure_schema(session)
    await ensure_schema(session)
    names = {row[0] for row in await session.run("CALL show_tables() RETURN name")}
    assert {"Character", "StateChange", "Utterance", "Meta"} <= names
    await session.close()


async def test_向量维度写进表定义(tmp_path: Path) -> None:
    """FLOAT[d] 的维度必须在建表时确定，配置里的 dimensions 是唯一来源。"""
    session = MemorySession(tmp_path / "db", dimensions=8)
    await session.open()
    await ensure_schema(session)
    await session.run("CREATE (:Utterance {id:'u1', text:'甲', kind:'dialogue', importance:0.5, access_count:0})")
    with pytest.raises(MemoryStoreError):
        # 6 维向量写进 FLOAT[8] 列必须失败
        await session.run("CREATE (:Utterance {id:'u2', text:'乙', kind:'dialogue', embedding:[1.0,0.0,0.0,0.0,0.0,0.0], importance:0.5, access_count:0})")
    await session.close()


async def test_索引建一次就够(tmp_path: Path) -> None:
    """同名索引重复创建会报错，所以 ensure_schema 必须先查再建。"""
    session = MemorySession(tmp_path / "db", dimensions=4)
    await session.open()
    await ensure_schema(session)
    await ensure_schema(session)
    await session.close()


async def test_版本落后直接报错(tmp_path: Path) -> None:
    """本期不做自动迁移，版本对不上就明确失败，别带着半旧的表继续跑。"""
    session = MemorySession(tmp_path / "db", dimensions=4)
    await session.open()
    await ensure_schema(session)
    await session.run("MATCH (m:Meta {key:'schema_version'}) SET m.value = '0'")
    with pytest.raises(MemoryStoreError):
        await ensure_schema(session)
    await session.close()
```

**Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_memory_store_schema.py -v`
Expected: FAIL，`ImportError: cannot import name 'ensure_schema'`。

**Step 3: 写实现**

`core/memory/store/schema.py`：

```python
#: 建表 DDL 的版本；表结构一变就要 +1，ensure_schema 会拒绝版本不符的旧库。
SCHEMA_VERSION = "1"
```

`ensure_schema(session)` 顺序（全部用 `CREATE ... IF NOT EXISTS`，实测节点表与关系表都支持）：

1. 节点表：`Meta(key, value)`、`Character(id, name, aliases, kind)`、`StateChange(id, key, value, t_from, t_to, source_utterance)`、`Utterance(id, text, kind, world_time, embedding FLOAT[d], importance, access_count, last_access)`。
   - `embedding` 的维度用 `session.dimensions` 拼进 DDL（`f"FLOAT[{session.dimensions}]"`）。
   - 主键一律 `PRIMARY KEY(id)`；`t_to` / `world_time` 可空。
2. 关系表：`HAS_STATE(FROM Character TO StateChange)`、`MENTIONS(FROM Utterance TO Character)`。
   > 期一不建 `Event` / `Reflection` / `RELATES` / `PARTICIPATES` / `CAUSES` / `SUPPORTED_BY`——那是期二、期三的表，提前建就是预留。
3. 索引：`Utterance.embedding` 的向量索引与 `Utterance.text` 的 FTS 索引，**先查存在性再建**：

```python
async def _ensure_vector_index(session: MemorySession, table: str, name: str, column: str) -> None:
    existing = await session.run("CALL show_indexes() RETURN table_name, index_name")
    if any(row[0] == table and row[1] == name for row in existing):
        return
    await session.run(f"CALL CREATE_VECTOR_INDEX('{table}', '{name}', '{column}')")
```

> `CALL show_indexes()` 的确切列名**必须在实施时实测确认**（写一条探针打印首行）；若列名不同，只改这里的比较逻辑，不要改调用点。
4. 版本：读 `Meta` 里 `schema_version`：
   - 无记录 → `MERGE` 写入 `SCHEMA_VERSION`；
   - 等于 `SCHEMA_VERSION` → 通过；
   - 不等 → `raise MemoryStoreError(f"库的 schema 版本 ... 与代码 ... 不符", operation="ensure_schema")`。

**Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_memory_store_schema.py -q`
Expected: 全绿。

**Step 5: 提交**

```bash
git add core/memory/store/schema.py tests/test_memory_store_schema.py
git commit -m "feat(memory): 建表、索引与 schema 版本校验"
```

---

## Task 6: 有界队列

**Files:**
- Create: `core/memory/ingest/__init__.py`
- Create: `core/memory/ingest/queue.py`
- Test: `tests/test_memory_queue.py`

**Step 1: 写失败的测试**

```python
async def test_入队即返回() -> None:
    queue = ObservationQueue(capacity=4)
    await queue.put(_obs("甲"))
    assert await queue.get() == _obs("甲")


async def test_满队列丢弃并记数() -> None:
    """过载走丢弃 + 告警，与故障走死信是两回事（设计文档 6.5）。"""
    queue = ObservationQueue(capacity=2)
    assert await queue.put(_obs("甲")) is True
    assert await queue.put(_obs("乙")) is True
    assert await queue.put(_obs("丙")) is False
    assert queue.dropped == 1


async def test_关闭后拒绝入队() -> None:
    queue = ObservationQueue(capacity=2)
    queue.close()
    assert await queue.put(_obs("甲")) is False
```

**Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_memory_queue.py -v`
Expected: FAIL，`ModuleNotFoundError`。

**Step 3: 写实现**

`core/memory/ingest/queue.py`：`ObservationQueue` 包一层 `asyncio.Queue(maxsize=capacity)`：

- `put` 用 `put_nowait`，`QueueFull` → `dropped += 1` 并返回 `False`（**绝不阻塞调用方**）；
- `get` 用 `await self._queue.get()`；
- `close()` 置标志，之后 `put` 直接返回 `False`（让 `stop()` 期间的观测不至于吊死）；
- 暴露只读属性 `depth`（`qsize()`）与 `dropped`。

**Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_memory_queue.py -q`
Expected: 全绿。

**Step 5: 提交**

```bash
git add core/memory/ingest tests/test_memory_queue.py
git commit -m "feat(memory): 新增有界观测队列"
```

---

## Task 7: 重试与死信

**Files:**
- Create: `core/memory/ingest/retry.py`
- Test: `tests/test_memory_retry.py`

**Step 1: 写失败的测试**

```python
def test_瞬时故障可重试() -> None:
    """超时/连接/请求错误都是瞬时故障，重试有意义。"""
    assert is_retryable(MemoryStoreError("x", operation="q")) is False
    assert is_retryable(LLMTimeoutError("超时")) is True
    assert is_retryable(LLMConnectionError("断网")) is True
    assert is_retryable(LLMRequestError("503", status_code=503, endpoint="e", model="m", request_id=None)) is True
    assert is_retryable(LLMSchemaError("不符 schema")) is False
    assert is_retryable(MemoryInputError("空")) is False


async def test_退避后重试成功(tmp_path: Path) -> None:
    """第一次必败、第二次成功：尝试次数记 2，且不落死信。"""
    ...


async def test_重试用尽进死信(tmp_path: Path) -> None:
    """死信条带 utterance_id、阶段与尝试次数；文件是 append-only JSONL。"""
    ...


async def test_死信可被重放剔除(tmp_path: Path) -> None:
    """重放成功后条目被剔除，文件被重写。"""
    ...


async def test_永久故障零重试直接死信(tmp_path: Path) -> None:
    ...


def test_退避基数可配成零() -> None:
    """退避基数抽成实例字段（来自配置），测试里可设 0，用例不必真等。"""
    ...
```

**Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_memory_retry.py -v`
Expected: FAIL，`ModuleNotFoundError`。

**Step 3: 写实现**

`core/memory/ingest/retry.py`：

```python
#: 瞬时故障的判定依据：超时 / 连接 / 请求错误。其余（schema 不符、输入非法、库错）重试无意义。
_RETRYABLE: tuple[type[BaseException], ...] = (
    EmbeddingTimeoutError, EmbeddingConnectionError, EmbeddingRequestError,
    LLMTimeoutError, LLMConnectionError, LLMRequestError,
    DecisionTimeoutError, DecisionConnectionError, DecisionRequestError,
)
```

- `is_retryable(exc) -> bool`：`isinstance(exc, _RETRYABLE)`。
- `RetryPolicy(max_attempts, backoff)`：`delay(attempt) -> float` 返回 `backoff * 2 ** (attempt - 1)`。
- `DeadLetter`：dataclass，字段 `utterance_id` / `stage` / `error_type` / `error_message` / `attempts` / `payload`（`dict | None`）/ `failed_at`；提供 `to_json()` 与 `from_json()`。
- `DeadLetterBox(path: Path)`：
  - `append(entry)`：以 `"a"` 模式写一行 JSON（`ensure_ascii=False`）；
  - `load() -> list[DeadLetter]`：逐行 `json.loads`，**坏行跳过并计数**（死信文件不能因为一行损坏就整份读不出来）；
  - `rewrite(entries)`：写临时文件再 `replace`（原子替换）。
- `async def run_with_retry(operation, *, policy, sleep=asyncio.sleep)`：把重试循环抽出来，`sleep` 注入以便测试设 0。返回 `(结果, 尝试次数)`，全败则抛最后一次异常。

> 阶段判定：调用方用字符串常量 `STAGE_UTTERANCE = "原文落库"` / `STAGE_STRUCTURED = "结构化"`，写进死信条。

**Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_memory_retry.py -q`
Expected: 全绿。

**Step 5: 提交**

```bash
git add core/memory/ingest/retry.py tests/test_memory_retry.py
git commit -m "feat(memory): 新增重试策略与死信队列"
```

---

## Task 8: Cypher 语句集

**Files:**
- Create: `core/memory/store/queries.py`
- Test: `tests/test_memory_queries.py`

**Step 1: 写失败的测试**

```python
def test_角色写入语句幂等语义() -> None:
    """Character 用 MERGE 写入；Kùzu 不会去重平行边，节点也一样要 MERGE 才幂等。"""
    query = MERGE_CHARACTER
    assert "MERGE" in query and "Character" in query
```

再补：`UPSERT_UTTERANCE` 含 `MERGE` 与 `Utterance`；`OPEN_STATE_CHANGES` 含 `t_to IS NULL`；`CLOSE_STATE_CHANGE` 含 `SET` 与 `t_to`。

**Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_memory_queries.py -v`
Expected: FAIL，`ImportError`。

**Step 3: 写实现**

`core/memory/store/queries.py`：只放模块级字符串常量（**唯一写 Cypher 的地方**，设计文档决策 18），配一个 `_escape(value: str) -> str` 做单引号转义（`'` → `\'`，并剔除反斜杠以外的控制字符），所有拼接都走它。常量清单：

| 常量 | 用途 |
| --- | --- |
| `MERGE_CHARACTER` | 按 `id` `MERGE` 角色，`ON CREATE` 填 `name` / `kind`，`ON MATCH` 合并 `aliases` |
| `FIND_CHARACTER_BY_NAME` | 精确 `name` 或 `aliases` 命中 |
| `UPSERT_UTTERANCE` | 按 `id` `MERGE` 原文（含向量、`importance`、`access_count`、`last_access`） |
| `MERGE_MENTIONS` | `Utterance → Character` 的 `MENTIONS` 边 |
| `OPEN_STATE_CHANGES` | 查某角色 `t_to IS NULL` 的当前状态 |
| `CLOSE_STATE_CHANGE` | 把某角色某个 key 的当前状态置 `t_to` |
| `INSERT_STATE_CHANGE` | 插一条新的 `StateChange` 并连 `HAS_STATE` |
| `UTTERANCE_COUNT` / `CHARACTER_COUNT` | 健康检查用的计数 |

**Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_memory_queries.py -q`
Expected: 全绿。

**Step 5: 提交**

```bash
git add core/memory/store/queries.py tests/test_memory_queries.py
git commit -m "feat(memory): 归拢 Cypher 语句与转义"
```

---

## Task 9: 服务骨架与写入路径

**Files:**
- Create: `core/memory/service.py`
- Modify: `core/memory/__init__.py`（导出 `MemoryService`）
- Test: `tests/test_memory_service.py`

**Step 1: 写失败的测试**

```python
def test_构造器声明与依赖一致() -> None:
    """容器按注解注入，dependencies 与 __init__ 形参必须逐一对齐。"""
    hints = get_type_hints(MemoryService.__init__)
    assert set(MemoryService.dependencies) == {
        hints["embedding"], hints["llm"], hints["decision"]
    }


async def test_观测落原文(tmp_path: Path) -> None:
    """observe 入队即返回 utterance_id；worker 消费后原文与向量都在库里。"""
    ...


async def test_观测载荷非法立刻报错() -> None:
    """输入校验发生在入队之前，不进队列。"""
    with pytest.raises(MemoryInputError):
        await service.observe(Observation(kind="scene", text=" "))


async def test_队列满丢弃并告警(tmp_path: Path) -> None:
    ...


async def test_嵌入失败重试后成功(tmp_path: Path) -> None:
    ...


async def test_嵌入一直失败进死信(tmp_path: Path) -> None:
    """原文落库失败时死信条必须带完整载荷，否则原文永久丢失。"""
    ...


async def test_停服务会取消worker(tmp_path: Path) -> None:
    """stop() 必须 cancel + await，且不吞 CancelledError。"""
    ...
```

**Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_memory_service.py -v`
Expected: FAIL，`ImportError: cannot import name 'MemoryService'`。

**Step 3: 写实现**

`core/memory/service.py`：

```python
class MemoryService(Service):
    """记忆服务：把「记住」与「想起」收成一个服务。"""

    name: ClassVar[str] = "memory"
    dependencies: ClassVar[tuple[type[Service], ...]] = (EmbeddingService, LLMService, DecisionService)

    def __init__(
        self,
        embedding: EmbeddingService,
        llm: LLMService,
        decision: DecisionService,
        config: MemorySettings,
    ) -> None:
        super().__init__()
        self._embedding: EmbeddingService = embedding
        self._llm: LLMService = llm
        self._decision: DecisionService = decision
        self._config: MemorySettings = config
        self._session: MemorySession | None = None
        self._queue: ObservationQueue | None = None
        self._deadletters: DeadLetterBox | None = None
        self._worker: asyncio.Task[None] | None = None
```

- `start()`：`MemorySession(config.db_path, dimensions=config.dimensions)` → `open()` → `ensure_schema()` → 建 `ObservationQueue` / `DeadLetterBox` → `self._worker = asyncio.create_task(self._consume())`。
- `stop()`：`queue.close()` → `worker.cancel()` → `await asyncio.gather(worker, return_exceptions=True)` → `session.close()`；全部幂等。
- `observe(observation)`：

```python
        queue = self._require_queue()
        utterance_id = new_id()          # 仓库内自增的 ULID 式字符串，见 models.py
        payload = observation.model_dump()
        if not await queue.put(payload):
            self.log.error("观测队列已满，丢弃本回合", 队列上限=self._config.queue_size, 丢弃数=queue.dropped)
            return utterance_id
        return utterance_id
```

  注意：入队前先做一次 `Observation.model_validate`（`observe` 的形参**收 `Observation`**，非法载荷在构造时就被 pydantic 拒掉；`MemoryInputError` 用于「类型正确但语义不可用」的情形，例如文本全空白——此时把 `ValidationError` 包成 `MemoryInputError`）。

- `_consume()`：`while True: payload = await queue.get()` → `await self._ingest(payload)`；整个循环体包 `try/except Exception`，**只记日志不中断**；`except asyncio.CancelledError: raise`（绝不吞）。
- `_ingest(payload)`：
  1. `vector = await self._embedding.embed(text)`，结果长度与 `config.dimensions` 不符 → 抛 `MemoryStoreError`；
  2. 用 `run_with_retry` 包住「阶段 A：`UPSERT_UTTERANCE`」；
  3. 瞬时故障重试、永久故障与用尽 → `deadletter.append(...)` 并 `return`；
  4. `self.log.info("原文已入库", 回合=utterance_id, 维度=len(vector), 尝试=attempts)`。
- `replay_deadletters()`：读死信 → 逐条重试一次 → `rewrite` 成功的剔除、失败的保留；**期一由显式调用与测试驱动，期三挂进定时循环**。
- `health()`：`session.opened` 与依赖状态；extra 报 `库路径` / `角色数` / `原文数` / `死信`；未开启时套 `not_running_detail(self.state)`。

**Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_memory_service.py -q && uvx basedpyright core tests 2>&1 | tail -2`
Expected: 全绿、0/0/0。

**Step 5: 提交**

```bash
git add core/memory/service.py core/memory/__init__.py tests/test_memory_service.py
git commit -m "feat(memory): 服务骨架与原文写入路径"
```

---

## Task 10: 角色与状态的显式写入

**Files:**
- Create: `core/memory/store/writes.py`
- Test: `tests/test_memory_writes.py`

**Step 1: 写失败的测试**

```python
async def test_重复建角色只留一个(tmp_path: Path) -> None:
    """MERGE 语义：同名角色反复写入不会变成两个节点。"""
    ...


async def test_状态变更关旧开新(tmp_path: Path) -> None:
    """写第二次时旧的 t_to 被填上、新的一条 t_to 为空——当前状态永远唯一。"""
    ...


async def test_当前状态只回报未关闭的那条(tmp_path: Path) -> None:
    ...


async def test_某时刻状态可回溯(tmp_path: Path) -> None:
    """同一角色同一 key 在 T 时刻只能查到当时有效的那条。"""
    ...
```

**Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_memory_writes.py -v`
Expected: FAIL，`ImportError`。

**Step 3: 写实现**

`core/memory/store/writes.py`（纯函数，收 `MemorySession`，不持有状态，便于期二让抽取流程直接复用）：

```python
async def merge_character(session, *, character_id, name, kind, aliases=()) -> None:
    """按 id 幂等写入角色；别名合并而不是覆盖。"""


async def close_state(session, *, character_id, key, t_to) -> None:
    """把该角色该 key 当前有效的那条状态收口。"""


async def insert_state_change(session, *, change_id, character_id, key, value, t_from, source_utterance) -> None:
    """插入一条状态变更并连 HAS_STATE。"""


async def current_states(session, *, character_id) -> list[tuple[str, str]]:
    """当前状态：t_to IS NULL 的那些。"""


async def states_at(session, *, character_id, moment) -> list[tuple[str, str]]:
    """某时刻状态：区间谓词与 current 是同一个查询，只差一个条件。"""
```

`value` 一律存字符串（复杂值由调用方序列化成 JSON 字符串），这样 `StateChange` 不需要 MAP，也就绕开了实测里 MAP 的那些别扭。

**Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_memory_writes.py -q`
Expected: 全绿。

**Step 5: 提交**

```bash
git add core/memory/store/writes.py tests/test_memory_writes.py
git commit -m "feat(memory): 角色与状态的事件溯源写入"
```

---

## Task 11: 最小召回（语义 + 结构化 + 模板渲染）

**Files:**
- Create: `core/memory/recall/__init__.py`
- Create: `core/memory/recall/semantic.py`
- Create: `core/memory/recall/structured.py`
- Create: `core/memory/recall/assemble.py`
- Modify: `core/memory/service.py`（加 `recall`）
- Test: `tests/test_memory_recall.py`
- Test: `tests/test_memory_assemble.py`

**Step 1: 写失败的测试**

```python
async def test_语义召回按向量排序(tmp_path: Path) -> None:
    """近的那条排前面；返回带来源 id 与距离。"""
    ...


async def test_空库召回返回空块() -> None:
    """记忆为空是合法状态，不能报错。"""
    context = await service.recall("随便问问")
    assert context.block == ""
    assert context.items == []


def test_模板渲染分层() -> None:
    """渲染是纯函数：关系状态在前、近期原文在后；不含 LLM。"""
    block = render_block(states=[("心境", "平静")], utter), ...
    assert "心境" in block


async def test_召回的只读部分并发(tmp_path: Path) -> None:
    """语义与结构化各自 to_thread，互不依赖——用探针断言两路都跑到了。"""
    ...


async def test_超预算按相关性与重要度截断() -> None:
    """预算裁剪不按时间倒序。"""
    ...
```

**Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_memory_recall.py tests/test_memory_assemble.py -v`
Expected: FAIL，`ImportError`。

**Step 3: 写实现**

- `semantic.py`：`embed(query)` → `CALL QUERY_VECTOR_INDEX('Utterance','utt_vec',[..],k) RETURN node.id, node.text, distance`，包成 `MemoryItem(kind="utterance", ...)`。**结果别名固定用 `node`**（实测写成表名会报 `not in scope`）。
- `structured.py`：按 `focus` 角色取 `current_states`。
- `assemble.py`：
  - `render_block(states, items) -> str`：**确定性纯函数**，分层排版（先「此时的状态」，再「相关回忆」）；
  - 本期 `block` **一律走模板**（`block_by_llm` 在期三接 LLM 时才生效，配置项先读但只记进日志，不触发调用）；
  - 超 `recall.budget` 时按 `score × importance` 排序截断。
- `service.recall(query, *, focus=(), world_time=None, budget=None) -> MemoryContext`：`asyncio.gather` 并发跑语义与结构化，合并后渲染，最后把命中条目的 `access_count` / `last_access` **经队列交给 worker 回写**（不直写，避免与写路径并发）。

**Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_memory_recall.py tests/test_memory_assemble.py -q`
Expected: 全绿。

**Step 5: 提交**

```bash
git add core/memory/recall core/memory/service.py tests/test_memory_recall.py tests/test_memory_assemble.py
git commit -m "feat(memory): 最小召回与模板渲染"
```

---

## Task 12: 注册进容器

**Files:**
- Modify: `core/service/registry.py`
- Modify: `tests/test_service_manager.py`（注册顺序断言）
- Modify: `tests/test_main.py`（若有服务清单断言）

**Step 1: 写失败的测试**

在注册顺序断言里把期望改成：

```python
assert [type(s).__name__ for s in manager.services] == [
    "ClockService", "EmbeddingService", "LLMService", "DecisionService", "MemoryService",
]
```

**Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_service_manager.py -k 注册 -v`
Expected: FAIL，清单少一个。

**Step 3: 写实现**

`core/service/registry.py`：

```python
manager.register(ClockService, EmbeddingService, LLMService, DecisionService, MemoryService)
```

并更新该模块 docstring 里「反向依赖 embedding / llm / decision」的措辞（补上 memory 与「注册顺序即依赖顺序，最下游在最后」）。**注意不要动 `core/service/__init__.py`**——registry 有意不挂在它上面（会成环）。

**Step 4: 跑测试确认通过**

Run:
```bash
.venv/bin/python -m pytest -q 2>&1 | tail -3
uvx basedpyright core tests 2>&1 | tail -2
```

Expected: 全绿、0/0/0。

**Step 5: 提交**

```bash
git add core/service/registry.py tests/test_service_manager.py
git commit -m "feat(service): 注册记忆服务"
```

---

## Task 13: 文档与端到端验收

**Files:**
- Modify: `README.md`
- Modify: `.env.example`
- Modify: `data/config/cosmos.yaml`（若 Task 1 漏了注释）

**Step 1: 补文档**

- `README.md`：
  - 目录结构补 `memory/` 一行（描述「记忆层：原始回合与角色状态入库，向量 + 结构化召回」）；
  - 依赖方向那行补 `memory → (config, logger, service, embedding, llm, decision)`，并说明它是容器里最下游的服务；
  - `llm` 章节补第三端点 `memory`（含「只接受显式 `endpoint=`，不参与模型名匹配」这条约定）；
  - 配置项表补 `llm.memory.*` 与 `memory.*` 全部字段；
  - 新增「记忆层」章节，写清 `observe` / `recall` 两个口与本期边界。
- `.env.example`：补 `MEMORY_LLM_API_KEY`。

> README 里的 python 代码块会被 ruff 检查（`exclude = ["docs"]` 只排除 `docs`），写示例时必须能过 `ruff check .`。

**Step 2: 端到端冒烟**

Run:
```bash
cd /home/cosmos/项目/Aliya-cosmos/.worktrees/memory
timeout -s TERM 6 .venv/bin/python -m core.main
```

Expected: 进程正常启动、健康检查列出 `memory` 服务（无密钥时应为 unhealthy 且详情可读）、收到 SIGTERM 后优雅关闭。

> 若 `core/main.py` 的健康检查字段里出现「库路径」等新字段被保留名挡掉，按 `HEALTH_LOG_RESERVED_KEYS` 的既有规则处理（同名让位），**不要**去改保留名集合。

**Step 3: 全量判据**

Run:
```bash
.venv/bin/python -m pytest --cov=core 2>&1 | tail -3
uvx basedpyright core tests 2>&1 | tail -2
uv run ruff check . && uv run ruff format --check .
```

Expected: 覆盖率 100%；`0 errors, 0 warnings, 0 notes`；ruff 全绿。

**Step 4: 提交**

```bash
git add README.md .env.example data/config/cosmos.yaml
git commit -m "docs(memory): 补充记忆层配置与使用说明"
```

---

## 验收标准（期一）

1. `.venv/bin/python -m pytest --cov=core` = **100%**（`core/main.py` 维持 omit）。
2. `uvx basedpyright core tests` = `0 errors, 0 warnings, 0 notes`。
3. `uv run ruff check .` 与 `uv run ruff format --check .` 全绿。
4. `python -m core.main` 冒烟：无任何密钥也能启动，`memory` 服务健康检查可读，SIGTERM 优雅关闭。
5. 端到端：喂入若干回合后重启进程，原文与向量仍在（持久化）。
6. 端到端：角色状态写两次后，`t_to IS NULL` 的当前状态恰好一条，`states_at` 能回到旧值。
7. 端到端：注入一个必然失败的嵌入后，死信文件出现记录且带完整载荷；`replay_deadletters()` 能重放并剔除。
8. 回归：`tests/test_llm_service.py` 全部既有用例一字未改地通过（`endpoint=None` 行为不变）。

---

## 本期的显式不做（留给期二、期三）

| 项 | 期次 |
| --- | --- |
| LLM 抽取四类候选、实体归位与自动新建角色 | 期二 |
| `laya/jev` 双重门控 | 期二 |
| `Event` / `Reflection` 与事件相关的关系表、图多跳召回 | 期二、期三 |
| `RELATES` 关系演化 | 期三 |
| `consolidate` 定时循环（反思 / 衰减 / 删除 / 重放挂钩） | 期三 |
| `recall` 的 LLM 记忆块编排（`block_by_llm` 本期只读配置不调用） | 期三 |
