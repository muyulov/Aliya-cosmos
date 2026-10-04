"""记忆块组装测试：模板渲染、预算裁剪与召回依据。

这三个都是**纯函数**（`assemble.py`），不碰库、不调 LLM——所以本文件不需要真库，
也不起服务：输入什么、输出什么，逐字断言即可。
"""

from __future__ import annotations

from core.memory.models import MemoryItem
from core.memory.recall.assemble import build_trace, fit_budget, render_block
from core.memory.recall.semantic import SemanticHit


def _条目(文本: str, *, 编号: str = "u1", 分数: float = 1.0) -> MemoryItem:
    """造一条最小召回条目（期一只产出 utterance）。"""
    return MemoryItem(kind="utterance", text=文本, source_id=编号, score=分数)


def _命中(
    文本: str, *, 编号: str = "u1", 分数: float = 1.0, 重要度: float = 1.0, 距离: float = 0.0
) -> SemanticHit:
    """造一条带距离与重要度的向量命中。"""
    return SemanticHit(item=_条目(文本, 编号=编号, 分数=分数), distance=距离, importance=重要度)


# --------------------------------------------------------------------------- 渲染


def test_模板渲染分层() -> None:
    """先「此时的状态」再「相关回忆」，逐行固定，无 LLM 参与。"""
    block = render_block(states=[("心境", "平静")], items=[_条目("风起了")])

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
    高 = _命中("重要", 编号="a", 分数=0.5, 重要度=1.0)  # 0.5
    低 = _命中("很近", 编号="b", 分数=1.0, 重要度=0.0)  # 0.0
    取用 = fit_budget([低, 高], budget=100)

    assert [命.item.source_id for 命 in 取用] == ["a", "b"]


def test_预算不足时截断() -> None:
    """预算只装得下第一条（各 2 字），装不下就停——按排序截断，不是时间倒序。"""
    一 = _命中("甲乙", 编号="a", 分数=1.0)
    二 = _命中("丙丁", 编号="b", 分数=0.5)
    取用 = fit_budget([一, 二], budget=2)

    assert [命.item.source_id for 命 in 取用] == ["a"]


def test_零预算不取任何条目() -> None:
    """预算为 0（状态段已用光）时不取任何回忆，仍不报错。"""
    assert fit_budget([_命中("甲")], budget=0) == []


# --------------------------------------------------------------------------- 依据


def test_召回依据含两路() -> None:
    """trace 同时记向量距离与结构化命中，供审计「为什么想起了这个」。"""
    痕 = build_trace([_命中("甲", 编号="u1", 距离=0.25)], [("c1", "心境", "平静")])

    assert [步.source for 步 in 痕] == ["vector", "structured"]
    assert "u1" in 痕[0].detail
    assert "心境" in 痕[1].detail
