# 记忆层（归忆 / Remembrance-Lingering）设计

日期：2026-10-03
状态：设计已确认，待实施
基线：`main`（config / logger / service / llm / embedding / decision 各层就绪）

---

## 动工前实测补记（2026-10-03）

本文的存储与模型取舍全部建立在 Kùzu **0.11.3** 的实测结论上，以下每条都在本机跑过：

| 能力 | 实测结论 |
| --- | --- |
| 图多跳 | 路径模式 `(a)-[:R]->(:X)-[:R]->(c)` 可用 |
| 边属性 | 可在边上存 `kind` / `strength` / 整数区间并用于 `WHERE` |
| 平行边 | **允许，且不去重**：同端点同标签可插出两条完全相同的边 |
| 事务 | `BEGIN TRANSACTION` / `COMMIT` / `ROLLBACK` 生效，回滚后计数复原 |
| 时间切片 | `e.t_from <= T AND (e.t_to IS NULL OR e.t_to > T)` 一次命中 |
| 向量索引语法 | `CALL CREATE_VECTOR_INDEX('表','索引','列')`、`CALL QUERY_VECTOR_INDEX('表','索引',[..],k)`；**结果别名固定用 `node`**（`RETURN 表名.列` 会报 `not in scope`） |
| 向量索引增量 | 建索引**之后**插入的数据**立刻可召回**，无需重建 |
| 向量列更新 | **建索引后该列禁止 `SET`**（`Cannot set property ... used in one or more indexes`），要改只能删节点重插 |
| 同名索引 | 不可重复创建，需先 `DROP` |
| 全文索引 | `CALL CREATE_FTS_INDEX` / `CALL QUERY_FTS_INDEX`；增量插入立刻可命中；重建需先 `CALL DROP_FTS_INDEX` |
| 索引共存 | 同一张表上 FTS 与向量索引可共存，各自增量生效 |
| 删除 | 裸 `DELETE` 遇有边的节点直接报错，**必须 `DETACH DELETE`**；删后向量索引同步移除该节点 |
| `MAP` | 可用但不顺手：读单键必须 `map_extract(m,'k')` 且返回**列表**，没有 `map_contains_key`，不能单键增删；`{'k':'v'}` 字面量会被当 STRUCT 而失败，必须写 `map([...],[...])` |
| 保留名 | 表名不能叫 `Char`（撞内置类型），报 `already exists in catalog` |
| 扩展 | `INSTALL VECTOR` / `INSTALL FTS` / `LOAD ...` **离线可用**（wheel 自带，实测本机无 `~/.kuzu` 目录），无需联网 |
| 依赖 | 纯 Python 取值用 `has_next()` / `get_next()`，**不需要 numpy / pandas**（`get_as_df()` 才需要 numpy） |
| 建表幂等 | 节点表与关系表都支持 `CREATE ... IF NOT EXISTS` |
| 版本 | 0.11.3；`uv` 之外只新增 `kuzu` 一个依赖 |

---

## 〇、背景：归忆是什么

「归忆」是 `core/memory/` 的代号。它的文献基础见 `core/memory/docs/memory-literature.md`，选型落点如下：

| 文献脉络 | 本设计的落点 |
| --- | --- |
| MemGPT / MemOS 的分层与记忆调度 | 写入（`ingest`）、召回（`recall`）、固化（`consolidate`）三段式，各自独立成包 |
| HippoRAG 的图索引 + 多跳 | Kùzu 图上的关系多跳召回 |
| Zep / Graphiti 的双时态 | 角色属性用 `t_from` / `t_to` 区间表达有效时间 |
| Mem0 的提取–去重–更新流水线 | `ingest` 的 LLM 抽取 + `laya/jev` 去重与冲突判定 |
| MemoryBank 的遗忘曲线 | `consolidate` 的指数衰减 |
| Reflexion / ExpeL 的经验提炼 | `Reflection` 节点（时间窗 + 涉及角色的 LLM 总结） |
| A-MEM 的自动建链 | `Event` 与 `Utterance` 之间的 `SUPPORTED_BY` 、`Event` 之间的 `CAUSES` |

**场景定位：混合形态。** 用户以某个身份进入世界，Aliya 与其他 NPC 同处其中。记忆同时覆盖「我和你的关系」与「世界的演变」，因此世界里没有「主角 / 配角」之分——所有角色一视同仁地存进 `Character`。

---

## 一、目标与范围

新增 `core/memory/` 记忆层：一个**自带常驻循环的全自动观察者**，订阅上层喂入的原始回合，自主决定记什么、怎么记、何时忘。

**对外只有两个口**：

- `observe(观测)`：喂入原始回合，**入队即返回**，不等处理。
- `recall(情境) -> MemoryContext`：产出一段可直接塞进 prompt 的记忆块（外加可审计的结构化条目）。

**覆盖四类记忆**：角色本体、角色间关系、世界事件、对话与场景原文。

**失败可补偿**：写入失败按阶段重试（指数退避），重试用尽或不可重试则落**死信队列**，定时循环每轮重放一次。

**非目标（本次明确不做）**：

