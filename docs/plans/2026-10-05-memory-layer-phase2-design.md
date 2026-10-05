# 记忆层（归忆）期二「世界」设计

日期：2026-10-05
状态：设计已确认，待实施
基线：`main`（期一「地基」已交付，见 `2026-10-03-memory-layer-design.md` 与 `2026-10-03-memory-layer-plan.md`）

---

## 〇、本期定位

期一把「能存能取」打通：原文落库（单协程 worker）、向量与结构化两路召回、模板渲染、重试与死信。放进图库的东西只有**角色、状态、原文**，`observe` 拿到什么就存什么，不做任何理解。

期二补上**理解**与**世界**：让模块自己从原文里看出「谁做了什么、跟谁有关、两人是什么关系」，把这些落成图上的事件与边，并让召回能顺着图走到**间接关联**的人与事。

期一留给本期的三件预留件，本期全部启用：`MemoryConfigError`、`MemoryExtractError`、`STAGE_STRUCTURED`。

---

## 一、目标与范围

**新增五类落库**：事件（`Event`）、参与（`PARTICIPATES`）、溯源（`SUPPORTED_BY`）、提及（`MENTIONS`，期一建表未写）、关系（`RELATES`）。

**新增一段理解流水线**：门控双层（laya 短路口 + LLM 判定）与 LLM 抽取（四类候选）、实体归位（精确 → FTS → 新建）、状态与关系的冲突判定。

**新增一路召回**：从 `focus` 出发的角色 → 事件 → 角色两跳，把共同事件与间接关联的人带进记忆块。

**产出判据**（承 §十五）：
1. **事件可溯源到原文** —— 每个 `Event` 都能顺 `SUPPORTED_BY` 摸回喂进来的 `Utterance`。
2. **多跳能召回间接关联** —— A、B 共同参与一事件后，`recall(focus=[A])` 能召回该事件并带出 B。

**非目标（本期明确不做）**：

| 项 | 留到 |
| --- | --- |
| `Reflection` 表与其生成 | 期三 |
| `consolidate` 定时循环（反思 / 衰减 / 删除 / 合并） | 期三 |
| `RELATES` 的**关系演化**（本期只首次建立，之后原地覆盖） | 期三 |
| `CAUSES` 的**写入**（本期只建表，跨回合因果要反思才看得到） | 期三 |
| `recall.block_by_llm`（LLM 编排记忆块）与模板降级 | 期三 |
| 冗余 `Utterance` / 同义事件的归一 | 期三 |
| 换嵌入模型后的「重建库 + 重放原文」离线流程 | 需要时 |
| 多世界 / 多租户、软归档、参数化记忆、跨模态记忆 | 不做 |

---

## 二、决策记录

