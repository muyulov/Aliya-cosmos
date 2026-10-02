"""判断层对外出口。

用法：
    from core.decision import choice, noul, score

    questions = {
        "部门": choice("归哪个部门？", {"billing": "账单", "other": None}),
        "退款": noul("是否要求退款？"),
    }

三原语是 Jev 与 Laya 共用的线协议，可以混在同一趟请求里问完；
laya 与 jev 只是两个端点，等 DecisionService 落地后用 backend= 选。
"""

from core.decision.client import SYSTEMONE_PATH, build_client
from core.decision.errors import (
    DecisionConfigError,
    DecisionConnectionError,
    DecisionError,
    DecisionRequestError,
    DecisionResponseError,
    DecisionTimeoutError,
)
from core.decision.questions import (
    ChoiceQuestion,
    NoulCriteria,
    NoulQuestion,
    Question,
    ScoreQuestion,
    choice,
    noul,
    score,
)
from core.decision.service import (
    ChoiceAnswer,
    DecisionResult,
    DecisionService,
    ScoreAnswer,
    State,
)

__all__ = [
    "SYSTEMONE_PATH",
    "ChoiceAnswer",
    "ChoiceQuestion",
    "DecisionConfigError",
    "DecisionConnectionError",
    "DecisionError",
    "DecisionRequestError",
    "DecisionResponseError",
    "DecisionResult",
    "DecisionService",
    "DecisionTimeoutError",
    "NoulCriteria",
    "NoulQuestion",
    "Question",
    "ScoreAnswer",
    "ScoreQuestion",
    "State",
    "build_client",
    "choice",
    "noul",
    "score",
]