- 进程内向量模型 / 自研 ANN 索引。检索交给 Kùzu 的 HNSW，模型交给既有 `EmbeddingService`。
- 参数化记忆（知识编辑、模型微调）。三条主线里只取「明文记忆」。
- 跨模态记忆（图像 / 音频的独立记忆通道）。多模态能力在 `llm` 层已有，记忆层不单独建模。
- 软归档 / 回收站。遗忘即删（见决策 12）。
- 多世界隔离与多租户。**一库一世界**，世界的切换由上层换 `db_path` 解决。
- Kùzu 的自动 schema 迁移。只做「建表幂等 + 版本核对」，版本落后直接报错。
- HTTP / CLI 接口层。记忆层只做服务，暴露面由上层决定。

---

## 二、决策记录

| # | 议题 | 结论 | 被否决的方案与原因 |
| --- | --- | --- | --- |
| 1 | 场景定位 | 混合形态：用户也是角色，Aliya 与 NPC 同处一世界 | 单一伙伴关系（装不下「世界演变」）；旁观者式多角色世界（丢掉「我和你的关系」） |
| 2 | 记忆实体 | 四类全要：角色本体 / 关系 / 事件 / 对话原文 | 只取子集（四臂互相引用，缺一臂则溯源与多跳都断） |
| 3 | 写入驱动 | **全自动观察者**：模块自带常驻循环，自主决定记 / 反思 / 遗忘 | 纯被动库（把语义理解推给上层，等于把记忆的智能搬到别处）；纯 LLM 流水线（无自主性，无法反思与遗忘） |
| 4 | 召回形态 | **四合一**：语义 + 结构化 + 图多跳 + 上下文组装，一个 `recall` 出口 | 拆成四个方法（调用方要自己融合，等于把融合逻辑外泄） |
| 5 | 存储底座 | **Kùzu 一体**：图 / 关系 / 事件 / 原文 / 向量 / 全文都在一个嵌入式库 | SQLite + 自建向量（多跳要手写递归 CTE，且「向量数据库」名不副实）；Kùzu + LanceDB（多一套重依赖 + 两份存储的一致性）；NetworkX + LanceDB（图持久化与迁移全要自己写） |
| 6 | `laya/jev` 职责 | 四类判断全交给它：写入门控、去重/冲突、关系变化、召回重排与遗忘判定 | 只做其中一类（`decision` 层的延迟优势来自批量混问，分散调用是浪费）；全部交给 LLM（贵且慢，而这些都是分类/打分任务，正是 `decision` 层的主场） |
| 7 | 循环触发 | **事件 + 定时双轨**：回合级微提炼 + 周期级深反思与遗忘 | 纯事件（没有反思与遗忘的时机）；纯定时（回合级反馈太迟） |
| 8 | 写入门控 | **双重判定**：`laya` 与 LLM 各判一次，每次判「重要度」与「新颖度」两问，**四个条件取交集**；`laya` 层不过直接短路，不调 LLM | 单引擎单判据（README 已明说 Laya 零样本接近随机，单点误判直接污染记忆）；单引擎双判据（省 LLM，但判据同源、无法互相纠偏） |
| 9 | 关系的时间性 | **无时间轴**：`RELATES` 每对角色最多一条边，`kind` 与 `strength` 都是当前值、**原地覆盖** | 双时态区间边（用户明确「强度不留痕」，且实测平行边不去重会导致「改一条边命中多条」的静默污染） |
| 10 | 角色属性 | **纯事件溯源**：不存 `attributes` 快照，「当前状态」= `StateChange` 里 `t_to IS NULL` 的那些 | JSON 快照 + 变更日志（两份真相必然漂移）；`MAP` 列（实测读键要 `map_extract` 拿列表、不能单键增删，比事件溯源更别扭） |
| 11 | 记忆块渲染 | **交给 LLM**，确定性模板作为降级路径 | 只做模板（用户明确要求交给 LLM）；只做 LLM（缺密钥时 `recall` 整条挂掉，违反本仓「缺 key 不 fail fast」的既有约定） |
| 12 | 遗忘语义 | **即删**：`DETACH DELETE`，不做软归档；**只作用于记忆条目**（`Utterance` / `Event` / `Reflection`），`Character` / `RELATES` / `StateChange` 永不自动删除 | 软归档（用户明确要求即删）；骨架也纳入衰减（一次阈值误判会抹掉一个角色及其全部历史） |
| 13 | `Reflection` 的 scope | **时间窗 + 涉及角色**：一条反射 = 「这段时间里这些人发生了什么」 | 每角色一份传记（同一段对话被写了 N 遍，token 浪费且容易互相矛盾） |
| 14 | 记忆的 LLM 配置 | 新增第三个端点 **`llm.memory`**，与 `chat` / `vision` 同构，记忆通过注入的 `LLMService` 调用它 | `memory.llm` 自带端点（重试/超时/结构化输出/错误映射要在记忆层重写一遍）；只换模型不换端点（共用 `llm.chat` 的连接与密钥，做不到真正独立） |
| 15 | 第三端点的路由 | 五个能力方法各加 keyword-only `endpoint="chat"\|"vision"\|"memory"\|None`；**`None` 时行为与现在一字不变**，记忆**一律显式传 `"memory"`** | 把 `memory` 也纳入「按模型名匹配」的既有规则（README 已写明「两头 model 常常同名」，第三个端点一旦同名就会把 chat 调用抢走）；推翻现有规则改成必须显式（破坏既有 README 约定与全部用例） |
| 16 | 队列满时 | **有界队列（默认 256），满则记 ERROR 并丢弃该观测** | 无界队列（内存无上限）；阻塞等待（记忆不该拖慢对话） |
| 17 | 处理失败时 | **按阶段重试 + 死信队列**（见 6.5）：瞬时故障指数退避重试，用尽或不可重试则进 DLQ，`consolidate` 每轮重放一次 | ~~降级不重试~~（**用户改判**：失败会无声消失）；只重试不设死信（重试用尽后失败仍会消失）；DLQ 存 Kùzu（原文落库失败时库本身可能写不进去，DLQ 会跟着挂）；队列满也进 DLQ（过载 ≠ 故障，会让 DLQ 爆炸且重放永远追不上）；整条回滚（连原文都不留，违背「原始记忆优先」） |
| 18 | 面板层归属 | 记在 `core/memory/store/`，作为唯一接触 Kùzu 的地方 | 各包直连 Kùzu（连接与事务管理散落各处） |

