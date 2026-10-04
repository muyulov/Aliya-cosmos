"""记忆层建表、索引与 schema 版本。

`ensure_schema` 每次 `start()` 都会跑一遍，所以它**必须幂等**：

1. 节点与关系表一律 `CREATE ... IF NOT EXISTS`（实测 Kùzu 0.11.3 对两者都支持），
   重复执行只是空操作。
2. 向量索引与全文索引**不能**重复创建（同名会直接报错），故先 `CALL show_indexes()`
   查存在性，缺哪个补哪个。
3. 版本对账：`Meta.schema_version` 无记录则写入当前版本，相符则放行，
   **不符直接抛 `MemoryStoreError`**——本期不做自动迁移，带着半旧的表继续跑
   比明确失败危险得多。

只建期一真正会用到的表。`Event` / `Reflection` 与 `RELATES` / `PARTICIPATES` /
`CAUSES` / `SUPPORTED_BY` 是期二、期三的东西，提前建就是预留。
"""

from __future__ import annotations

from core.memory.errors import MemoryStoreError
from core.memory.store.session import MemorySession

#: 建表 DDL 的版本；表结构一变就要 +1，ensure_schema 会拒绝版本不符的旧库。
SCHEMA_VERSION = "1"

#: `Meta` 里记版本用的键名。
_VERSION_KEY = "schema_version"

#: 向量索引：(表名, 索引名, 列名)。
_VECTOR_INDEXES: tuple[tuple[str, str, str], ...] = (("Utterance", "utt_vec", "embedding"),)

#: 全文索引：(表名, 索引名, 列名)。本层只支持单列（`CREATE_FTS_INDEX` 收的是列表）。
_FTS_INDEXES: tuple[tuple[str, str, str], ...] = (("Utterance", "utt_fts", "text"),)


def _node_table_ddl(dimensions: int) -> tuple[str, ...]:
    """期一的节点表 DDL。

    `Utterance.embedding` 的维度**只能**由配置给定（`FLOAT[d]` 建表后不可改），
    故 DDL 在运行时按 `dimensions` 拼出。`world_time` / `last_access` / `t_to`
    允许为空——这三个值在写入那一刻未必知道。
    """
    return (
        "CREATE NODE TABLE IF NOT EXISTS Meta(key STRING, value STRING, PRIMARY KEY(key))",
        (
            "CREATE NODE TABLE IF NOT EXISTS Character("
            + "id STRING, name STRING, aliases STRING[], kind STRING, PRIMARY KEY(id))"
        ),
        (
            "CREATE NODE TABLE IF NOT EXISTS StateChange("
            + "id STRING, key STRING, value STRING, t_from INT64, t_to INT64, "
            + "source_utterance STRING, PRIMARY KEY(id))"
        ),
        (
            "CREATE NODE TABLE IF NOT EXISTS Utterance("
            + "id STRING, text STRING, kind STRING, world_time INT64, "
            + f"embedding FLOAT[{dimensions}], importance DOUBLE, access_count INT64, "
            + "last_access INT64, PRIMARY KEY(id))"
        ),
    )


#: 期一的关系表 DDL：角色持状态、话语提及角色。
_REL_TABLE_DDL: tuple[str, ...] = (
    "CREATE REL TABLE IF NOT EXISTS HAS_STATE(FROM Character TO StateChange)",
    "CREATE REL TABLE IF NOT EXISTS MENTIONS(FROM Utterance TO Character)",
)


async def _ensure_vector_index(session: MemorySession, table: str, name: str, column: str) -> None:
    """向量索引不存在时才建（同名重复创建会报错）。"""
    existing = await session.run("CALL show_indexes() RETURN table_name, index_name")
    if any(row[0] == table and row[1] == name for row in existing):
        return
    _ = await session.run(f"CALL CREATE_VECTOR_INDEX('{table}', '{name}', '{column}')")


async def _ensure_fts_index(session: MemorySession, table: str, name: str, column: str) -> None:
    """全文索引不存在时才建（同名重复创建会报错，重建需先 DROP）。"""
    existing = await session.run("CALL show_indexes() RETURN table_name, index_name")
    if any(row[0] == table and row[1] == name for row in existing):
        return
    _ = await session.run(f"CALL CREATE_FTS_INDEX('{table}', '{name}', ['{column}'])")


async def _ensure_version(session: MemorySession) -> None:
    """对账 `Meta.schema_version`：无则写入，相符放行，不符报错。"""
    rows = await session.run(f"MATCH (m:Meta {{key:'{_VERSION_KEY}'}}) RETURN m.value")
    if not rows:
        _ = await session.run(
            f"MERGE (m:Meta {{key:'{_VERSION_KEY}'}}) ON CREATE SET m.value = '{SCHEMA_VERSION}'"
        )
        return
    stored_version = rows[0][0]
    if stored_version != SCHEMA_VERSION:
        raise MemoryStoreError(
            f"库的 schema 版本为 {stored_version!r}，与代码要求的 {SCHEMA_VERSION!r} 不符；"
            + "本期不做自动迁移，请重建库或升级版本",
            operation="ensure_schema",
        )


async def ensure_schema(session: MemorySession) -> None:
    """建表、建索引、对版本，全部幂等，可重复执行。

    顺序照设计文档 §5.6：先表后索引再版本——索引要依赖表与列存在，
    版本检查放最后，免得一个旧库建到一半就报错。
    """
    for ddl in _node_table_ddl(session.dimensions):
        _ = await session.run(ddl)
    for ddl in _REL_TABLE_DDL:
        _ = await session.run(ddl)
    for table, name, column in _VECTOR_INDEXES:
        await _ensure_vector_index(session, table, name, column)
    for table, name, column in _FTS_INDEXES:
        await _ensure_fts_index(session, table, name, column)
    await _ensure_version(session)