| # | 议题 | 结论 | 被否决的方案与原因 |
| --- | --- | --- | --- |
| 1 | 本期边界 | **严格按 §十五 期二一次做完**：Event + 四张关系表 + 图多跳 + 双重门控 + 实体归位，走一个 worktree + design/plan | 拆成写/读两期（世界图是个整体，先写后读会让读路径无料可读）；只做写路径（判据二落空）；只做读路径（期一没有事件可读） |
| 2 | 依赖未就绪（`llm.memory` 无 key / decision 未启用） | **沿用期一分类**：包成 `MemoryConfigError` ⇒ 不可重试 ⇒ 阶段 B 每回合进死信，原文不失；膨胀靠期三「超限不再自动重试」收敛 | 跳过结构化只记降级态（用户明确要求与期一「缺 key ⇒ 不可重试 ⇒ 死信」保持一致）；启动时探测并整段旁路（配置改变不了，但把「不可用」藏进启动期，health 之外看不见） |
| 3 | 抽取产出 | **0..n 事件**；落 `MENTIONS`/`PARTICIPATES`/`SUPPORTED_BY` 与首次 `RELATES`；**`CAUSES` 只建表不抽** | 同回合内抽因果（单条回合的多事件大多是并列叙述，判因果噪声大）；至多一事件（一段叙述常含多个事件，压成一个会丢参与关系） |
| 4 | 多跳产出 | **分三处落，不改 `MemoryItem.kind`**：共同事件 ⇒ `items`；关联角色与当前关系 ⇒ block 新增「相关人物」段 + `TraceStep(source="graph")` | 把 `kind` 扩成五类（角色与关系是世界的**骨架当前态**，不是「记忆条目」，扩类会让调用方分支爆炸）；只当召回扩容器（判据二「召回间接关联」就变成了「召回了别的东西」） |
| 5 | 关系 `kind`/`strength` | **闭合词表 + laya 判定**：`kind` 由 `choice` 从代码常量词表里选，`strength` 由 `score` 给 0–1；「是否新关系 + 归类 + 打分」合在**一次批量 `predict`** 里 | LLM 抽取时自由给 `kind`（与决策 6「关系判定交给 laya/jev」相背，且词表会随对话漂移）；只记 `kind`、`strength` 恒为 1（`RELATES` 的演进本来就要吃强度，远期必然要补） |
| 6 | schema 升级 | **只加表的向前迁移**：`SCHEMA_VERSION` `"1"` → `"2"`；`stored < current` 补建新表后写回版本，`stored > current` 报错 | 一律报错（每加一层表就要手删一次开发库）；不升版本号（两个结构不同的库都自称 `"1"`，版本号失去意义） |
| 7 | 流水线结构 | **拆两段**：worker 只做「写」（阶段 A + 阶段 B 的落库指令），LLM 抽取与 laya 判定放独立并发任务（上限 `pipeline.extract_workers`，默认 4） | 维持单协程串行（吞吐降到 ~1/抽取耗时，队列满丢的是**原文**，且 recall 回写排到 LLM 后面）；阶段 B 独立单协程（简单，但慢回合仍在阶段 B 内排队，本可以并发） |
| 8 | 写入保序 | **`seq` + 重排缓冲 + 墓碑**：阶段 A 领单调递增 `seq`，阶段 B 带 `seq` 回交，worker 按序提交；失败/死信的 `seq` 交墓碑 | 按 `world_time` 排序（`world_time` 在配置里明确允许缺失，那时的错序是**静默**的）；改 `record_state_change` 容忍乱序（要拆接区间，复杂度远大于加一个缓冲） |
| 9 | 内部写入指令的队列语义 | **同一个队列、两个入口**：`put(观测)` 保留期一的「满则丢弃 + 计数」语义，新增 `put_internal(写入指令 / 墓碑 / 访问回写)` **不设界** | 内部指令与观测共用同一个有界入口（**丢一个 `StructuredResult` 会让它的 `seq` 永不抵达，重排缓冲永久卡死**，此后所有写入都停摆）；把队列整体改成无界（观测的背压闸门就没了）；另起一条无界队列 + 双队列 select（同一件事要两套取用逻辑） |
| 10 | `Event` 的访问列 | **本期顺手加上 `access_count` / `last_access`** | 不加（期三衰减要吃这两个字段，届时要再迁一次 schema） |
| 11 | 归位 FTS 的范围 | **只对 `Character.name` 建全文索引**；`aliases` 仍走精确命中（既有 `FIND_CHARACTER_BY_NAME` 已覆盖） | 对 `aliases` 也建 FTS（`aliases` 是 `STRING[]`，数组列能否进 FTS **未实测**，为一条未验证的能力押上归位的主路径不划算） |
| 12 | 判定结果的去向 | **门控判出的重要度回写 `Utterance.importance`** | 不回写（`fit_budget` 按 `相关度 × 重要度` 排序，而期一写死 `0.0` —— 不回写等于预算裁剪名存实亡） |
| 13 | 阶段 B 死信的重放 | **从库里读回 `text` 与 `embedding`** 重跑 | 重新调嵌入端点（重复付费，且把「重放」又挂回了 embedding 的可用性上） |
| 14 | 关系词表的形态 | **代码常量闭合词表**（`RELATION_KINDS`），不做配置 | 进配置（词表一旦可配，两个世界的关系类型就不可比，而 `RELATES.kind` 是要被 `choice` 严格匹配的；先让它稳定，需要再提） |

---

## 三、流水线架构（本期最大改动）

### 3.1 两段的划分

```
observe(观测)
  └─► 【观测队列】有界：深度 ≥ queue_size 即丢弃 + ERROR（丢的是原文，见 决策 9）
        └─► 阶段 A（worker 单协程，快）
              embed(text) → UPSERT_UTTERANCE → 领 seq（单调递增）
              └─► 交给阶段 B 调度器

阶段 B（并发任务，上限 pipeline.extract_workers）
  ① 用原文向量取 Top-k 近邻 → 「新颖度」对照集（只读，走会话的单线程执行器）
  ② 门控一层（laya 一次批量两问）；不过 ⇒ 短路，交回「只回写 importance」的写入指令
  ③ 门控二层 + 抽取（LLM 一次调用，endpoint="memory"）；判定不过 ⇒ 丢候选，只回写 importance
  ④ 实体归位（精确 → FTS → 新建）
  ⑤ 去重/冲突 + 关系判定（laya 一次批量）
  ⑥ 产出写入指令（带 seq）→ 交回【写入队列】

【写入队列】不设界（决策 9）：承载 阶段 B 的写入指令、墓碑、召回访问回写
  └─► worker：重排缓冲按 seq 顺序取，单事务落库
```

**为什么阶段 A 仍在 worker 上**：它只做「一次嵌入 + 一次插入」，与期一相同；且 `seq` 必须在**单一串行点**分配，worker 正是那个点。

**为什么阶段 A 的吞吐回到了期一**：LLM 不再占着 worker，观测队列满（丢原文）重新变回罕见事件。这正是拆两段的直接收益。

### 3.2 重排缓冲与墓碑

