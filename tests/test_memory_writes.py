"""记忆层「角色与状态」显式写入的行为测试。

`queries.py` 只提供语句常量，拼装与事务编排在 `writes.py`。本文件全部在**真实的临时
Kùzu 库**上跑，断言的是**行为**而不是字符串形状：

- 角色按 id `MERGE`：反复写同名角色只留一个节点，别名取并集而不是覆盖。
- 状态是事件溯源：写第二次时旧条被收口、新条开放，**当前状态恰有一条**。
- 「某时刻状态」与「当前状态」是同一个查询（设计 §5.5）：当前态就是把区间谓词退化成
  `t_to IS NULL`，故两者都从 `states_at` / `current_states` 出。
- **同一键至多一条开放态**这条不变量收在数据层（`record_state_change`），不再指望
  调用方成对调用 close + insert——Task 8 的审查实测发现 `CLOSE_STATE_CHANGE` 会把
  该 key 的**全部**开放条一次关掉，把不变量丢给调用方就是静默 bug 的来源。
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from core.memory import MemoryStoreError
from core.memory.store import MemorySession
from core.memory.store import writes as writes模块
from core.memory.store.schema import ensure_schema
from core.memory.store.writes import (
    close_state,
    current_states,
    insert_state_change,
    merge_character,
    record_state_change,
    states_at,
)


async def _起库(tmp_path: Path) -> MemorySession:
    """开一个 4 维的临时库并建好表，供各用例复用。"""
    session = MemorySession(tmp_path / "db", dimensions=4)
    await session.open()
    await ensure_schema(session)
    return session


# --------------------------------------------------------------------------- 角色


async def test_重复建角色只留一个(tmp_path: Path) -> None:
    """MERGE 语义：同名角色反复写入不会变成两个节点，别名合并不丢。"""
    session = await _起库(tmp_path)

    await merge_character(
        session, character_id="c1", name="阿黎", kind="person", aliases=("阿黎酱", "小黎")
    )
    # 第二次换名字、只带一个新别名：应当仍是同一个节点，别名取并集
    await merge_character(
        session, character_id="c1", name="阿黎（改）", kind="other", aliases=("黎黎",)
    )

    行 = await session.run("MATCH (c:Character {id:'c1'}) RETURN c.name, c.aliases")

    assert len(行) == 1
    assert 行[0][0] == "阿黎"
    assert sorted(cast("list[str]", 行[0][1])) == ["小黎", "阿黎酱", "黎黎"]

    await session.close()


# --------------------------------------------------------------------------- 状态


async def test_状态变更关旧开新(tmp_path: Path) -> None:
    """写第二次后旧条 t_to 被填、新条 t_to 为空，当前状态恰好一条。"""
    session = await _起库(tmp_path)
    await merge_character(session, character_id="c1", name="阿黎", kind="person")

    await record_state_change(
        session, character_id="c1", key="mood", value="平静", t_from=1, source_utterance="u1"
    )
    await record_state_change(
        session, character_id="c1", key="mood", value="激动", t_from=5, source_utterance="u2"
    )

    全条 = sorted(
        (行[0], 行[1], 行[2])
        for 行 in await session.run(
            "MATCH (s:StateChange) RETURN s.value, s.t_to, s.t_from ORDER BY s.t_from"
        )
    )

    # 旧条被收口在 t_from=5，新条仍开放（t_to 为空）
    assert 全条 == [("平静", 5, 1), ("激动", None, 5)]

    当前 = await current_states(session, character_id="c1")

    assert 当前 == [("mood", "激动")]

    await session.close()


async def test_当前状态只回报未关闭的那条(tmp_path: Path) -> None:
    """已关闭的历史值不出现在当前状态里；从未写过的角色回报空表而非报错。"""
    session = await _起库(tmp_path)
    await merge_character(session, character_id="c1", name="阿黎", kind="person")
    await merge_character(session, character_id="c2", name="空杯", kind="person")

    await record_state_change(
        session, character_id="c1", key="mood", value="平静", t_from=1, source_utterance="u1"
    )
    await record_state_change(
        session, character_id="c1", key="mood", value="激动", t_from=5, source_utterance="u2"
    )

    当前 = await current_states(session, character_id="c1")
    空角色 = await current_states(session, character_id="c2")

    assert [值 for _, 值 in 当前] == ["激动"]
    assert 空角色 == []

    await session.close()


async def test_某时刻状态可回溯(tmp_path: Path) -> None:
    """时刻 T 查到当时有效的值（设计 §5.5）。

    一条状态占 [1,5)，另一条从 5 起：取 3 命中旧值、取 5 命中新值（左闭右开）、
    取 9 命中新值；再取一个早于开端的时刻 0 则落空。
    """
    session = await _起库(tmp_path)
    await merge_character(session, character_id="c1", name="阿黎", kind="person")

    await record_state_change(
        session, character_id="c1", key="mood", value="平静", t_from=1, source_utterance="u1"
    )
    await record_state_change(
        session, character_id="c1", key="mood", value="激动", t_from=5, source_utterance="u2"
    )

    开端前 = await states_at(session, character_id="c1", moment=0)
    旧时段 = await states_at(session, character_id="c1", moment=3)
    交界点 = await states_at(session, character_id="c1", moment=5)
    新时段 = await states_at(session, character_id="c1", moment=9)

    assert 开端前 == []
    assert 旧时段 == [("mood", "平静")]
    assert 交界点 == [("mood", "激动")]
    assert 新时段 == [("mood", "激动")]

    await session.close()


async def test_同一键上一条开放态至多一条(tmp_path: Path) -> None:
    """组合写入 N 次后，t_to IS NULL 的恰好 1 条。

    `record_state_change` 把「先关旧再开新」收进数据层并在一个事务里完成，
    调用方无需记得成对调用；这条用例盯住的就是那个不变量本身。
    """
    session = await _起库(tmp_path)
    await merge_character(session, character_id="c1", name="阿黎", kind="person")

    for 序号, 值 in enumerate(["一", "二", "三", "四", "五"], start=1):
        await record_state_change(
            session,
            character_id="c1",
            key="mood",
            value=值,
            t_from=序号,
            source_utterance=f"u{序号}",
        )

    开放条数 = await session.run(
        "MATCH (:Character {id:'c1'})-[:HAS_STATE]->(s:StateChange) "
        + "WHERE s.t_to IS NULL RETURN count(s)"
    )
    总条数 = await session.run("MATCH (s:StateChange) RETURN count(s)")

    assert 开放条数 == [[1]]
    assert 总条数 == [[5]]
    assert await current_states(session, character_id="c1") == [("mood", "五")]

    await session.close()


async def test_撞主键整笔回滚且保留原故障(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """白盒探针：让插入那一步撞主键，验证关旧与插入同事务、失败则整笔回滚。

    手法是钉死 `new_id`，预先占用同一个主键——`record_state_change` 会先关旧条、
    再插新条，撞主键发生在**关旧之后**，因此只有回滚生效才能保住旧条仍然开放。

    还要断言上抛的是**最初那个故障**（主键撞车）而不是回滚自己的报错：实测约束错会让
    Kùzu 自行中止事务，事后 `ROLLBACK` 会报 "No active transaction"，若让它顶掉原异常，
    排查时会被指到完全无关的方向。
    """
    session = await _起库(tmp_path)
    await merge_character(session, character_id="c1", name="阿黎", kind="person")

    monkeypatch.setattr(writes模块, "new_id", lambda: "撞车的号")
    await record_state_change(
        session, character_id="c1", key="mood", value="平静", t_from=1, source_utterance="u1"
    )

    # 再写一次：关旧会先把 s(平静) 的 t_to 置 5，随后插入新条撞上同一主键而失败
    with pytest.raises(MemoryStoreError) as 捕获:
        await record_state_change(
            session, character_id="c1", key="mood", value="激动", t_from=5, source_utterance="u2"
        )

    旧条 = await session.run("MATCH (s:StateChange) RETURN s.value, s.t_to")
    当前 = await current_states(session, character_id="c1")

    assert "No active transaction" not in str(捕获.value)
    # 回滚生效：旧条仍开放、值未变，且没有留下第二把「半关」的痕迹
    assert 旧条 == [["平静", None]]
    assert 当前 == [("mood", "平静")]

    await session.close()


async def test_语法错整笔回滚(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """另一类失败：解析错。此时事务仍存活，走的是显式 `ROLLBACK` 那条路。

    白盒手法是让 `escape_literal` 直通（不转义），于是含单引号的值会把插入语句喂成
    语法错——这正是 `test_memory_queries.py` 里那条反面用例的手法。验证关旧那一步
    也被一并回滚。
    """
    session = await _起库(tmp_path)
    await merge_character(session, character_id="c1", name="阿黎", kind="person")
    await record_state_change(
        session, character_id="c1", key="mood", value="平静", t_from=1, source_utterance="u1"
    )

    def _不转义(原值: str) -> str:
        """故意的转义直通：含单引号的值会把语句喂成语法错，正是本用例要的失败。"""
        return 原值

    monkeypatch.setattr(writes模块, "escape_literal", _不转义)

    with pytest.raises(MemoryStoreError):
        await record_state_change(
            session,
            character_id="c1",
            key="mood",
            value="它's a lie",
            t_from=5,
            source_utterance="u2",
        )

    assert await current_states(session, character_id="c1") == [("mood", "平静")]

    await session.close()


async def test_单条写入必带来源话语(tmp_path: Path) -> None:
    """设计 §5.4 第 3 条：每次结构化写入都要带 source_utterance，否则溯源链断在此。"""
    session = await _起库(tmp_path)
    await merge_character(session, character_id="c1", name="阿黎", kind="person")

    await insert_state_change(
        session,
        change_id="s1",
        character_id="c1",
        key="mood",
        value="平静",
        t_from=1,
        source_utterance="u1",
    )

    行 = await session.run("MATCH (s:StateChange {id:'s1'}) RETURN s.source_utterance, s.t_to")

    assert 行 == [["u1", None]]

    await session.close()


async def test_收口只动指定键(tmp_path: Path) -> None:
    """close_state 只关该键的开放条；别键与已关闭的历史值不受影响。"""
    session = await _起库(tmp_path)
    await merge_character(session, character_id="c1", name="阿黎", kind="person")
    await insert_state_change(
        session,
        change_id="s1",
        character_id="c1",
        key="mood",
        value="平静",
        t_from=1,
        source_utterance="u1",
    )
    await insert_state_change(
        session,
        change_id="s2",
        character_id="c1",
        key="place",
        value="树下",
        t_from=1,
        source_utterance="u1",
    )

    await close_state(session, character_id="c1", key="mood", t_to=9)

    关掉的行 = await session.run("MATCH (s:StateChange {id:'s1'}) RETURN s.t_to")

    assert 关掉的行 == [[9]]
    assert await current_states(session, character_id="c1") == [("place", "树下")]

    await session.close()


async def test_文本取值里的引号原样读回(tmp_path: Path) -> None:
    """外部文本写进状态必须经转义：含单引号与反斜杠的值原样读回，不喂坏语句。"""
    session = await _起库(tmp_path)
    await merge_character(session, character_id="c1", name="阿黎's", kind="person")

    刁钻 = "他说\\'你好'\\"

    await record_state_change(
        session, character_id="c1", key="intro", value=刁钻, t_from=1, source_utterance="u'1"
    )

    行 = await session.run("MATCH (s:StateChange) RETURN s.value, s.source_utterance")

    assert 行 == [[刁钻, "u'1"]]
    assert await current_states(session, character_id="c1") == [("intro", 刁钻)]

    await session.close()
