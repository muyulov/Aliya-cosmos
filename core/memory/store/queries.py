"""归拢本层全部 Cypher 语句与字符串转义。

**这是本层唯一写 Cypher 的地方**（设计文档决策 18）：其余包一律不直写 Cypher，
要读写图库就去 `writes.py` 调对应函数，语句细节留在这里。

## 转义通道（先读这段再用）

外部文本（角色名、别名、话语原文……）**必须**经 `escape_literal` 之后才能拼进语句——
这是防注入与防语法错的**唯一**关口，任何绕过都是缺陷。

Kùzu 的字符串字面量规则是**实测**出来的，与 SQL 的直觉不同：

1. 反斜杠**是**转义符：字面量里 `\\\\` 表示一个反斜杠、`\\'` 表示一个单引号。
2. 因此 `escape_literal` **必须先转义反斜杠、再转义单引号**；顺序颠倒既会吞掉反斜杠，
   又会把 `'\''` 误喂成语法错（实测两种错法都能复现）。
3. 换行、制表符等控制字符**可以原样**进单引号字面量并原样读回；但 `\\n` / `\\t`
   这类反斜杠转义序列**不被识别**（会被吃成裸 `n` / `t`）。故控制字符原样直写，
   **不要**在 `escape_literal` 里对它们做任何处理。
4. 双引号在本层无特殊含义，不处理。

## 用法约定

- 需要注入取值的语句写成 `str.format` 模板，占位符形如 `{name}`；**调用方负责**
  把每个取值先过 `escape_literal`（或本身是数字/布尔/null 这类无需转义的 Cypher 字面量）。
- 无需取值的语句是普通常量，直接 `run(常量)` 即可。
- 占位符**故意**只收简单名字（如 `{name}`）：跑一遍 `format` 就能看出哪处漏了转义，
  命名一乱就看不出来了，故保持克制。
"""

from __future__ import annotations

__all__ = [
    "CHARACTER_COUNT",
    "CLOSE_STATE_CHANGE",
    "FIND_CHARACTER_BY_NAME",
    "INSERT_STATE_CHANGE",
    "MERGE_CHARACTER",
    "MERGE_MENTIONS",
    "OPEN_STATE_CHANGES",
    "UPSERT_UTTERANCE",
    "UTTERANCE_COUNT",
    "escape_literal",
]

# --------------------------------------------------------------------------- 角色

#: 按 id 落一个角色。`ON CREATE` 填 `name` / `kind` / 初始别名；
#: `ON MATCH` 把新别名并进 `aliases`（`list_distinct` 去重）——
#: `aliases` 是原生 `STRING[]`，**不能**直接赋值覆盖，否则后写的会把先写的冲掉。
#: 幂等：同一 id 写两次只留一个节点。占位符：`{id}` `{name}` `{aliases}` `{kind}`，
#: 其中 `{aliases}` 是由调用方拼好的、**已逐个转义**的列表字面量（形如 `['a', 'b']`）。
MERGE_CHARACTER: str = (
    "MERGE (c:Character {{id:'{id}'}}) "
    "ON CREATE SET c.name = '{name}', c.kind = '{kind}', c.aliases = {aliases} "
    "ON MATCH SET c.aliases = list_distinct(c.aliases + {aliases})"
)

#: 按精确 `name` 或别名命中角色（不走 FTS，走这条路的是「实体归位」的精确步）。
#: 占位符：`{name}`（已转义）。返回 `id` 列。
FIND_CHARACTER_BY_NAME: str = (
    "MATCH (c:Character) WHERE c.name = '{name}' OR '{name}' IN c.aliases RETURN c.id"
)

# --------------------------------------------------------------------------- 话语

#: 按 id 落一条话语原文（含向量与访问元数据），幂等：同一 id 写两次只留一行。
#:
#: **为什么不是 `MERGE ... ON CREATE SET`**（实测踩出的坑，别改回去）：
#: `Utterance.embedding` 建了向量索引之后，Kùzu **拒绝任何 `SET` 写这一列**
#: （报 "Cannot set property vec ... used in one or more indexes"），`ON CREATE SET`
#: 也是 `SET`，一样被拒；而把向量塞进 `MERGE` 的模式属性里又不能去重（第二次直接撞
#: 主键）。唯一可行且幂等的写法是「`OPTIONAL MATCH` 探存在性 → 不存在才 `CREATE`」，
#: 且向量必须**随创建一并落在节点模式里**。这也正合设计 §5.4「向量一次写定，永不变更」：
#: 存在即原样返回，重写不覆盖文本（文本与向量本就出自同一次嵌入）。
#:
#: 占位符：`{id}` `{text}` `{kind}` `{world_time}` `{embedding}` `{importance}`
#: `{access_count}` `{last_access}`；数值、向量与 `null` 由调用方直填，
#: 文本类（`{id}` `{text}` `{kind}`）须先过 `escape_literal`。
UPSERT_UTTERANCE: str = (
    "OPTIONAL MATCH (u:Utterance {{id:'{id}'}}) "
    "WITH u WHERE u IS NULL "
    "CREATE (n:Utterance {{id:'{id}', text:'{text}', kind:'{kind}', "
    "world_time:{world_time}, embedding:{embedding}, importance:{importance}, "
    "access_count:{access_count}, last_access:{last_access}}})"
)