`seq` 在阶段 A 分配，因此**天然无竞争**。worker 侧维护 `next_seq` 与 `pending: dict[int, StructuredResult]`：

- 收到 `seq` 时放进 `pending`，然后 `while next_seq in pending:` 按序取出、落库、`next_seq += 1`；
- **过程 A 的每件结果都必须回交**，哪怕它失败了 —— 否则缺号会让缓冲永久停摆，此后**所有**写入都排队等着。因此阶段 B 的任务用 `try/finally` 保证「必交」，失败时交一枚**墓碑**（`plan=None`，只推进计数）；
- 死信与墓碑是两回事：死信是**记录**（写进 `deadletter.jsonl` 供重放），墓碑是**推进信号**（让缓冲不停）。一次失败同时产生两者。

### 3.3 落库粒度

一条观测的全部增量写收在**一个事务**里：角色 → 事件 → 边（`PARTICIPATES`/`SUPPORTED_BY`/`MENTIONS`/`RELATES`）→ 状态 → `importance` 回写。中途任何一步失败 ⇒ `ROLLBACK` 整笔（沿用 `record_state_change` 已实测的回滚语义：语法错时 `ROLLBACK` 有效；约束错已被 Kùzu 自行中止，回滚失败不顶掉原始故障）。

原文不受影响 —— 它在阶段 A 就已落库，这正是「原始记忆优先」的兑现。

### 3.4 生命周期

- `start()`：建会话 → `ensure_schema`（含 v2 迁移）→ 建队列与死信箱 → 起 worker。
- `stop()`：`close()` 观测队列 → `cancel` worker 与**全部在途的阶段 B 任务** → `gather(return_exceptions=True)` 收殓 → 关会话。**绝不吞 `CancelledError`**。阶段 B 任务在 `finally` 里交墓碑的逻辑要考虑「正在取消」：取消时交的墓碑已无意义（worker 也要退出了），但写成「`finally` 里只在未取消时交」会让代码多一个分支而收益为零，故**照交**，由队列的关闭语义兜住。

---

## 四、数据模型与 schema v2

### 4.1 新增节点表（1 张）

| 表 | 字段 | 说明 |
| --- | --- | --- |
| `Event` | `id`(PK), `summary`, `world_time`, `place`, `importance`, `access_count`, `last_access`, `embedding FLOAT[d]` | 世界事件；后两个访问列见 决策 10 |

`Reflection` **本期不建**：期三才用，提前建就是预留（沿用期一 `schema.py` 的同一原则）。

### 4.2 新增关系表（4 张，正好凑齐 §5.2 的六张）

| 表 | 端点 | 字段 |
| --- | --- | --- |
| `RELATES` | `Character → Character` | `kind`, `strength`, `updated_at`, `source_utterance` |
| `PARTICIPATES` | `Character → Event` | `role` |
| `CAUSES` | `Event → Event` | 无（本期只建表，无写入点，见 决策 3） |
| `SUPPORTED_BY` | `Event → Utterance` | 无 |

**`RELATES` 没有 `t_from`/`t_to`**：决策 9（期一设计）定死了「每对角色最多一条边，`kind` 与 `strength` 都是当前值、原地覆盖」，时间痕迹只在 `updated_at` 与 `source_utterance` 上。这正是「强度不留痕」的落地。

### 4.3 新增索引

```cypher
CALL CREATE_VECTOR_INDEX('Event', 'evt_vec', 'embedding');
CALL CREATE_FTS_INDEX('Event', 'evt_fts', ['summary']);
CALL CREATE_FTS_INDEX('Character', 'char_fts', ['name']);
```

- `char_fts` 为实体归位的模糊步服务，**只索引 `name`**（决策 11）。
- `_ensure_fts_index` 现只支持单列，本期的三个索引都是单列，**不需要扩**；若将来要索引 `aliases`，先在探针里确认数组列能否进 FTS。

### 4.4 schema 迁移（`"1"` → `"2"`）

`_ensure_version` 的语义从「不等即报错」改成三分支：

| 情形 | 动作 |
| --- | --- |
| 无 `schema_version` 记录 | 写入当前版本 |
| `stored < current` | **向前加表迁移**：同一次 `ensure_schema` 已把所有新表/索引 `CREATE ... IF NOT EXISTS` 建好，此处只把版本写回 |
| `stored == current` | 通过 |
| `stored > current` | **报错**（库比代码新，带着旧代码跑更危险） |

**为什么可以这么简单**：期二**只加表与索引，不动任何期一表的列**。`CREATE ... IF NOT EXISTS` 是幂等的加表动作，对旧库天然安全。**这条前提在未来任何一期都必须成立** —— 一旦某期要改列（例如给 `Utterance` 加字段），就必须换一套真正的迁移方案，不能再套用本分支。

### 4.5 公开模型零改动