---

## 三、配置层改动

### 3.1 `core/config/settings.py` 新增

```python
class MemoryGateSettings(BaseModel):
    """写入门控：双重判定（laya 与 LLM 各判一次，每次判重要度与新颖度）。"""

    backend: Backend = "laya"          # 第一层判定走哪个判断端点
    importance_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    novelty_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    skip_laya: bool = False            # True = 跳过第一层，直接进 LLM（laya 不可用时的应急开关）


class MemoryRecallSettings(BaseModel):
    """召回策略。"""

    top_k: int = Field(default=8, gt=0)          # 每张表各取多少条
    hops: int = Field(default=2, ge=1, le=3)     # 图多跳的跳数
    budget: int = Field(default=2000, gt=0)      # 记忆块的字符预算
    block_by_llm: bool = True                    # False = 恒走确定性模板


class MemoryDecaySettings(BaseModel):
    """遗忘衰减：score = importance * exp(-Δt/τ) * (1 + log(1 + access_count))。"""

    tau: float = Field(default=30.0, gt=0)       # 衰减常数（世界时间单位）
    floor: float = Field(default=0.05, gt=0.0, le=1.0)  # 跌破此值即删除


class MemoryConsolidateSettings(BaseModel):
    """定时循环。"""

    enabled: bool = True
    interval: float = Field(default=3600.0, gt=0)  # 周期（秒）
    min_events: int = Field(default=20, gt=0)      # 累积够多少条事件才触发一轮


class MemoryRetrySettings(BaseModel):
    """失败重试与死信。"""

    max_attempts: int = Field(default=3, ge=1)   # 单条最多重试次数（首次不计）
    backoff: float = Field(default=1.0, gt=0)    # 退避基数（秒），按 backoff * 2^n 递增
    deadletter_path: str = "data/memory/deadletter.jsonl"  # append-only，刻意独立于 Kùzu


class MemorySettings(BaseModel):
    """记忆层配置。"""

    db_path: str = "data/memory"                 # Kùzu 数据库目录（一库一世界）
    dimensions: int = Field(default=1024, gt=0)  # 向量列维度，必须与嵌入端点实际返回一致
    queue_size: int = Field(default=256, gt=0)
    gate: MemoryGateSettings = MemoryGateSettings()
    recall: MemoryRecallSettings = MemoryRecallSettings()
    consolidate: MemoryConsolidateSettings = MemoryConsolidateSettings()
    decay: MemoryDecaySettings = MemoryDecaySettings()
    retry: MemoryRetrySettings = MemoryRetrySettings()
```

并在 `Settings` 上加顶层字段 `memory: MemorySettings = MemorySettings()`，在 `LLMSettings` 上加 `memory: LLMEndpointSettings = LLMEndpointSettings()`。

> `dimensions` 为什么必填：Kùzu 的向量列是 `FLOAT[d]`，**维度必须在建表时就确定**，不能用 `EmbeddingSettings.dimensions`（那里 `None` 表示「用模型原始维度」，语义相反）。运行时每次写入复核向量长度，不符即抛 `MemoryStoreError`。

### 3.2 `data/config/cosmos.yaml` 新增

```yaml
llm:
  # ...既有 chat / vision...
  memory:                      # 记忆专用端点：抽取与记忆块渲染走这里
    base_url: ${MEMORY_LLM_BASE_URL:https://api.openai.com/v1}
    api_key: ${MEMORY_LLM_API_KEY:}
    model: gpt-4o-mini

memory:
  db_path: data/memory
  dimensions: 1024
  queue_size: 256
  gate:
    backend: laya
    importance_threshold: 0.5
    novelty_threshold: 0.5
    skip_laya: false
  recall:
    top_k: 8
    hops: 2
    budget: 2000
    block_by_llm: true
  consolidate:
    enabled: true
    interval: 3600
    min_events: 20
  decay:
    tau: 30
    floor: 0.05
  retry:
    max_attempts: 3
    backoff: 1.0
    deadletter_path: data/memory/deadletter.jsonl
```

`.env.example` 补 `MEMORY_LLM_API_KEY`。

> 提醒：本仓模型全部 `extra="ignore"`，写错段名会**静默失效**，改配置后必须实测 `load_settings()` 并与 `model_dump_json()` 基线对拍。

---

## 四、模块结构

