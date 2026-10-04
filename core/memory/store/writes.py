"""角色与状态的事件溯源写入。

本模块是 `queries.py` 里那批 Cypher 常量的**唯一调用点**：语句形状归 `queries.py`，
取值转义、拼装与事务编排归这里，两条职责不越界。

## 为什么全是收 `session` 的纯函数

不持有会话、不缓存任何东西：同一个函数既能被 Task 9 的写入路径调用，也能在期二让
抽取流程直接复用，还能在测试里对着任意临时库跑。会话由调用方持有并决定生命周期，
这里只负责「把这一次写做好」。

## 为什么要有 `record_state_change`

「同一键至多一条开放态」是 `StateChange` 的核心不变量，但 `CLOSE_STATE_CHANGE`
会把该键的**全部**开放条一次关掉（实测）——把「先关旧再开新」丢给调用方成对调用，
就是静默 bug 的来源：漏一次 late close，那个键就留下两条开放态，此后的「当前状态」
不再唯一。故把它收进数据层，并在**一个事务**里完成，失败整笔回滚。

## `value` 为什么一律是字符串

复杂值（列表、对象）由调用方序列化成 JSON 字符串再写进来。这样 `StateChange` 的
`value` 是一列 `STRING`，不必引入 `MAP`——实测 Kùzu 的 `MAP` 读单键要 `map_extract`
且返回列表、不能单键增删、字面量写法还容易撞 STRUCT，用它换来的那点表达力不值得。
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import suppress

from core.memory.errors import MemoryStoreError
from core.memory.models import new_id
from core.memory.store.queries import (
    CLOSE_STATE_CHANGE,
    INSERT_STATE_CHANGE,
    MERGE_CHARACTER,
    OPEN_STATE_CHANGES,
    STATES_AT,
    escape_literal,
)
from core.memory.store.session import MemorySession

__all__ = [
    "close_state",
    "current_states",
    "insert_state_change",
    "merge_character",
    "record_state_change",
    "states_at",
]


def _alias_literal(aliases: Sequence[str]) -> str:
    """把别名拼成 Kùzu 的原生 `STRING[]` 字面量，逐个过 `escape_literal`。

    `aliases` 是原生数组列，整体作为字面量赋给 `ON CREATE`，故必须先在 Python 侧
    拼成 `['a', 'b']` 的形状；空序列给出 `[]`（合法的空数组字面量）。
    """
    return "[" + ", ".join(f"'{escape_literal(alias)}'" for alias in aliases) + "]"


def _key_value_rows(rows: list[list[object]]) -> list[tuple[str, str]]:
    """把 `key, value` 两列的结果收成 `(str, str)` 元组表。

    列在库里都是 `STRING`，但 `get_next()` 的静态类型是 `object`，故在此一次性收窄，
    免得每个调用点各写一遍 `str(...)`。
    """
    return [(str(row[0]), str(row[1])) for row in rows]


async def merge_character(
    session: MemorySession,
    *,
    character_id: str,
    name: str,
    kind: str,
    aliases: Sequence[str] = (),
) -> None:
    """按 id 幂等写入角色；别名取并集而不是覆盖。

    反复写同一 id 只留一个节点（`MERGE`），后写的 `name` / `kind` **不覆盖**先写的
    ——身份一旦落定就是落定，改名属于另一个语义，不该由一次「确保存在」顺手改掉。
    别名走 `list_distinct` 合并，故传重复别名也不会写出重复项。
    """
    statement = MERGE_CHARACTER.format(
        id=escape_literal(character_id),
        name=escape_literal(name),
        aliases=_alias_literal(aliases),
        kind=escape_literal(kind),
    )
    _ = await session.run(statement)


async def close_state(session: MemorySession, *, character_id: str, key: str, t_to: int) -> None:
    """把该角色该键当前有效的那条状态收口（置 `t_to`）。

    **依赖单写者保证同键至多一条开放**：若有两条开放，这一条语句会把它们**一起**关掉。
    需要这个不变量的调用方请用 `record_state_change`，它在一个事务里把数量关系守住。
    """
    statement = CLOSE_STATE_CHANGE.format(
        key=escape_literal(key),
        t_to=str(t_to),
        char=escape_literal(character_id),
    )
    _ = await session.run(statement)


async def insert_state_change(
    session: MemorySession,
    *,
    change_id: str,
    character_id: str,
    key: str,
    value: str,
    t_from: int,
    source_utterance: str,
) -> None:
    """插入一条状态变更并连 `HAS_STATE` 到所属角色（开新）。

    `source_utterance` 是必填而非可选：设计 §5.4 第 3 条要求每次结构化写入都带来源，
    否则事后答不了「为什么状态变了」。签名字段不给默认值，就是为了让漏传在类型检查
    阶段就炸出来，而不是悄悄写进一个空来源。

    **节点缺失时是静默 no-op**：语句以 `MATCH (c:Character ...)` 起头，角色不存在
    则什么也不发生、也不报错。调用方需保证该角色 id 已经存在（先 `merge_character`）。
    """
    statement = INSERT_STATE_CHANGE.format(
        id=escape_literal(change_id),
        key=escape_literal(key),
        value=escape_literal(value),
        t_from=str(t_from),
        source=escape_literal(source_utterance),
        char=escape_literal(character_id),
    )
    _ = await session.run(statement)


async def current_states(session: MemorySession, *, character_id: str) -> list[tuple[str, str]]:
    """当前状态：`t_to IS NULL` 的那些，返回 `(key, value)`。

    同一键至多一条开放态，故同一键在结果里不会出现两次（不变量由
    `record_state_change` 守住）。
    """
    statement = OPEN_STATE_CHANGES.format(char=escape_literal(character_id))
    return _key_value_rows(await session.run(statement))


async def states_at(
    session: MemorySession, *, character_id: str, moment: int
) -> list[tuple[str, str]]:
    """某时刻状态（设计 §5.5）：与 `current_states` 是同一个查询，只差一个区间谓词。

    区间取**左闭右开** `[t_from, t_to)`：交界点上命中新值而不是旧值，故「先关旧再开新」
    时两条状态不会同时命中同一时刻，同一键在任意时刻至多一条有效值。
    """
    statement = STATES_AT.format(char=escape_literal(character_id), moment=str(moment))
    return _key_value_rows(await session.run(statement))


async def record_state_change(
    session: MemorySession,
    *,
    character_id: str,
    key: str,
    value: str,
    t_from: int,
    source_utterance: str,
) -> None:
    """一次事务内「先关旧、再开新」，把「同键至多一条开放态」收进数据层。

    关旧用 `t_to = t_from`：新状态从 `t_from` 起生效，旧状态的失效时刻正是它，
    于是两条状态在 `t_from` 上恰好首尾相接、无缝隙也不重叠（区间左闭右开）。

    三条语句（`BEGIN` / 关旧 + 插新 / `COMMIT`）都走 `session.run`，因而落在**同一个
    执行器线程**上；Kùzu 的连接不跨线程，这是事务能成立的前提。中途任何一步失败都
    `ROLLBACK` 后上抛——不留「旧条已关、新条没插进来」的空档。

    **回滚是尽力而为**：实测 Kùzu 有两类失败——语法/解析错会让事务**继续存活**，
    此时 `ROLLBACK` 能成功；而主键冲突这类约束错会让 Kùzu **自行中止**事务，事后
    `ROLLBACK` 反而报 "No active transaction"。所以回滚语句本身失败时不能让它顶掉
    原始故障（那会把「主键撞车」报成「没有活动事务」，排查时完全指错方向）——
    两种情况都以上抛**最初那个异常**为准，回滚失败只当它已经被 Kùzu 中止了。
    """
    change_id = new_id()
    statements = (
        CLOSE_STATE_CHANGE.format(
            key=escape_literal(key),
            t_to=str(t_from),
            char=escape_literal(character_id),
        ),
        INSERT_STATE_CHANGE.format(
            id=escape_literal(change_id),
            key=escape_literal(key),
            value=escape_literal(value),
            t_from=str(t_from),
            source=escape_literal(source_utterance),
            char=escape_literal(character_id),
        ),
    )
    _ = await session.run("BEGIN TRANSACTION")
    try:
        for statement in statements:
            _ = await session.run(statement)
    except BaseException:
        # 关旧与插新必须同生共死：不留「已关未开」的空档，也不留半开的痕迹。
        # 约束错已被 Kùzu 自行中止（实测），此时回滚无事可做；不顶掉原始故障。
        with suppress(MemoryStoreError):
            _ = await session.run("ROLLBACK")
        raise
    _ = await session.run("COMMIT")
