"""结构化召回：按 `focus` 角色取「当前状态」（设计文档 §7 第 ③ 路）。

「当前状态」与「某时刻状态」是同一个查询，只差一个区间谓词（设计 §5.5）；期一
只取当前，历史回溯留给需要时再调 `writes.states_at`。
"""

from __future__ import annotations

from collections.abc import Sequence

from core.memory.store.session import MemorySession
from core.memory.store.writes import current_states

__all__ = ["recall_states"]


async def recall_states(
    session: MemorySession, *, character_ids: Sequence[str]
) -> list[tuple[str, str, str]]:
    """逐角色取当前状态，收成 `(角色 id, 键, 值)`。

    没有任何状态的角色不出现在结果里（空状态是合法状态，不是错误）；
    角色不存在同样只是查不到状态、静默落空，与 `current_states` 的语义一致。
    """
    命中: list[tuple[str, str, str]] = []
    for 角色 in character_ids:
        for 键, 值 in await current_states(session, character_id=角色):
            命中.append((角色, 键, 值))
    return 命中