```
core/memory/
  __init__.py       对外导出 MemoryService 与公开类型（Observation / MemoryContext / MemoryItem / 错误树）
  service.py        MemoryService：生命周期、两个对外端口、队列与 worker 的编排
  models.py         pydantic 模型：Observation、MemoryContext、MemoryItem、抽取候选、召回条目
  errors.py         MemoryError 及其子类
  store/
    schema.py       建表 DDL（幂等）、索引创建、版本核对
    session.py      Kùzu 连接与事务；所有同步调用的 to_thread 下沉点
    queries.py      Cypher 语句常量与小函数（按表聚合，唯一写 Cypher 的地方）
  ingest/
    queue.py        有界队列与背压策略（过载丢弃，与故障死信语义分开）
    retry.py        重试队列（退避调度）与死信队列（JSONL 追加、重放、剔除）
    extract.py      LLM 抽取四类候选（含门控第二层，一次调用同时产出判定与候选）
    gate.py         双重门控：laya 层短路 + LLM 层判定
    resolve.py      实体归位：名字/别名 → Character.id
    writer.py       单事务落库（StateChange 关旧开新、RELATES MERGE、Event、边）
  recall/
    semantic.py     向量召回（三张表各 Top-k）
    graph.py        关系多跳与关联角色
    structured.py   当前状态、当前关系、近期事件
    rerank.py       laya/jev 相关性重排
    assemble.py     LLM 编排记忆块 + 确定性模板降级
  consolidate/
    loop.py         周期任务与累积量触发；start/stop 的取消语义
    reflect.py      Reflection 生成
    decay.py        衰减计算与 DETACH DELETE
    merge.py        同义 StateChange / 冗余 Utterance 归一
```

**依赖方向**：`memory → (config, logger, service, embedding, llm, decision)`。它是容器里最下游的服务。

**服务契约**：

```python
class MemoryService(Service):
    name: ClassVar[str] = "memory"
    dependencies: ClassVar[tuple[type[Service], ...]] = (EmbeddingService, LLMService, DecisionService)

    def __init__(
        self,
        embedding: EmbeddingService,
        llm: LLMService,
        decision: DecisionService,
        config: MemorySettings,
    ) -> None: ...
```

`core/service/registry.py` 在既有三行后追加 `manager.register(MemoryService)`——它依赖前三个，注册在最后。

**生命周期**：

- `start()`：建 Kùzu 连接 → 幂等建表与索引 → 起 worker → 起定时循环。`enabled: false` 的依赖端点只警告不 fail fast（沿用既有惯例）。
- `stop()`：cancel 定时循环与 worker（`await` 收殓，**绝不吞 `CancelledError`**）→ 关 Kùzu。必须幂等。
- `health()`：Kùzu 可打开 + 依赖端点状态；extra 报 `库路径` / `角色数` / `事件数` / `条带`. 未启动时套 `not_running_detail(state)`。

---

## 五、数据模型

### 5.1 节点表（6 张）

| 表 | 字段 | 说明 |
| --- | --- | --- |
| `Character` | `id`(PK), `name`, `aliases`(STRING, JSON 数组), `kind` | 只有身份，**属性全部外置** |
| `StateChange` | `id`(PK), `key`, `value`, `t_from`, `t_to`, `source_utterance` | 角色属性的事件溯源 |
| `Event` | `id`(PK), `summary`, `world_time`, `place`, `importance`, `embedding FLOAT[d]` | 世界事件 |
| `Utterance` | `id`(PK), `text`, `kind`, `world_time`, `embedding FLOAT[d]`, `importance`, `access_count`, `last_access` | 对话与场景原文 |
| `Reflection` | `id`(PK), `scope`, `text`, `t_from`, `t_to`, `embedding FLOAT[d]` | 反思产物 |
| `Meta` | `key`(PK), `value` | 库的元信息（`schema_version`） |

### 5.2 关系表（6 张）

| 表 | 端点 | 字段 |
| --- | --- | --- |
| `HAS_STATE` | `Character → StateChange` | 无 |
| `RELATES` | `Character → Character` | `kind`, `strength`, `updated_at`, `source_utterance` |
| `PARTICIPATES` | `Character → Event` | `role` |
| `CAUSES` | `Event → Event` | 无 |
| `SUPPORTED_BY` | `Event → Utterance` | 无 |
| `MENTIONS` | `Utterance → Character` | 无 |

### 5.3 索引

```cypher
CALL CREATE_VECTOR_INDEX('Utterance',  'utt_vec',  'embedding');
CALL CREATE_VECTOR_INDEX('Event',      'evt_vec',  'embedding');
CALL CREATE_VECTOR_INDEX('Reflection', 'ref_vec',  'embedding');
CALL CREATE_FTS_INDEX('Utterance', 'utt_fts', ['text']);
CALL CREATE_FTS_INDEX('Event',     'evt_fts', ['summary']);
CALL CREATE_FTS_INDEX('Reflection','ref_fts', ['text']);
```

### 5.4 三条强制写入约束（全部由实测逼出）

1. **`RELATES` 必须 `MERGE` 而非 `CREATE`**。Kùzu 不去重平行边，「每对角色最多一条」这条契约只能靠 MERGE 保证；否则同一次关系判定重放就会留下两条边。
2. **向量列一次写定，永不变更**。建索引后该列禁止 `SET`；重新嵌入只能「删节点再插」。因此 `Utterance.embedding` / `Event.embedding` / `Reflection.embedding` 在插入后即视为不可变。
3. **每次结构化的写入都要带 `source_utterance`**（`StateChange` / `RELATES` 上）。否则事后无法回答「为什么关系变了」，回溯链断在这里。

### 5.5 当前状态与某时刻状态是同一个查询