`MemorySource` 已含 `event`、`TraceSource` 已含 `graph`，`MemoryItem` / `MemoryContext` / `TraceStep` / `Observation` **一个字都不改**。期二新增的都是模块内部模型（抽取候选、写入指令、多跳结果）。

---

## 五、写入路径

### 5.1 抽取 schema（一次 `chat_structured`，`endpoint="memory"`）

判定与抽取**合成一次调用**（期一设计 §6.2 第 2 条），但它们语义上仍是分开的两件事：判定不过就丢弃同一次返回的候选。

```
ExtractResult:
  importance: float            # 0–1，门控第二层的第一问
  novelty: float               # 0–1，门控第二层的第二问
  characters: [{name, aliases, kind}]
  states:     [{character, key, value}]
  events:     [{summary, place, world_time, importance,
                participants: [{character, role}]}]
  relations:  [{a, b}]         # 只给「这两人有互动」；kind/strength 不由 LLM 定（决策 5）
```

- `world_time` 缺失时**不伪造**（写入 `null`，与 `Utterance` 同一约定）。
- `relations` 只承载「候选对」，`kind` / `strength` 全交给 laya（决策 5）。
- `participants[].role` 由 LLM 给（如「发起者 / 旁观者」），落进 `PARTICIPATES.role`；这是**描述性**字段，不参与任何判定，故不需要闭集。

### 5.2 门控双层（四条件取交集）

```
laya 一次 predict（backend = gate.backend）
  · 重要度：score 原语，0–1
  · 新颖度：noul 原语，问「与已有记忆明显重复？」，取 1 − P(true)
  state = { 待判断内容: 原文, 已有相似记忆: [Top-k 邻居文本, ...] }

通过 = laya重要度 ≥ gate.importance_threshold
     ∧ laya新颖度 ≥ gate.novelty_threshold
     ∧ LLM重要度 ≥ gate.importance_threshold
     ∧ LLM新颖度 ≥ gate.novelty_threshold
```

- **laya 不过 ⇒ 短路**，不调 LLM（零 LLM 调用，这是双重门控的成本意义所在）。
- `gate.skip_laya = true` ⇒ 跳过第一层直接进 LLM（Laya 不可用或判得离谱时的应急开关，期一设计与 §6.4 都要求保留）。
- **无论怎么判，原文一定在库里**（判定结果从不用于删除原文，§6.4）。
- 对照集用 `recall.top_k` 条近邻，**不新增配置**。

### 5.3 判定结果的去向

- 重要度**回写 `Utterance.importance`**（决策 12）。期一写死 `0.0`，而 `fit_budget` 按 `相关度 × 重要度` 排序 —— 不回写等于预算裁剪名存实亡。
- 判定不过时也回写 importance（那条原文仍然是有质量的素材，只是不值得展开成结构化记忆）。

### 5.4 实体归位三级

| 级 | 依据 | 说明 |
| --- | --- | --- |
| ① 精确 | `name` 或 `aliases` 相等（先做空白与大小写归一） | 走既有 `FIND_CHARACTER_BY_NAME` |
| ② 模糊 | `char_fts` top-1 且 `score ≥ 阈值` | 阈值是模块常量，**实施时用探针定值**（Kùzu 的 FTS 分数刻度未实测，不能拍脑袋写） |
| ③ 新建 | 都不中 | `merge_character(id=new_id(), ...)`，`kind` 由抽取给、缺省填占位 |

新角色自动入库是「观察者自主性」的一部分（期一设计 §6.2 第 6 条）。

### 5.5 状态与关系判定（去重 / 冲突）

**状态**：对每个 `states` 候选，问一次 laya「新值 X 与当前值 Y 是否表达同一状态？」

- 同一状态 ⇒ **跳过**，不写 `StateChange`（免得同一状态反复关旧开新，把时间轴切成碎片）；
- 不同 ⇒ `record_state_change`（事务内关旧开新，`t_to = t_from`，区间左闭右开）。

**关系**：对每个候选对 `(a, b)`，合在**同一次 `predict`** 里问：

- `choice`：「这两人是什么关系？」—— 选项是 `RELATION_KINDS` 闭合词表，外加一个「无关系」的选项（判成「无」则不落边）；
- `score`：「关系有多深？」—— 0–1，落 `strength`；
- 落库走 `MERGE`（决策 9：Kùzu 不去重平行边，「每对至多一条」只能靠 `MERGE` 保证）。

**每次观测的外部调用上限 = 3**（1 次 laya 门控 + 1 次 LLM + 1 次 laya 判定）；laya 层短路时退化为 1 次。

### 5.6 失败语义（决策 2 / §6.5）

| 阶段 | 失败内容 | 死信载荷 | 同时交墓碑 |
| --- | --- | --- | --- |
| A（原文落库） | 嵌入 / 插入失败 | **完整观测载荷**（原文还没进库，这是唯一副本） | — （A 在 worker 上，无缓冲） |
| B（结构化） | 抽取 / 判定 / 归位 / 落库失败 | **只记 `utterance_id`** + `STAGE_STRUCTURED` | **必交** |

