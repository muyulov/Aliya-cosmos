"""记忆块组装测试：模板渲染、预算裁剪与召回依据。

这三个都是**纯函数**（`assemble.py`），不碰库、不调 LLM——所以本文件不需要真库，
也不起服务：输入什么、输出什么，逐字断言即可。
"""

from __future__ import annotations

from core.memory.models import MemoryItem
from core.memory.recall.assemble import build_trace, fit_budget, render_block
from core.memory.recall.semantic import SemanticHit


def _item(text: str, *, uid: str = "u1", score: float = 1.0) -> MemoryItem:
    """造一条最小召回条目（期一只产出 utterance）。"""
    return MemoryItem(kind="utterance", text=text, source_id=uid, score=score)


def _hit(
    text: str,
    *,
    uid: str = "u1",
    score: float = 1.0,
    importance: float = 1.0,
    distance: float = 0.0,
) -> SemanticHit:
    """造一条带距离与重要度的向量命中。"""
    return SemanticHit(
        item=_item(text, uid=uid, score=score), distance=distance, importance=importance
    )


# --------------------------------------------------------------------------- 渲染


def test_模板渲染分层() -> None:
    """先「此时的状态」再「相关回忆」，逐行固定，无 LLM 参与。"""
    block = render_block(states=[("心境", "平静")], items=[_item("风起了")])

    assert block.splitlines() == ["【此时的状态】", "- 心境：平静", "【相关回忆】", "- 风起了"]


def test_空输入渲染成空串() -> None:
    """状态与回忆都空时给空串：记忆为空是合法状态，不是错误。"""
    assert render_block(states=[], items=[]) == ""


def test_无回忆只渲染状态() -> None:
    """只有结构性命中时，不出现空的「相关回忆」标题。"""
    assert render_block(states=[("心境", "平静")], items=[]) == "【此时的状态】\n- 心境：平静"


# --------------------------------------------------------------------------- 预算


def test_预算充足按相关度乘重要度排序() -> None:
    """排序键是 `相关度 × 重要度`，不是单独的相关度。"""
    high = _hit("重要", uid="a", score=0.5, importance=1.0)  # 0.5
    low = _hit("很近", uid="b", score=1.0, importance=0.0)  # 0.0
    kept = fit_budget([low, high], budget=100)

    assert [hit.item.source_id for hit in kept] == ["a", "b"]


def test_预算不足时截断() -> None:
    """预算只装得下第一条（各 2 字），装不下就停——按排序截断，不是时间倒序。"""
    one = _hit("甲乙", uid="a", score=1.0)
    two = _hit("丙丁", uid="b", score=0.5)
    kept = fit_budget([one, two], budget=2)

    assert [hit.item.source_id for hit in kept] == ["a"]


def test_零预算不取任何条目() -> None:
    """预算为 0（状态段已用光）时不取任何回忆，仍不报错。"""
    assert fit_budget([_hit("甲")], budget=0) == []


# --------------------------------------------------------------------------- 依据


def test_召回依据含两路() -> None:
    """trace 同时记向量距离与结构化命中，供审计「为什么想起了这个」。"""
    trace = build_trace([_hit("甲", uid="u1", distance=0.25)], [("c1", "心境", "平静")])

    assert [step.source for step in trace] == ["vector", "structured"]
    assert "u1" in trace[0].detail
    assert "心境" in trace[1].detail
