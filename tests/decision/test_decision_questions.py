"""三原语构造器测试。"""

from __future__ import annotations

from core.decision import choice, noul, score


def test_choice_带选项描述() -> None:
    question = choice("归哪个部门？", {"billing": "账单", "other": None})

    assert question == {
        "type": "choice",
        "instructions": "归哪个部门？",
        "criteria": {"billing": "账单", "other": None},
    }


def test_score_保持级序() -> None:
    question = score("愤怒程度？", ["平静", "不满", "愤怒"])

    assert question == {
        "type": "score",
        "instructions": "愤怒程度？",
        "criteria": ["平静", "不满", "愤怒"],
    }


def test_noul_两个描述都给才带标准() -> None:
    question = noul("是否要求退款？", true="明确要求", false="没提")

    assert question == {
        "type": "noul",
        "instructions": "是否要求退款？",
        "criteria": {"true": "明确要求", "false": "没提"},
    }


def test_noul_只给一个描述时不带标准() -> None:
    question = noul("是否要求退款？", true="明确要求")

    assert "criteria" not in question


def test_noul_两个描述都不给时不带标准() -> None:
    question = noul("是否要求退款？")

    assert question == {"type": "noul", "instructions": "是否要求退款？"}


def test_构造器不校验上限() -> None:
    # 选项数上限是端点的事（Jev 255 / Laya 100），本地按任一家的规则拦都会误伤另一家
    question = choice("选一个", {f"opt{index}": None for index in range(300)})

    assert len(question["criteria"]) == 300