**依赖未就绪的处理**（决策 2）：把下层的 `LLMConfigError` / `DecisionConfigError` 在记忆层边界统一包成 `MemoryConfigError(dependency=...)`。它不在 `_RETRYABLE` 里 ⇒ 一次都不重试 ⇒ 直接死信。

> **一条硬约束**：只有 **schema 不符** 才包成 `MemoryExtractError`；`LLMTimeoutError` / `LLMConnectionError` / `LLMRequestError` 必须**裸抛**，否则会被判成永久故障、丢掉重试。

**死信重放（阶段 B）**：从 `Utterance` 一次 `MATCH` 读回 `text` 与 `embedding`（向量列**可以读**，只是建索引后禁止 `SET`），重跑阶段 B 全流程，并**领一个新的 `seq`** 走同一套重排缓冲 —— 这样重放与在途观测之间也自动保序。成功者下一轮剔除，失败者保留。

---

## 六、读取路径

### 6.1 四路合一（在期一的两路上扩两路）

```
recall(情境)
  ├─ ① 语义：embed(query) → Utterance 与 Event 两张表各 Top-k（并发提交）
  ├─ ② 图多跳：从 focus 出发两跳 → 共同事件、间接关联角色、当前关系
  ├─ ③ 结构化：focus 的当前状态（期一已有）
  ├─ ④ 重排 + 组装：裁剪 → 分层渲染 → trace
  └─ ⑤ 回写：命中的 Utterance 与 Event 的访问计数，经写入队列交给 worker
```

四路只读且互不依赖，全部 `asyncio.gather` 提交；实际执行仍在会话的**单线程执行器**上串行（Kùzu 连接不跨线程），并发只是不为无谓的先后等待买单。

### 6.2 语义召回扩到两张表

`SemanticHit` 现在写死 `kind="utterance"`，要泛化成「按表产出」：

| 表 | 索引 | `item.kind` | `item.text` | `item.score` |
| --- | --- | --- | --- | --- |
| `Utterance` | `utt_vec` | `"utterance"` | `text` | `1 / (1 + 距离)` |
| `Event` | `evt_vec` | `"event"` | `summary` | 同上 |

两表结果按 `source_id` 去重（同一 Event 可能同时被语义与图多跳命中）。

### 6.3 图多跳（新增 `recall/graph.py`）

- **起点**：`focus` 支持**角色 id 或名字**。名字先走一次归位（精确 → FTS）；**未命中就忽略，绝不新建** —— `recall` 是只读的，路过的名字不该在库里留下角色。
- **两跳模式**（设计 §5.1 实测「路径模式可用」）：
  - `(a:Character)-[:PARTICIPATES]->(e:Event)<-[:PARTICIPATES]-(b:Character)` ⇒ 共同事件 `e` 与间接关联角色 `b`；
  - `(a:Character)-[:RELATES]->(b:Character)` ⇒ 当前关系。
- **`recall.hops` 的消费**：本期支持 `hops = 2`。更长的可变长路径模式（`*1..2`）**未实测**，实施时先跑探针；不支持就按两跳写死，并把 `hops` 的取值域收紧到 `2`（配置项的 `ge=1, le=3` 暂不动，避免改坏期一的配置契约）。
- **产出分三处**（决策 4）：共同事件 ⇒ `MemoryItem(kind="event")` 进 `items`；关联角色与当前关系 ⇒ block 的**「相关人物」段** + `TraceStep(source="graph", detail="a -[kind]-> b")`。

### 6.4 组装扩展

`render_block` 的参数从 `(states, items)` 扩成 `(states, characters, items)`，三个段落：

```
【此时的状态】
- 心境：平静
【相关人物】
- 阿岚（友人，2）
【相关回忆】
- ...
```

`「相关人物」` 段的每条是「名字（关系类型，强度）」，没有关系时只写「名字」。段序固定：状态 → 人物 → 回忆。

`build_trace` 增收 graph 依据与两张表的向量依据。

### 6.5 预算与回写

- 裁剪仍是 `相关度 × 重要度` 降序，`Event` 条目一并参与；「相关人物」段与状态段一样是**必有段**，先扣其长度。
- 回写：`Utterance` 与 `Event` 各一条 `TOUCH_*` 语句，都经写入队列交给 worker（决策 9：内部指令不设界，不会因队列满而丢）。

---

## 七、配置层改动

### 7.1 `core/config/settings.py` 新增

```python
class MemoryPipelineSettings(BaseModel):
    """阶段 B 的并发上限。"""

    extract_workers: int = Field(default=4, gt=0)
```

`MemorySettings` 追加一个子段：

```python
    pipeline: MemoryPipelineSettings = MemoryPipelineSettings()
```

### 7.2 `data/config/cosmos.yaml` 追加

```yaml
memory:
  # ...既有...
  pipeline:
    extract_workers: 4
```