```cypher
// 当前状态
MATCH (c:Character {id:$id})-[:HAS_STATE]->(s:StateChange)
WHERE s.t_to IS NULL
RETURN s.key, s.value;

// 时刻 T 的状态：把 s.t_to IS NULL 换成区间谓词即可
```

### 5.6 初始化与版本（幂等）

1. `CREATE NODE TABLE IF NOT EXISTS` / `CREATE REL TABLE IF NOT EXISTS` 建全部表（实测两者都支持）。
2. 读 `Meta.schema_version`：为空则写入当前版本；不等于当前版本则抛 `MemoryStoreError`（**不做自动迁移**，版本升级由实施计划单独处理）。
3. `CALL CREATE_VECTOR_INDEX` / `CALL CREATE_FTS_INDEX` 在索引不存在时才创建（同名索引重复创建会报错，且实测 FTS 需先 `DROP`）。

---

## 六、写入路径

```
observe(观测) ──► 有界队列 ──► worker（单协程串行）
  ① embed(text) → 原文落库（向量 + FTS）
  ② 用这枚向量取 Top-k 近邻 → 作为「新颖度」的对照集
  ③ 门控一层【laya 一次批量问两题】：重要度 score ≥ 阈值？ 新颖度 noul？
        —— 任一不过 ⇒ 只留原文，结束（短路，零 LLM 调用）
  ④ 门控二层 + 抽取【LLM 一次调用，endpoint="memory"】：
        · 双问判定（重要度 / 新颖度，基于原文与对照集）
        · 四类候选（角色 / 状态 / 事件 / 关系）
        —— 判定不过 ⇒ 丢弃候选，只留原文
  ⑤ 实体归位：名字 → Character.id（精确 name/aliases → FTS → 新建 Character）
  ⑥ 去重/冲突、关系变化判定【laya/jev 一次批量问】
  ⑦ 单事务写图
```

### 6.1 对外契约

```python
async def observe(self, observation: Observation) -> str:   # 返回 utterance_id
```

**入队即返回，绝不等 LLM。** 对话延迟与记忆处理彻底解耦。

```python
class Observation(BaseModel):
    kind: Literal["dialogue", "narration", "scene"]
    text: str
    speaker: str | None = None      # 角色 id 或名字
    participants: list[str] = []    # 可选：本回合的在场者
    world_time: int | None = None   # 故事内时间；缺省表示未知，不伪造
```

观测里**不含**抽取结果——抽取是模块自己的事。

### 6.2 六个关键取舍

1. **门控在抽取之前**：用原始文本先打廉价分，不值得记就只落原文、不进 LLM。顺序反过来既贵又慢。
2. **LLM 层把门控与抽取合成一次调用**：同一段文本、同一引擎，没必要分两次；但**判定不过就丢弃同一次返回的候选**，两件事在语义上仍然分开。
3. **队列有界，满则丢弃并记 ERROR**。丢的只是「还没结构化的回合」，绝不阻塞对话循环。
4. **worker 单协程串行**，天然满足 Kùzu 单写者；所有同步库调用经 `asyncio.to_thread` 下沉。
5. **失败按阶段补偿**：瞬时故障重试、永久故障与重试用尽落死信队列；**原文永不因失败而消失**（见 6.5）。
6. **实体归位三级**：精确 `name` / `aliases` → FTS 模糊 → 都不中则**新建 Character**。新角色自动入库，是「观察者」自主性的一部分。

### 6.3 双重门控的成本画像

- `laya` 层不过 ⇒ 0 次 LLM 调用（绝大多数「不值得记」的回合只花一次本地批量判定）。
- `laya` 层过了 ⇒ 1 次 LLM 调用（门控 + 抽取合并）。
- 另加 0～1 次 `laya/jev` 批量判定（去重/冲突、关系变化），与门控第三层可合并在同一次请求里混问。

### 6.4 风险与对策

README 已明说「Laya 是便于特化的基座，不是零样本决策引擎」，零样本准确率接近随机。因此：

- 两问的阈值必须可配（`gate.importance_threshold` / `gate.novelty_threshold`）。
- 默认取**保守值**（宁多记勿漏记）。
- 提供 `gate.skip_laya: true`，跳过第一层直接进 LLM——这是 Laya 不可用或判得离谱时的应急开关。
- **判定结果不用于删除原文**：无论门控怎么判，`Utterance` 一定落库。

### 6.5 失败重试与死信

**两类失败，两条出路**——这是本层的核心区分：

| 失败类型 | 判定依据 | 出路 |
| --- | --- | --- |
| **瞬时故障** | 超时 / 连接失败 / 429 / 5xx（下层抛 `*TimeoutError` / `*ConnectionError` / `*RequestError`） | 进**重试队列**，退避 `backoff × 2^n`，最多 `max_attempts` 次 |
| **永久故障** | schema 不符（`LLMSchemaError`）、输入非法（`MemoryInputError`）、Kùzu 报错（`MemoryStoreError`） | 直接进**死信队列** |
| 重试用尽 | — | 进**死信队列** |

**按阶段重试，不整条重来**：

- **阶段 A（原文落库）**失败 → 重试时用完整观测载荷重跑；DLQ 条目保存**完整载荷**。原文必须最终进库。
- **阶段 B（结构化：抽取 / 判定 / 落库）**失败 → 原文已在库里，DLQ 条目只记 `utterance_id` 与失败阶段；重放时从库里读原文即可，不重复占用队列载荷。

