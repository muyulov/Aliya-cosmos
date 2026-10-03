"""记忆层建表、索引与 schema 版本测试。

`ensure_schema` 在每次 `start()` 都会跑一遍，因此它**必须幂等**：
表用 `CREATE ... IF NOT EXISTS`，索引要先查后建，版本要么写入要么放行、
要么明确报错。本文件锁住这四件事，任何一处退化成"建第二次就炸"都会当场变红。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.memory import MemoryStoreError
from core.memory.store import MemorySession
from core.memory.store.schema import SCHEMA_VERSION, ensure_schema


async def test_建表幂等(tmp_path: Path) -> None:
    """连建两次不报错——初始化在每次 start() 都会跑一遍。"""
    session = MemorySession(tmp_path / "db", dimensions=4)

    await session.open()
    await ensure_schema(session)
    await ensure_schema(session)

    表名集 = {行[0] for 行 in await session.run("CALL show_tables() RETURN name")}

    # 等集断言（非子集）：期一只许建这六张表，多建即违反「不许预留」的硬规格。
    assert 表名集 == {"Character", "HAS_STATE", "MENTIONS", "Meta", "StateChange", "Utterance"}

    await session.close()


async def test_关系表也建起来(tmp_path: Path) -> None:
    """HAS_STATE 与 MENTIONS 是期一唯一用到的两张关系表，必须建出来。"""
    session = MemorySession(tmp_path / "db", dimensions=4)

    await session.open()
    await ensure_schema(session)

    表名集 = {行[0] for 行 in await session.run("CALL show_tables() RETURN name")}

    assert {"HAS_STATE", "MENTIONS"} <= 表名集

    await session.close()


async def test_向量维度写进表定义(tmp_path: Path) -> None:
    """FLOAT[d] 的维度必须在建表时确定，配置里的 dimensions 是唯一来源。"""
    session = MemorySession(tmp_path / "db", dimensions=8)

    await session.open()
    await ensure_schema(session)
    _ = await session.run(
        "CREATE (:Utterance {id:'u1', text:'甲', kind:'dialogue', importance:0.5, access_count:0})"
    )

    with pytest.raises(MemoryStoreError):
        # 6 维向量写进 FLOAT[8] 列必须失败
        _ = await session.run(
            "CREATE (:Utterance {id:'u2', text:'乙', kind:'dialogue', "
            + "embedding:[1.0,0.0,0.0,0.0,0.0,0.0], importance:0.5, access_count:0})"
        )

    await session.close()


async def test_索引建一次就够(tmp_path: Path) -> None:
    """同名索引重复创建会报错，所以 ensure_schema 必须先查再建。"""
    session = MemorySession(tmp_path / "db", dimensions=4)

    await session.open()
    await ensure_schema(session)
    await ensure_schema(session)

    await session.close()


async def test_向量与全文索引都落库(tmp_path: Path) -> None:
    """两张索引确实建在 Utterance 上，各只有一个。"""
    session = MemorySession(tmp_path / "db", dimensions=4)

    await session.open()
    await ensure_schema(session)

    索引名集 = {
        行[1]
        for 行 in await session.run("CALL show_indexes() RETURN table_name, index_name")
        if 行[0] == "Utterance"
    }

    assert 索引名集 == {"utt_vec", "utt_fts"}

    await session.close()


async def test_版本落后直接报错(tmp_path: Path) -> None:
    """本期不做自动迁移，版本对不上就明确失败，别带着半旧的表继续跑。"""
    session = MemorySession(tmp_path / "db", dimensions=4)

    await session.open()
    await ensure_schema(session)
    _ = await session.run("MATCH (m:Meta {key:'schema_version'}) SET m.value = '0'")

    with pytest.raises(MemoryStoreError) as 捕获:
        await ensure_schema(session)

    assert 捕获.value.operation == "ensure_schema"

    await session.close()


async def test_版本相符放行(tmp_path: Path) -> None:
    """版本一致时再次初始化应当无声通过。"""
    session = MemorySession(tmp_path / "db", dimensions=4)

    await session.open()
    await ensure_schema(session)

    版本行 = await session.run("MATCH (m:Meta {key:'schema_version'}) RETURN m.value")

    assert 版本行 == [[SCHEMA_VERSION]]

    await session.close()