### 7.3 本期开始被消费的既有配置项

| 配置 | 先用在哪 |
| --- | --- |
| `gate.backend` | 门控一层与 §5.5 判定走哪个判断端点 |
| `gate.importance_threshold` / `gate.novelty_threshold` | 四条件交集的比较阈值 |
| `gate.skip_laya` | 跳过第一层 |
| `recall.hops` | 图多跳跳数（本期实际只用 2，见 6.3） |
| `pipeline.extract_workers` | 阶段 B 并发上限（新增） |

**仍只读不消费**：`recall.block_by_llm`、`consolidate.*`、`decay.*`（期三）。

> 提醒：本仓模型全部 `extra="ignore"`，写错段名会**静默失效**，改配置后必须实测 `load_settings()` 并与 `model_dump_json()` 基线对拍（期一计划 Task 1 的同一要求）。

### 7.4 代码常量

| 常量 | 位置 | 说明 |
| --- | --- | --- |
| `RELATION_KINDS` | `ingest/resolve.py` 或 `ingest/gate.py` | 关系 `kind` 的闭合词表（决策 14） |
| FTS 归位阈值 | `ingest/resolve.py` | 实施时用探针定值（5.4） |

---

## 八、错误处理与日志

### 8.1 错误树：零新增，三个预留件本期启用

| 类型 | 本期首次启用点 |
| --- | --- |
| `MemoryConfigError` | 依赖未就绪（`llm.memory` 无 key / decision 未启用 / embedding 无 key），`dependency` 报是哪一支 |
| `MemoryExtractError` | 抽取结果不符合 schema（包装 `LLMSchemaError`） |
| `STAGE_STRUCTURED` | 阶段 B 的死信阶段标记 |

**分层边界**：下层的 `*ConfigError` 在记忆层边界包成 `MemoryConfigError`；下层的可重试错误（`*TimeoutError` / `*ConnectionError` / `*RequestError`）**原样上抛**，不包、不改类型。

### 8.2 日志

沿用「不记正文」的约定，只记条数、耗时、token、命中数。本期新增的结构化字段：

`保序缓冲深度`、`next_seq`、`阶段B在途`、`门控结果`（四条件各自的判定）、`候选数`（角色/状态/事件/关系各几个）、`归位结果`（精确/模糊/新建各几个）、`关系变更`、`墓碑数`、`丢弃数`、`删除数`（期三用）。

服务内日志用 `self.log`（自带 `[memory]` 前缀），报错用 `self.log_error(...)`。

### 8.3 health 的 extra 扩展

在期一的 `库路径 / 角色数 / 原文数 / 死信 / 嵌入就绪` 之后追加：`事件数`、`关系数`、`保序缓冲深度`、`阶段B在途`、`结构化就绪`（`llm.memory` 与 `gate.backend` 对应的 decision 端点是否就绪）。

`healthy` 的判据**不变**：只看 Kùzu 是否打开（期一实施补记第 1 条的口径）。

---

## 九、模块结构与改动清单

### 9.1 模块结构（新增用「新」标注）

```
core/memory/
  service.py            worker 拆两段、重排缓冲、stop 收殓阶段 B
  models.py             公开模型零改动；内部模型（抽取候选 / 写入指令 / 多跳结果）
  ingest/
    queue.py            三类 job（PendingObservation / 写入指令 / 墓碑）+ 观测闸门移至 observe
    retry.py            不变（STAGE_STRUCTURED 本期首次被使用）
    extract.py      (新) 抽取 schema + chat_structured 调用
    gate.py         (新) 双重门控（laya 短路 + LLM 二层）
    resolve.py      (新) 实体归位三级 + RELATION_KINDS
    writer.py       (新) 写入指令 → 单事务落库
    pipeline.py     (新) 阶段 B 调度、并发上限、结果回交
  recall/
    semantic.py         泛化到 Utterance 与 Event 两张表
    graph.py        (新) 两跳与当前关系
    structured.py       不变（focus 归位放在 service 层做）
    assemble.py         「相关人物」段 + graph trace
  store/
    schema.py           v2 表 / 索引 / 三分支迁移
    queries.py          新增 Event 写入、四类边、图多跳、FTS 归位、Event 召回、TOUCH_EVENTS、读回原文+向量
    writes.py           新增 merge_event / merge_relates / link_participates / link_supported_by / read_utterance
```

依赖方向不变：`memory → (config, logger, service, embedding, llm, decision)`。

### 9.2 改动清单

