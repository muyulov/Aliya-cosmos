"""记忆层存储面板：唯一接触 Kùzu 的地方。

各包不直连图库，连接与事务管理收在这里，语句与建表各有其位。
"""

from core.memory.store.queries import (
    CHARACTER_COUNT,
    CLOSE_STATE_CHANGE,
    FIND_CHARACTER_BY_NAME,
    INSERT_STATE_CHANGE,
    MERGE_CHARACTER,
    MERGE_MENTIONS,
    OPEN_STATE_CHANGES,
    STATES_AT,
    UPSERT_UTTERANCE,
    UTTERANCE_COUNT,
    escape_literal,
)
from core.memory.store.schema import SCHEMA_VERSION, ensure_schema
from core.memory.store.session import MemorySession
from core.memory.store.writes import (
    close_state,
    current_states,
    insert_state_change,
    merge_character,
    record_state_change,
    states_at,
)

__all__ = [
    "CHARACTER_COUNT",
    "CLOSE_STATE_CHANGE",
    "FIND_CHARACTER_BY_NAME",
    "INSERT_STATE_CHANGE",
    "MERGE_CHARACTER",
    "MERGE_MENTIONS",
    "OPEN_STATE_CHANGES",
    "SCHEMA_VERSION",
    "STATES_AT",
    "UPSERT_UTTERANCE",
    "UTTERANCE_COUNT",
    "MemorySession",
    "close_state",
    "current_states",
    "ensure_schema",
    "escape_literal",
    "insert_state_change",
    "merge_character",
    "record_state_change",
    "states_at",
]
