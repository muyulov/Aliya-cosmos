"""向量召回：查询向量 → Kùzu 的 HNSW → 最近的若干条原文。

期一只给 `Utterance` 建了向量索引（`Event` / `Reflection` 是期二、期三的表），
故这里只召回对话原文；三张表各 Top-k 是期二、期三扩表后的事。

**距离与分数**：HNSW 给的是 L2 距离（越小越近），而公开的 `MemoryItem.score`
约定「越大越相关」。这里用 `1 / (1 + 距离)` 做单调折算，落在 (0, 1]：距离为 0
得 1，距离越大越趋近 0，既不改变排序也能直接与重要度相乘。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from core.memory.models import MemoryItem
from core.memory.store.queries import SEMANTIC_UTTERANCES
from core.memory.store.session import MemorySession

__all__ = [
    "SemanticHit",
    "recall_utterances",
]


def _向量字面量(vector: Sequence[float]) -> str:
    """把查询向量拼成 Kùzu 的浮点列表字面量（向量是数值，无需 `escape_literal`）。"""
    return "[" + ", ".join(repr(float(分量)) for 分量 in vector) + "]"


def _相关度(distance: float) -> float:
    """把 L2 距离折成「越大越相关」的分数：`1 / (1 + 距离)`，落在 (0, 1]。"""
    return 1.0 / (1.0 + max(distance, 0.0))


def _可选整数(value: object) -> int | None:
    """库里的可空整数列：`None` 原样保留（世界时间未知就是不伪造），否则收成 `int`。"""
    return None if value is None else int(cast("int", value))


@dataclass(frozen=True, slots=True)
class SemanticHit:
    """一条向量召回命中：公开条目 + 原始距离 + 重要度。

    `importance` 不进 `MemoryItem`（那是公开模型，字段按设计文档 §7.1 定死），
    但预算裁剪要按 `相关度 × 重要度` 排序，故在内部随命中一起带出。
    """

    item: MemoryItem
    distance: float
    importance: float


async def recall_utterances(
    session: MemorySession, *, query_vector: Sequence[float], top_k: int
) -> list[SemanticHit]:
    """按查询向量取最近的 `top_k` 条原文。

    库为空或索引里没有任何命中时返回空表（**不报错**）——记忆为空是合法状态。
    """
    语句 = SEMANTIC_UTTERANCES.format(embedding=_向量字面量(query_vector), k=top_k)
    命中: list[SemanticHit] = []
    for 行 in await session.run(语句):
        距离 = float(cast("float", 行[4]))
        命中.append(
            SemanticHit(
                item=MemoryItem(
                    kind="utterance",
                    text=str(行[1]),
                    source_id=str(行[0]),
                    score=_相关度(距离),
                    world_time=_可选整数(行[2]),
                ),
                distance=距离,
                importance=float(cast("float", 行[3])),
            )
        )
    return 命中