**死信队列落点：append-only JSONL 文件**（`memory.retry.deadletter_path`），**刻意不放进 Kùzu**——阶段 A 失败恰恰意味着库可能正写不进去，那时 DLQ 必须还能写。

**重放**：`consolidate` 每轮末尾扫一遍 DLQ，对未解决条目重试一次；成功的在下一轮重写文件时剔除；连续失败超过 `max_attempts` 的条目**保留但不再自动重试**（只记日志），避免无限循环。DLQ 条数进 `health()` 的 extra。

**为什么「队列满」仍然丢弃、而不是进 DLQ**（与决策 16 的分界）：队列满代表**过载**（对话比记忆快），把全部溢出塞进 DLQ 会让 DLQ 爆炸且重放永远追不上；**过载走丢弃 + ERROR 告警，故障走 DLQ**，两者不是一回事。

---

## 七、读取路径

```
recall(情境) -> MemoryContext
  情境 = { query: str, focus: list[str] = [], world_time: int | None = None, budget: int | None = None }
  ├─ ① 语义召回：embed(query) → Utterance / Event / Reflection 三张表各 Top-k（并发 to_thread）
  ├─ ② 图多跳：从 focus 出发 hops 跳 → 关联角色、共同事件、当前关系（并发）
  ├─ ③ 结构化：focus 的当前状态 + 当前关系 + 近期事件（并发）
  ├─ ④ 重排：laya/jev score「与情境的相关性」
  ├─ ⑤ 组装：LLM 编排记忆块（失败或缺 key 时退回确定性模板）
  └─ ⑥ 回写 access_count / last_access（经同一个 worker 串行）
```

### 7.1 返回结构

| 字段 | 内容 |
| --- | --- |
| `block: str` | 可直接塞进 prompt 的记忆块 |
| `items: list[MemoryItem]` | 结构化条目：类型 / 文本 / 来源 id / 分数 / 世界时间 |
| `trace: list[TraceStep]` | 每条的**依据**：向量距离、图路径、命中时间段 |

`items` 与 `trace` **无条件返回**，`block` 只是它的可读渲染；调用方永远拿得到事实底座，出问题也能解释「为什么想起了这个」。

### 7.2 五个取舍

1. **召回是只读的**，唯一副作用是命中条目的 `access_count` / `last_access` 回写（衰减要吃这两个字段）。回写**不能直写**——经同一个 worker 串行落库，否则与写路径并发写 Kùzu。
2. **只读部分可并发**：四路召回互不依赖，各走 `to_thread`，并发跑完再合并。
3. **记忆块默认交给 LLM**（`recall.block_by_llm: true`），输入是结构化的召回结果而非原始对话。
   - **降级**：LLM 不可用（未配 key / 端点未启用）→ 退回确定性模板，保证 `recall` 永不因缺 key 失败。
   - 模板渲染是纯函数，可单独测试。
4. **超预算按 `相关性 × 重要度` 截断**，不按时间倒序截断。
5. **无命中返回空块，不报错**——记忆为空是合法状态。

---

## 八、定时循环

`start()` 起一个后台 task，`stop()` 里 `cancel` + `await`（沿用容器约定）。每轮：

| 动作 | 做什么 |
| --- | --- |
| 反思 | 取一个时间窗内的事件与对话 → LLM 总结成 `Reflection`（scope = 时间窗 + 涉及角色，带 embedding） |
| 衰减 | `score = importance × exp(-Δt/τ) × (1 + log(1 + access_count))`，跌破 `decay.floor` → `DETACH DELETE` |
| 合并 | 同义重复的 `StateChange`（同 key 同时刻）与完全冗余的 `Utterance` 归一后再衰减 |
| 重放 | 扫一遍死信队列，对未解决条目重试一次；成功的下一轮剔除，连续超限的保留但不再自动重试 |

### 8.1 六个不变量

1. **双轨触发**：既按 `consolidate.interval`（秒），也按累积量（攒够 `min_events` 条事件才跑），避免空转。
2. **首轮延后一个周期**，不在启动瞬间抢占资源。
3. **所有写操作经同一个 worker 串行**——定时循环与写路径共享单写者。
4. **只删记忆条目**：`Utterance` / `Event` / `Reflection`。`Character` / `RELATES` / `StateChange` 是世界的骨架，**永不自动删除**，只接受显式指令。
5. **删除必须 `DETACH DELETE`**（裸 `DELETE` 遇有边的节点会直接报错）。
6. **单轮失败只记日志**，不中断循环；`consolidate.enabled: false` 时模块退化成纯写入 + 召回。
7. **死信重放不必成功**：重放失败只记日志，条目不剔除、留在 DLQ 等下一轮。

### 8.2 「遗忘即删」的必然配套

删除不可逆，所以三条强制项：① 每次删除都记一条结构化日志（删了什么、分数多少、为什么）；② 阈值与 `τ` 必须可配；③ 默认取保守值（宁留不删）。

---

## 九、LLM 层改动（`llm.memory` 第三端点）

### 9.1 风险：模型名路由会被第三个端点污染

现有 `LLMService._endpoint(model)` 的规则是「传的 model 命中 `vision.model` 且 vision 已启用 → vision，否则 chat」。README 已写明「两头的 model 常常同名」，因此 vision 用「未启用时不接管」来避免误伤。**第三个端点一旦也参与模型名匹配，chat 调用就会被记忆端点抢走。**