| 文件 | 改动 |
| --- | --- |
| `core/config/settings.py` | 新增 `MemoryPipelineSettings`；`MemorySettings.pipeline` |
| `data/config/cosmos.yaml` | `memory.pipeline` |
| `core/memory/store/schema.py` | v2 表 / 索引 / 三分支版本迁移 |
| `core/memory/store/queries.py` | 新增本期全部 Cypher（仍守住「唯一写 Cypher 的地方」） |
| `core/memory/store/writes.py` | 事件与四类边的写入、读回原文 |
| `core/memory/ingest/**` | 新增 `extract` / `gate` / `resolve` / `writer` / `pipeline`；改造 `queue` |
| `core/memory/recall/**` | 新增 `graph`；泛化 `semantic`；扩展 `assemble` |
| `core/memory/service.py` | 拆两段、重排缓冲、stop 收殓 |
| `README.md` | 记忆层章节补事件与多跳；配置表补 `memory.pipeline.*` |
| `tests/**` | 新增期二用例；期一用例全绿。**三个文件必然要改**（见 §十 末的「地雷清单」），其余一字不改 |

> `.env.example` **无需改动**：`MEMORY_LLM_API_KEY` 期一已补。

---

## 十、测试策略

沿用期一的三条基线，加一条本期特有的：

1. **每用例一个库**：`tmp_path` 下的临时 Kùzu 目录，天然隔离。
2. **依赖全假**：假 embedder / 假 `LLMService` / 假 `DecisionService`，白盒注入 `MemoryService` 的私有依赖。
3. **Kùzu 交互不假**：图、边、索引的行为直接用真库跑（本期新增的 4 张关系表与 3 个索引都该在真库上锁死）。
4. **并发与保序不靠 sleep**：假抽取里挂 `asyncio.Event`，由用例决定哪个 `seq` 先完成，构造**确定性**的乱序，再断言落库顺序。

**必须有的用例**（15 条）：

| # | 用例 | 锁住什么 |
| --- | --- | --- |
| 1 | v1 库迁移到 v2：新表/索引补齐、旧数据仍在、版本写回 `"2"` | 决策 6 |
| 2 | 库版本**高于**代码 ⇒ 报错 | 决策 6 第三分支 |
| 3 | 门控短路：laya 不过 ⇒ LLM 零调用 | 双重门控的成本意义 |
| 4 | 门控四条件取交集（四个分支各一条） | 决策 2（期一设计） |
| 5 | 依赖未就绪 ⇒ 阶段 B 死信（`STAGE_STRUCTURED`、`payload=None`、`attempts==1`） | 决策 2 |
| 6 | **保序**：乱序完成后按 `seq` 顺序落库；缺号暂存等待 | 决策 8 |
| 7 | **墓碑**：`seq=1` 进死信后 `seq=2` 仍能落库，缓冲不卡死 | 决策 8 |
| 8 | 实体归位三级各一条（精确 / FTS / 新建） | 5.4 |
| 9 | `RELATES` 反复判定只留一条边，`kind`/`strength` 原地覆盖 | 决策 9（期一设计） |
| 10 | `SUPPORTED_BY` 指向原文；`MENTIONS`/`PARTICIPATES` 成边 | 判据一（事件可溯源） |
| 11 | 图多跳：A、B 的共同事件能被召回，B 进「相关人物」段 | 判据二（多跳召回间接关联） |
| 12 | `Event` 的向量召回（`evt_vec`）与访问计数回写 | 6.2 / 6.5 |
| 13 | 单事务：一条观测的增量写中途失败 ⇒ 整笔回滚（角色/事件都没留下） | 3.3 |
| 14 | 阶段 B 死信重放：从库里读回 `text`+`embedding` 重跑，成功后剔除 | 决策 13 |
| 15 | **回归**：期一既有用例全绿（三个必然要改的文件见下） | 不破坏既有契约 |

> 测试名不得含断言关键词（期一全局约束第 6 条），否则会被日志文件的 `tmp_path` 命中而假红。

### 10.1 地雷清单：期一这三个文件**必然要改**

这不是回归，是期二语义变更的直接后果。把清单钉在这里，是为了让「红了」与「坏了」一眼分得开。

| 文件 | 为什么会红 | 怎么改 |
| --- | --- | --- |
| `tests/memory/test_memory_store_schema.py` | ① `test_建表幂等` 是**等集**断言（六张表），期二必然多出五张；② `test_版本落后直接报错` 把版本设成 `'0'` 求报错，而决策 6 之后 `'0' < '2'` 属**向前迁移**、不再报错 | ① 表集合改成期二的十一张；② 该用例改写成「版本**高于**代码 ⇒ 报错」，另补一条「版本低于代码 ⇒ 迁移并写回当前版本」 |
| `tests/memory/test_memory_assemble.py` | `render_block` 的参数从 `(states, items)` 变成 `(states, characters, items)` | 按新签名传参；补「相关人物」段的断言 |
| `tests/memory/test_memory_service.py` | `_service` 挂具里塞的是 `LLMService.__new__(LLMService)` 与 `DecisionService.__new__(DecisionService)`（裸实例，无 `_config`）。期二阶段 B 会真的调用它们 ⇒ 每回合都抛 `AttributeError` ⇒ 进死信；于是 `test_嵌入失败重试后成功` / `test_嵌入连接错误也重试` 里 `_deadletters(service) == []` 的断言当场变红 | 挂具换成**能跑阶段 B 的假 LLM / 假 decision**（可开关「未就绪」）；并把「零死信」断言限定到阶段 A 的死信 |

