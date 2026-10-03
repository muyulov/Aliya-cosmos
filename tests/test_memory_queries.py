"""记忆层 Cypher 语句集与转义测试。

`queries.py` 是本层**唯一**写 Cypher 的地方（设计文档决策 18）。本文件不满足于
「字符串里含 MERGE」这类形同虚设的断言——每条语句都要在**真实的临时 Kùzu 库**上
跑一遍，断言**行为**：幂等、不重复、区间语义、命中与落空。

最要紧的一组是转义：`escape_literal` 是外部文本进入 Cypher 的**唯一**通道，它是防注入与
防语法错的全部。Kùzu 的字符串字面量规则是**实测**出来的（不是照搬别处的想象）：

- 反斜杠**是**转义符：`\\` 表示一个反斜杠，`\'` 表示一个单引号。
- 因此**必须先转义反斜杠、再转义单引号**；顺序反了既会丢反斜杠又会触发语法错。
- 换行、制表符等控制字符**可以原样**进字面量并原样读回；但 `\\n` / `\\t` 这类
  反斜杠转义序列**不被识别**（会被吃成裸 `n` / `t`），所以控制字符必须原样直写。
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from core.memory import MemoryStoreError
from core.memory.store import MemorySession
from core.memory.store.queries import (
    CHARACTER_COUNT,
    CLOSE_STATE_CHANGE,
    FIND_CHARACTER_BY_NAME,
    INSERT_STATE_CHANGE,
    MERGE_CHARACTER,
    MERGE_MENTIONS,
    OPEN_STATE_CHANGES,
    UPSERT_UTTERANCE,
    UTTERANCE_COUNT,
    escape_literal,
)
from core.memory.store.schema import ensure_schema


def _角色(id_: str, name: str, aliases: list[str] | None = None, kind: str = "person") -> str:
    """拼一条 MERGE_CHARACTER 语句；别名列表走 `escape_literal` 与原生 STRING[] 字面量。"""
    别名 = aliases or []
    别名列表 = "[" + ", ".join(f"'{escape_literal(a)}'" for a in 别名) + "]"
    return MERGE_CHARACTER.format(
        id=escape_literal(id_),
        name=escape_literal(name),
        aliases=别名列表,
        kind=escape_literal(kind),
    )


def _话语(
    id_: str,
    text: str,
    *,
    kind: str = "dialogue",
    world_time: int | None = None,
    embedding: list[float] | None = None,
    importance: float = 0.5,
    access_count: int = 0,
    last_access: int | None = None,
) -> str:
    """拼一条 UPSERT_UTTERANCE 语句；可空列用 Cypher 的 `null` 关键字。"""
    向量 = "null" if embedding is None else "[" + ", ".join(repr(v) for v in embedding) + "]"
    世界时间 = "null" if world_time is None else str(world_time)
    末次访问 = "null" if last_access is None else str(last_access)
    return UPSERT_UTTERANCE.format(
        id=escape_literal(id_),
        text=escape_literal(text),
        kind=escape_literal(kind),
        world_time=世界时间,
        embedding=向量,
        importance=repr(float(importance)),
        access_count=str(access_count),
        last_access=末次访问,
    )


async def _起库(tmp_path: Path) -> MemorySession:
    """开一个 4 维的临时库并建好表，供各用例复用。"""
    session = MemorySession(tmp_path / "db", dimensions=4)
    await session.open()
    await ensure_schema(session)
    return session


# --------------------------------------------------------------------------- 角色


async def test_角色写入幂等且别名合并不丢(tmp_path: Path) -> None:
    """同一角色写两次只留一个节点；后写的别名与先写的合在一处，不覆盖。"""
    session = await _起库(tmp_path)

    _ = await session.run(_角色("c1", "阿黎", ["阿黎酱", "小黎"]))
    # 第二次换名字、只带一个新别名：name/kind 不覆盖，别名并集
    _ = await session.run(_角色("c1", "阿黎（改）", ["黎黎"], kind="other"))

    行 = await session.run("MATCH (c:Character {id:'c1'}) RETURN c.name, c.kind, c.aliases")

    assert len(行) == 1
    assert 行[0][0] == "阿黎"
    assert 行[0][1] == "person"
    assert sorted(cast("list[str]", 行[0][2])) == ["小黎", "阿黎酱", "黎黎"]

    await session.close()


async def test_按名字或别名精确命中(tmp_path: Path) -> None:
    """精确 name 命中、别名命中、都不中返回空——三种情形各验一次。"""
    session = await _起库(tmp_path)
    _ = await session.run(_角色("c1", "阿黎", ["甜栗子"]))

    命中名 = await session.run(FIND_CHARACTER_BY_NAME.format(name=escape_literal("阿黎")))
    命中别名 = await session.run(FIND_CHARACTER_BY_NAME.format(name=escape_literal("甜栗子")))
    落空 = await session.run(FIND_CHARACTER_BY_NAME.format(name=escape_literal("查无此人")))

    assert [行[0] for 行 in 命中名] == ["c1"]
    assert [行[0] for 行 in 命中别名] == ["c1"]
    assert 落空 == []

    await session.close()


async def test_角色计数(tmp_path: Path) -> None:
    """健康检查用的角色计数：空库为 0，写两个角色后为 2。"""
    session = await _起库(tmp_path)

    assert await session.run(CHARACTER_COUNT) == [[0]]

    _ = await session.run(_角色("c1", "甲"))
    _ = await session.run(_角色("c2", "乙"))

    assert await session.run(CHARACTER_COUNT) == [[2]]

    await session.close()


# --------------------------------------------------------------------------- 话语


async def test_话语写入幂等且向量落库(tmp_path: Path) -> None:
    """同一 id 写两次只留一行；4 维向量确实写进去且读回一致。

    第二次写入**不覆盖**文本：设计 §5.4 规定向量一次写定、永不变更，而文本与向量
    出自同一次嵌入，故「存在即原样返回」才是正确语义（Kùzu 也不允许 `SET` 已建索引的
    向量列，想覆盖也没有正当手段）。
    """
    session = await _起库(tmp_path)

    _ = await session.run(_话语("u1", "风起了", embedding=[1.0, 0.0, 0.0, 0.0]))
    _ = await session.run(_话语("u1", "风起了（重写）", embedding=[1.0, 0.0, 0.0, 0.0]))

    行 = await session.run("MATCH (u:Utterance {id:'u1'}) RETURN u.text, u.embedding, u.importance")

    assert len(行) == 1
    assert 行[0][0] == "风起了"
    assert list(cast("list[float]", 行[0][1])) == [1.0, 0.0, 0.0, 0.0]

    await session.close()


async def test_话语计数(tmp_path: Path) -> None:
    """健康检查用的话语计数：空库为 0，写两条后为 2。"""
    session = await _起库(tmp_path)

    assert await session.run(UTTERANCE_COUNT) == [[0]]

    _ = await session.run(_话语("u1", "一"))
    _ = await session.run(_话语("u2", "二"))

    assert await session.run(UTTERANCE_COUNT) == [[2]]

    await session.close()


async def test_提及边写两次不重复(tmp_path: Path) -> None:
    """Kùzu 不去重平行边：同一条 MENTIONS 连写两次必须仍只有一条边。"""
    session = await _起库(tmp_path)
    _ = await session.run(_角色("c1", "阿黎"))
    _ = await session.run(_话语("u1", "阿黎开口了"))

    连边 = MERGE_MENTIONS.format(utt=escape_literal("u1"), char=escape_literal("c1"))
    _ = await session.run(连边)
    _ = await session.run(连边)

    行 = await session.run(
        "MATCH (:Utterance {id:'u1'})-[m:MENTIONS]->(:Character {id:'c1'}) RETURN count(m)"
    )

    assert 行 == [[1]]

    await session.close()


# --------------------------------------------------------------- 状态三件套与区间


async def test_关旧开新后当前状态恰一条(tmp_path: Path) -> None:
    """先开一条当前状态，再「关旧 + 开新」，当前查询只能剩最新那条。"""
    session = await _起库(tmp_path)
    _ = await session.run(_角色("c1", "阿黎"))

    # 开一条：心情=平静
    _ = await session.run(
        INSERT_STATE_CHANGE.format(
            id=escape_literal("s1"),
            key=escape_literal("mood"),
            value=escape_literal("平静"),
            t_from="1",
            source=escape_literal("u1"),
            char=escape_literal("c1"),
        )
    )
    当前一 = await session.run(OPEN_STATE_CHANGES.format(char=escape_literal("c1")))
    assert [(行[0], 行[1]) for 行 in 当前一] == [("mood", "平静")]

    # 关旧（置 t_to=5）+ 开新（心情=激动，t_from=5）
    _ = await session.run(
        CLOSE_STATE_CHANGE.format(key=escape_literal("mood"), t_to="5", char=escape_literal("c1"))
    )
    _ = await session.run(
        INSERT_STATE_CHANGE.format(
            id=escape_literal("s2"),
            key=escape_literal("mood"),
            value=escape_literal("激动"),
            t_from="5",
            source=escape_literal("u2"),
            char=escape_literal("c1"),
        )
    )

    当前二 = await session.run(OPEN_STATE_CHANGES.format(char=escape_literal("c1")))

    assert len(当前二) == 1
    assert (当前二[0][0], 当前二[0][1]) == ("mood", "激动")

    await session.close()


async def test_某时刻状态用区间条件查到旧值(tmp_path: Path) -> None:
    """设计 §5.5：当前状态与某时刻状态是同一个查询，只差一个区间条件。

    一条状态占了 [1,5)，另一条从 5 起。取时刻 3 应命中旧值「平静」，
    取时刻 7 应命中新值「激动」——这正是「把 t_to IS NULL 换成区间谓词」的验证。
    """
    session = await _起库(tmp_path)
    _ = await session.run(_角色("c1", "阿黎"))
    _ = await session.run(
        INSERT_STATE_CHANGE.format(
            id=escape_literal("s1"),
            key=escape_literal("mood"),
            value=escape_literal("平静"),
            t_from="1",
            source=escape_literal("u1"),
            char=escape_literal("c1"),
        )
    )
    _ = await session.run(
        CLOSE_STATE_CHANGE.format(key=escape_literal("mood"), t_to="5", char=escape_literal("c1"))
    )
    _ = await session.run(
        INSERT_STATE_CHANGE.format(
            id=escape_literal("s2"),
            key=escape_literal("mood"),
            value=escape_literal("激动"),
            t_from="5",
            source=escape_literal("u2"),
            char=escape_literal("c1"),
        )
    )

    旧时刻 = await session.run(
        "MATCH (:Character {id:'c1'})-[:HAS_STATE]->(s:StateChange) "
        + "WHERE s.key = 'mood' AND s.t_from <= 3 AND (s.t_to IS NULL OR 3 < s.t_to) "
        + "RETURN s.value"
    )
    新时刻 = await session.run(
        "MATCH (:Character {id:'c1'})-[:HAS_STATE]->(s:StateChange) "
        + "WHERE s.key = 'mood' AND s.t_from <= 7 AND (s.t_to IS NULL OR 7 < s.t_to) "
        + "RETURN s.value"
    )

    assert 旧时刻 == [["平静"]]
    assert 新时刻 == [["激动"]]

    await session.close()


async def test_关闭只关指定键的当前状态(tmp_path: Path) -> None:
    """CLOSE 只动当前（t_to 为空）且 key 相符的那条，别键与已关的不受影响。"""
    session = await _起库(tmp_path)
    _ = await session.run(_角色("c1", "阿黎"))
    _ = await session.run(
        INSERT_STATE_CHANGE.format(
            id=escape_literal("s1"),
            key=escape_literal("mood"),
            value=escape_literal("平静"),
            t_from="1",
            source=escape_literal("u1"),
            char=escape_literal("c1"),
        )
    )
    _ = await session.run(
        INSERT_STATE_CHANGE.format(
            id=escape_literal("s2"),
            key=escape_literal("place"),
            value=escape_literal("树下"),
            t_from="1",
            source=escape_literal("u1"),
            char=escape_literal("c1"),
        )
    )

    _ = await session.run(
        CLOSE_STATE_CHANGE.format(key=escape_literal("mood"), t_to="9", char=escape_literal("c1"))
    )

    关掉的行 = await session.run("MATCH (s:StateChange {id:'s1'}) RETURN s.t_to")
    别键当前 = await session.run(OPEN_STATE_CHANGES.format(char=escape_literal("c1")))

    assert 关掉的行 == [[9]]
    assert [(行[0], 行[1]) for 行 in 别键当前] == [("place", "树下")]

    await session.close()


# --------------------------------------------------------------------------- 转义


@pytest.mark.parametrize(
    "原文",
    [
        "平凡的一句话",
        "it's a lie",  # 单引号
        "反斜杠\\在这里",  # 单反斜杠
        "反斜杠'与引号\\混着来",  # 反斜杠 + 单引号
        "换行\n在中间",  # 换行
        "制表\t符",  # 制表符
        "表情😀与中文🎏",  # emoji
        '双引号"无关紧要"',  # 双引号（不应被特殊处理）
        "\\'\\",  # 刁钻：反斜杠、引号、反斜杠
    ],
)
async def test_转义后原样读回(tmp_path: Path, 原文: str) -> None:
    """含引号/反斜杠/控制字符/emoji 的文本，转义后写进去必须原样读回。

    这是防注入与防语法错的全部：读回值等于原文，说明转义没有多丢字符、
    也没有把内容喂成语法错。
    """
    session = await _起库(tmp_path)

    _ = await session.run(_话语("u1", 原文, embedding=[0.0, 0.0, 0.0, 1.0]))
    行 = await session.run("MATCH (u:Utterance {id:'u1'}) RETURN u.text")

    assert 行[0][0] == 原文

    await session.close()


async def test_未转义的单引号会写坏语句(tmp_path: Path) -> None:
    """反面：跳过 `escape_literal` 直拼单引号，语句必然被喂成语法错而落不下去。

    这条用例同时是「防伪实验的锚」：若把 `escape_literal` 改成原样返回，上面那条
    参数化用例里含单引号的样本会跟着变红——证明转义真的在干活。
    """
    session = await _起库(tmp_path)

    _ = await session.run(_话语("u1", "还没事", embedding=[0.0, 0.0, 0.0, 1.0]))
    # 绕过 escape_literal：直接把含单引号的原文拼进字面量
    坏语句 = (
        "MERGE (u:Utterance {id:'u2'}) ON CREATE SET u.text = 'it's a lie', "
        "u.kind = 'dialogue', u.importance = 0.5, u.access_count = 0"
    )

    with pytest.raises(MemoryStoreError) as 捕获:
        _ = await session.run(坏语句)

    assert 捕获.value.operation == "run"

    await session.close()