### 9.2 方案：显式端点优先，缺省行为一字不变

五个能力方法各加 keyword-only 参数：

```python
async def chat(
    self,
    messages: Sequence[ChatCompletionMessageParam],
    *,
    endpoint: Literal["chat", "vision", "memory"] | None = None,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> str: ...
```

- **`endpoint=None`（默认）⇒ 行为与现在完全一致**：仍按 model 在 chat / vision 之间匹配。既有 README 约定与全部用例不受影响。
- **记忆层一律显式传 `endpoint="memory"`**，不参与模型名匹配 —— 从根上杜绝「同名模型被记忆端点抢走」。
- 显式指定端点时，端点参数（`temperature` / `max_tokens` / `structured_mode`）取该端点配置，`model` 仍可覆盖。

这不是新风格：判断层的 `backend="jev" | "laya"` 就是同一套「显式选端点」的做法，此处只是推广到 llm 层。

### 9.3 连带改动

- `LLMSettings` 加 `memory: LLMEndpointSettings`；`LLMService` 加 `_memory` 客户端。
- `start()` 建三个客户端；`stop()` 关三个；`health()` 的 extra 增加 `记忆模型` / `记忆端点`，**三头全没客户端才 unhealthy**。
- 缺 key 仍只警告不 fail fast。
- **实现注意点**：五个能力方法体内原本把 `_endpoint()` 的返回值命名为 `endpoint`（`client, endpoint, resolved = ...`），它与新加的 keyword 形参 `endpoint=` **同名遮蔽**——加了形参后这一行会直接覆盖形参。需把方法体内的局部名改名（实施时用的是 `slot`）。`_endpoint()` 自身内部的局部名是 `vision` / `chat`，不受影响。

> **实施补记（2026-10-03）**：上述遮蔽点已按 `slot` 改名落地。另外端点选择在实施时收敛成**一张表**（`slots: dict[EndpointName, tuple[AsyncOpenAI | None, LLMEndpointSettings]]`），而不是链式三元——因为 `EndpointName` 是闭合的 `Literal`，字典查找可被类型检查证明穷尽，日后新增端点时漏改会在判型阶段暴露，而链式三元会静默落进最后的 `else`。（`EndpointName` 是实施期新引入的模块级类型别名，设计文档原文未列出。）

---

## 十、错误处理与日志

### 10.1 错误树

与 `LLMError` / `EmbeddingError` / `DecisionError` 同构：调用方一次 `except MemoryError` 兜住整层，下层异常包进来但挂在 `__cause__`。

| 类型 | 触发 | 属性 |
| --- | --- | --- |
| `MemoryConfigError` | 依赖端点未就绪（embedding 无 key / llm 无 key / decision 未启用）却发生调用 | `dependency` |
| `MemoryStoreError` | Kùzu 打开、建表、查询、事务失败（包装其 `RuntimeError`） | `operation` |
| `MemoryInputError` | `observe` 载荷非法（空文本、未知 kind） | 无 |
| `MemoryExtractError` | 抽取结果不符合 schema（包装 `LLMSchemaError`） | 无 |

**后台循环的异常不外抛**——只记日志继续跑。只有 `observe` / `recall` 两条请求路径抛异常。

**可重试性由错误类型判定**（见 6.5）：`*TimeoutError` / `*ConnectionError` / `*RequestError` 属瞬时故障 → 重试；`MemoryInputError` / `MemoryStoreError` / `LLMSchemaError` 属永久故障 → 直接死信。

### 10.2 日志

- 不记正文（沿用 llm / decision 的约定）：只记条数、耗时、token、命中数。
- 关键结构化字段：`阶段`(抽取/门控/落库/召回/反思/衰减)、`队列深度`、`丢弃数`、`门控结果`、`删除数`。
- 服务内日志用 `self.log`（自带 `[memory]` 前缀），报错用 `self.log_error(...)`。

---

## 十一、改动清单

| 文件 | 改动 |
| --- | --- |
| `pyproject.toml` | 新增依赖 `kuzu` |
| `core/config/settings.py` | 新增 `MemorySettings` 及其四个子配置；`LLMSettings.memory`；`Settings.memory` |
| `data/config/cosmos.yaml` | 新增 `llm.memory` 段与 `memory` 段 |
| `.env.example` | 补 `MEMORY_LLM_API_KEY` |
| `core/llm/service.py` | 第三端点 `_memory`；五个方法加 `endpoint=`；`_endpoint()` 内部变量改名 |
| `core/memory/**` | 新层（见第四节目录） |
| `core/service/registry.py` | 追加 `manager.register(MemoryService)` |
| `README.md` | 配置表补 `llm.memory` 与 `memory` 段；新增「记忆层」章节；目录结构补一行 |
| `tests/**` | 新增记忆层测试；`tests/test_llm_service.py` 补第三端点与显式路由用例 |

---

## 十二、测试策略

