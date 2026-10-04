"""记忆块组装：确定性模板渲染、预算裁剪与召回依据。

期一的 `block` **一律走模板**：`recall.block_by_llm` 到期三才接 LLM，本期只读配置、
不触发调用（见 `MemoryService.recall` 的处理）。渲染与裁剪都是**纯函数**，不碰库、
不调 LLM，便于单独测试，也是「LLM 不可用时降级」的落点（设计文档 §7.2 第 3 条）。
"""

from __future__ import annotations

from collections.abc import Sequence

from core.memory.models import MemoryItem, TraceStep
from core.memory.recall.semantic import SemanticHit

__all__ = [
    "build_trace",
    "fit_budget",
    "render_block",
]

#: 记忆块的两个分层标题：先「此时的状态」，再「相关回忆」。
_状态标题 = "【此时的状态】"
_回忆标题 = "【相关回忆】"


def render_block(*, states: Sequence[tuple[str, str]], items: Sequence[MemoryItem]) -> str:
    """把当前状态与命中的回忆排成分层文本；两者都空时给空串（记忆为空是合法状态）。

    纯函数：同样的输入永远给同样的输出，不含时间、随机或外部状态。
    """
    行: list[str] = []
    if states:
        行.append(_状态标题)
        行.extend(f"- {键}：{值}" for 键, 值 in states)
    if items:
        行.append(_回忆标题)
        行.extend(f"- {条目.text}" for 条目 in items)
    return "\n".join(行)


def fit_budget(hits: Sequence[SemanticHit], *, budget: int) -> list[SemanticHit]:
    """按 `相关度 × 重要度` 降序排列，取前缀直到正文超过预算。

    **截断依据是排序而不是时间倒序**（设计文档 §7.2 第 4 条）：先把命中按
    `相关度 × 重要度` 排好，再从上往下装，装不下就停——所以「最近说过」但既不相关
    又不重要的那条会先被丢掉。`budget` 是留给这些条目正文的字符数（状态段已由调用方
    扣除），为 0 时不取任何条目。
    """
    排序 = sorted(hits, key=lambda 命: 命.item.score * 命.importance, reverse=True)
    取用: list[SemanticHit] = []
    用量 = 0
    for 命 in 排序:
        段长 = len(命.item.text)
        if 用量 + 段长 > budget:
            break
        取用.append(命)
        用量 += 段长
    return 取用


def build_trace(
    hits: Sequence[SemanticHit], states: Sequence[tuple[str, str, str]]
) -> list[TraceStep]:
    """把两路召回的依据收成 trace：向量距离 + 结构化命中。

    trace 与 `items` 一样**无条件产出**，调用方永远拿得到「为什么想起了这个」的事实底座
    （设计文档 §7.1）：命中条目记下它的向量距离，结构化命中记下是哪角色的哪个键值。
    """
    痕 = [
        TraceStep(source="vector", detail=f"{命.item.source_id} 距离 {命.distance:.6f}")
        for 命 in hits
    ]
    痕.extend(TraceStep(source="structured", detail=f"{角色} {键}={值}") for 角色, 键, 值 in states)
    return 痕