#: 连一条 `Utterance → Character` 的 `MENTIONS` 边。
#: Kùzu **不去重平行边**（设计 §5.4），故必须 `MERGE` 而非 `CREATE`，否则同一次抽取
#: 重放会留下两条边。占位符：`{utt}` `{char}`（均已转义）。
MERGE_MENTIONS: str = (
    "MATCH (u:Utterance {{id:'{utt}'}}), (c:Character {{id:'{char}'}}) MERGE (u)-[:MENTIONS]->(c)"
)

# --------------------------------------------------------------------------- 状态

#: 查某角色**当前**（`t_to` 为空）的全部状态。占位符：`{char}`（已转义）。
#: 返回 `key, value`。设计 §5.5：某时刻状态就是这条，把 `t_to IS NULL` 换成区间谓词即可。
OPEN_STATE_CHANGES: str = (
    "MATCH (c:Character {{id:'{char}'}})-[:HAS_STATE]->(s:StateChange) "
    "WHERE s.t_to IS NULL RETURN s.key, s.value"
)

#: 把某角色某个 `key` 的**当前**状态置上 `t_to`（关旧）。
#: 只动 `t_to` 为空且 key 相符的那条，别键与已关的不受影响。
#: 占位符：`{key}` `{t_to}` `{char}`；`{key}` 与 `{char}` 须先过 `escape_literal`，`{t_to}` 是数字。
CLOSE_STATE_CHANGE: str = (
    "MATCH (c:Character {{id:'{char}'}})-[:HAS_STATE]->(s:StateChange) "
    "WHERE s.key = '{key}' AND s.t_to IS NULL SET s.t_to = {t_to}"
)

#: 插一条 `StateChange` 并连 `HAS_STATE` 到所属角色（开新）。
#: 写入必带 `source_utterance`，否则事后答不了「为什么状态变了」（设计 §5.4）。
#: 占位符：`{id}` `{key}` `{value}` `{t_from}` `{source}` `{char}`；
#: 除 `{t_from}` 是数字外，其余文本类须先过 `escape_literal`。`t_to` 落 `null`（新状态即当前）。
INSERT_STATE_CHANGE: str = (
    "MATCH (c:Character {{id:'{char}'}}) "
    "CREATE (s:StateChange {{id:'{id}', key:'{key}', value:'{value}', "
    "t_from:{t_from}, t_to:null, source_utterance:'{source}'}}) "
    "CREATE (c)-[:HAS_STATE]->(s)"
)

# --------------------------------------------------------------------------- 计数

#: 话语总数（健康检查用）。
UTTERANCE_COUNT: str = "MATCH (u:Utterance) RETURN count(u)"

#: 角色总数（健康检查用）。
CHARACTER_COUNT: str = "MATCH (c:Character) RETURN count(c)"


def escape_literal(value: str) -> str:
    """把外部文本转义成可安全嵌进 Kùzu 单引号字面量的内容。

    **本层唯一的转义通道**——所有外部文本在拼进 Cypher 前都必经此处；绕过它去直拼
    是缺陷。它是**公开**函数而非私有：上层（`writes.py` 等）本就要拿它把取值喂给
    本模块的模板，跨模块使用是常态，故不用下划线名前缀。

    规则（实测，见模块 docstring）：

    1. 先把 `\\` 换成 `\\\\`（**必须第一步**，否则下一步插进来的反斜杠会被二次转义）；
    2. 再把 `'` 换成 `\\'`。

    控制字符（换行、制表符）与 emoji 一律原样保留：它们能直接进字面量并原样读回，
    多此一举地处理反而会丢字符（Kùzu 不识别 `\\n` / `\\t` 这类序列）。
    """
    return value.replace("\\", "\\\\").replace("'", "\\'")
