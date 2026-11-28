"""日终反思：把当天做过的事沉淀成能进记忆的短句。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field


class ReflectionResult(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    facts: list[str] = Field(default_factory=list, max_length=2)


async def maybe_reflect(
    llm: Any,
    store: Any,
    *,
    now: datetime,
    last_reflection_date: str | None,
) -> tuple[list[str], bool]:
    """跨日首次、或当天 22 点后至少有一条记录时，做一次日终反思。

    返回 `(facts, attempted)`：`attempted=True` 表示今天已经试过（无论成败），
    调用方应把日期落盘，避免失败后每个 tick 都重试、或重启后重复反思。
    """
    today = now.date().isoformat()
    # 每天只反思一次：已经反思过就直接跳过，避免 22 点后每个 tick 都调模型
    if last_reflection_date == today:
        return [], False
    entries = store.recent(50)
    today_entries = [item for item in entries if str(item.get("at", ""))[:10] == today]
    if not today_entries and now.hour < 22:
        return [], False
    lines = "\n".join(
        f"- {item.get('title', '')}：{item.get('notes', '')}" for item in today_entries
    )
    messages = [
        {
            "role": "system",
            "content": "把下面的活动记录写成 1-2 句能记住的短句；用第一人称口吻，不写内部编号。",
        },
        {
            "role": "user",
            "content": lines or "今天没有具体活动，只写一句今天大致怎么过的。",
        },
    ]
    try:
        result = await llm.chat_structured(messages, ReflectionResult, max_tokens=200)
    except Exception as exc:
        logger.warning("日终反思失败", 原因=str(exc))
        return [], True
    return [fact.strip() for fact in result.facts if fact.strip()][:2], True