- **每用例一个库**：`tmp_path` 下的临时 Kùzu 目录，天然隔离；不共用 `:memory:`（便于断言落盘）。
- **依赖全假**：假 embedder / 假 `LLMService` / 假 `DecisionService`，白盒注入 `MemoryService` 的私有客户端（沿用既有各层的做法）。
- **定时循环不靠 sleep**：暴露 `consolidate_once()` 手动触发一轮；定时器本身只测「`start()` 起了任务」「`stop()` 取消了任务」。
- **Kùzu 交互不假**：图与索引行为直接用真库跑（已实测全部可用），这是本层最该锁死的部分。
- **必须有的用例**：
  1. `RELATES` 重复写入只留一条边（MERGE 生效）。
  2. 向量维度与 `memory.dimensions` 不符时报 `MemoryStoreError`。
  3. 门控短路：laya 不过时 LLM 零调用。
  4. 门控四条件取交集（四个分支各一条）。
  5. 失败降级：LLM 抽取抛错后原文仍在库中。
  6. 队列满时丢弃并记 ERROR，不影响后续观测。
  7. `recall` 在 LLM 不可用时退回模板且不抛错。
  8. 衰减只删记忆条目，`Character` / `RELATES` 在极端低分下仍存活。
  9. `DETACH DELETE` 后关联边与向量索引一并更新。
  10. `endpoint=None` 时 llm 路由行为与改动前逐条一致（回归锁）。
  11. 瞬时故障重试后成功：第 2 次尝试才落库，重试计数正确。
  12. 重试用尽进死信：DLQ 文件出现 1 条记录，带 `utterance_id` / 失败阶段 / 尝试次数。
  13. 永久故障不重试直接死信：`LLMSchemaError` 下重试次数为 0。
  14. DLQ 重放：`consolidate_once()` 后成功条目被剔除、文件被重写。
  15. 队列满与死信的分界：过载丢弃不写入 DLQ。
- 沿用基线：`uvx basedpyright core tests` = 0/0/0、`pytest --cov=core` = 100%。

---

## 十三、风险与取舍

| 风险 | 说明与对策 |
| --- | --- |
| **Laya 零样本质量差** | README 已警告零样本接近随机。对策：阈值可配、默认保守、`skip_laya` 应急开关；且门控**从不删原文**。 |
| **LLM 调用放大** | 每回合最多 2 次 LLM（门控+抽取合一、反思另计）。对策：laya 层短路掉绝大多数回合；`block_by_llm` 可关。 |
| **重试放大成本** | 瞬时故障重试会重复付费的调用。对策：`max_attempts` 上限、指数退避、退避基数可配（测试里 patch 成 0）。 |
| **死信队列膨胀** | 长期故障会让 DLQ 无限增长。对策：连续超限条目不再自动重试；DLQ 条数进 `health()` 可观测；路径可配。 |
| **硬删不可逆** | 对策：只删记忆条目；删除记结构化日志；阈值保守。 |
| **双轨循环的复杂度** | 后台任务与 worker 共享单写者，取消语义要严。对策：`stop()` 里 `cancel` + `await`，绝不吞 `CancelledError`；单轮失败不中断。 |
| **`recall` 依赖 LLM 会变慢** | 对策：模板降级路径常备；`block_by_llm` 可关。 |
| **Kùzu 单写者限制** | 所有写经同一 worker；召回的回写也排队。代价是召回无法并发写——可接受（回写是低频小事务）。 |
| **维度与模型耦合** | 换嵌入模型 = 向量列维度变化 = 必须重建库（向量不可 UPDATE）。对策：`dimensions` 进配置并在写入时复核；换模型时走「重建库 + 重放原文」的离线流程（本期不做，留待需要时）。 |
| **一库一世界** | 多世界要换 `db_path`。对策：明确写进非目标。 |

---

## 十四、验收标准

1. `uvx basedpyright core tests` = `0 errors, 0 warnings, 0 notes`。
2. `pytest --cov=core` = **100%**，`core/main.py` 维持 omit。
3. `uv run ruff check .` 与 `ruff format --check .` 全绿。
4. `python -m core.main` 冒烟：无任何密钥时进程正常启动，`memory` 服务健康检查为 unhealthy 且详情可读、优雅关闭。
5. 端到端实测：喂入若干回合后，`recall` 能召回相关记忆；重启进程后库中数据仍在（持久化）。
6. 端到端实测：`RELATES` 反复写入同一对角色，库中始终只有一条边。
7. 端到端实测：`consolidate_once()` 后低分条目被删、角色与关系仍在。
8. `endpoint=None` 的 llm 调用行为与改动前逐条一致（回归）。
9. 端到端实测：注入一个必然失败的抽取后，死信队列文件出现记录，`consolidate_once()` 能重放并清除。

---

## 十五、分期交付

每期各自走 worktree + design/plan，沿用既有节奏。

| 期 | 内容 | 产出判据 |
| --- | --- | --- |
| **一、地基** | `llm.memory` 第三端点与显式路由；`MemorySettings`；`store/`（建表、索引、版本）；写入 `Character` / `Utterance` / `StateChange`；**重试队列与死信队列**；最小 `recall`（向量 + 结构化，模板渲染）；门控暂为「全记」 | 能存能取、持久化、失败可补偿、llm 回归全绿 |
| **二、世界** | `Event` 与四张事件相关关系表；图多跳召回；`laya/jev` 双重门控；实体归位与新建角色 | 事件可溯源到原文，多跳能召回间接关联 |
| **三、演化** | `RELATES` 关系演化；`Reflection`；`consolidate`（反思 / 衰减 / 删除 / 合并）；`recall` 的 LLM 编排与降级 | 遗忘只删条目、骨架存活；记忆块由 LLM 生成 |

---

## 实施补记（留白）

实施期间与本文的偏差写在这里（体例同 `2026-10-02-decision-layer-design.md` 的「实施补记」）。