**不受影响、必须保持一字不改的**（尤其要盯住）：

- `tests/memory/test_memory_queue.py` —— 正因为决策 9 保住了 `put`（观测专用、满则丢）的签名与语义，这个文件**一行都不用动**。若实施时把 `put` 改成通用入口，这个文件会整片变红，那就是走错了路。
- `tests/memory/test_memory_recall.py`、`test_memory_writes.py`、`test_memory_queries.py`、`test_memory_retry.py`、`test_memory_models.py`、`test_memory_store_session.py` —— 全部一字不改。
- 非记忆层的 `tests/**` —— 全部一字不改。

---

## 十一、风险与取舍

| 风险 | 说明与对策 |
| --- | --- |
| **并发引入的乱序** | 本期最大的新增风险。对策：`seq` 在唯一串行点分配 + 重排缓冲 + 墓碑；用例 6/7 直接锁死。 |
| **死信膨胀（决策 2 的代价）** | 未配 `llm.memory` 时每回合一条死信。对策：`health().extra.死信` 可观测；期三补「连续超限不再自动重试」。用户已明确接受。 |
| **外部调用量翻三倍** | 每回合最多 3 次（1 laya + 1 LLM + 1 laya）。对策：laya 层短路掉绝大多数回合；`gate.skip_laya` 应急降级；`pipeline.extract_workers` 可控并发。 |
| **Laya 零样本质量差** | 期一设计的同一风险。对策：阈值可配、默认保守、`skip_laya`；且门控**从不删原文**。 |
| **FTS 归位阈值难定** | Kùzu FTS 分数刻度未实测。对策：**实施时用探针定值**，先求「宁新建不误并」（归位错并比新建一个角色更难挽回）。 |
| **多跳路径模式未实测** | 可变长路径（`*1..2`）未验证。对策：先跑探针；不支持就按两跳写死。 |
| **单事务体积变大** | 一条观测可能要写十几个节点与边。对策：事务仍只覆盖一条观测的增量，量级可控；失败整笔回滚，原文不失。 |
| **观测队列满仍丢原文** | 决策 9 只保证**内部指令**不丢，不改变观测队列的丢弃语义。对策：阶段 A 变快后丢弃重新变回罕见事件；深度与丢弃数进 health。 |
| **`RELATES` 首次建立可能建错** | 一旦建错，期三的演化会以错的起点走下去。对策：`source_utterance` 必带（可回溯）；`strength` 可被后续覆盖；「无关系」选项让误建的概率下降。 |
| **两跳的覆盖面** | 只有「共同参与事件」一条主路径，没有共同提及也算不上多跳。对策：本期判据只要求「能召回间接关联」，进一步路径（如经 `RELATES` 再跳）留待需要时扩。 |

---

## 十二、验收标准

1. `uvx basedpyright core tests` = `0 errors, 0 warnings, 0 notes`。
2. `pytest --cov=core` = **100%**，`core/main.py` 维持 omit。
3. `uv run ruff check .` 与 `ruff format --check .` 全绿。
4. `python -m core.main` 冒烟：无任何密钥时进程正常启动、`memory` 服务健康检查可读、优雅关闭。
5. 端到端：喂入若干回合后，**每个 `Event` 都能顺 `SUPPORTED_BY` 摸回原文**（判据一）。
6. 端到端：A、B 共同参与一事件后，`recall(focus=[A])` 能召回该事件并带出 B（判据二）。
7. 端到端：本机期一建的 v1 库被**平滑升到 v2**，旧数据仍在。
8. 端到端：`memory` 端点无 key 时，`observe` 不阻塞、原文仍在、阶段 B 落死信且可显式重放。
9. 回归：期一既有用例全绿 —— 其中三个文件按 §10.1「地雷清单」改写（期二语义变更的直接后果），其余一字不改。

---

## 十三、与期三的边界

| 项 | 期三 |
| --- | --- |
| `Reflection` 表与反思生成 | ✓ |
| `consolidate` 定时循环（反思 / 衰减 / 删除 / 重放挂钩） | ✓ |
| `RELATES` 的关系演化（本期只首次建立） | ✓ |
| `CAUSES` 的写入（反思时判跨回合因果） | ✓ |
| `recall.block_by_llm` 与模板降级 | ✓ |
| 冗余 `Utterance` / 同义事件的归一（`merge.py`） | ✓ |
| 死信「连续超限不再自动重试」的计数 | ✓ |
| 死信重放里 `DeadLetterBox.rewrite` 的磁盘异常外抛（期一补记的两处已知缺口之二） | ✓ |

---

## 实施补记（留白）

实施期间与本文的偏差写在这里（体例同期一设计文档的「实施补记」）。
