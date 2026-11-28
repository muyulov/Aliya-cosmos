"""低频规划：一次结构化调用产出 1-3 个活动；失败走零 LLM 兜底池。"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, ClassVar, Literal, cast

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from agent.aliya.life.models import PendingActivity
from agent.aliya.life.ship import ShipAdapter
from core.llm import LLMService

ActivityKind = Literal["repair", "observe", "rest", "wander", "social", "organize", "read"]
Interruptibility = Literal["high", "medium", "low"]


class PlannedActivity(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    title: str
    place: str
    kind: ActivityKind
    expected_minutes: int = Field(default=30, ge=5, le=180)
    interruptibility: Interruptibility = "medium"
    output_hint: str = ""


class PlanResult(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    activities: list[PlannedActivity] = Field(default_factory=list, max_length=3)


#: 零 LLM 兜底池：既省成本，也是「她累了」的语义表达
FALLBACK_POOL: tuple[dict[str, Any], ...] = (
    {"title": "躺一会儿", "kind": "rest", "expected_minutes": 25},
    {"title": "翻翻旧手册", "kind": "read", "expected_minutes": 30},
    {"title": "在舱里慢慢走两圈", "kind": "wander", "expected_minutes": 15},
    {"title": "靠着舷窗发呆", "kind": "rest", "expected_minutes": 20},
    {"title": "把工具重新摆一遍", "kind": "organize", "expected_minutes": 30},
    {"title": "数一会儿星星", "kind": "observe", "expected_minutes": 25},
)


class LifePlanner:
    """只负责「接下来做什么」；时间推进与落盘交给 LifeLoop。"""

    def __init__(
        self,
        llm: LLMService,
        adapter: ShipAdapter,
        *,
        rest_threshold: float = 0.25,
    ) -> None:
        self._llm = llm
        self._adapter = adapter
        self._rest_threshold = rest_threshold

    async def plan(
        self,
        *,
        place: str,
        energy: float,
        now: datetime,
        recent: list[str],
    ) -> list[PendingActivity]:
        # `now` 已是服务端时区（见 transport.now），不要再转 UTC，否则夜间档会错 8 小时
        hour = now.hour
        night = hour >= 23 or hour < 5
        low_mode = energy < self._rest_threshold or night
        if low_mode:
            return self._fallback(place, rest_only=True)

        moment = now.strftime("%Y-%m-%d %H:%M")
        messages = cast(
            "Any",
            [
                {
                    "role": "system",
                    "content": (
                        "你是生活规划器。给一个叫 Aliya 的人安排 1-3 件接下来能亲手做的事。"
                        "title 必须是她能对人说出口的一句话；place 只能从给定舱室里选；"
                        "不要安排派活或调度，不要写内部编号。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"现在：{moment}\n所在：{place}（{self._adapter.name_of(place)}）\n"
                        f"能做的提示：{'、'.join(self._adapter.activities_at(place)) or '无'}\n"
                        f"精力：{energy:.2f}\n最近做过：{'；'.join(recent[-10:]) or '无'}\n"
                        "只输出 activities 数组。"
                    ),
                },
            ],
        )
        try:
            result = await asyncio.wait_for(
                self._llm.chat_structured(messages, PlanResult, max_tokens=500),
                timeout=15,
            )
        except Exception as exc:
            logger.warning("生活规划失败，落兜底池", 错误类型=type(exc).__name__, 原因=str(exc))
            return self._fallback(place, rest_only=low_mode)

        accepted: list[PendingActivity] = []
        for item in result.activities:
            # 只接受当前舱室或一步可达的舱室，避免规划出到不了的地方
            if item.place != place and item.place not in self._adapter.neighbors(place):
                continue
            if low_mode and item.kind not in {"rest", "wander"}:
                continue
            accepted.append(PendingActivity.model_validate(item.model_dump()))
        if not accepted:
            return self._fallback(place, rest_only=low_mode)
        return accepted[:3]

    def _fallback(self, place: str, *, rest_only: bool) -> list[PendingActivity]:
        pool = [
            item for item in FALLBACK_POOL if not rest_only or item["kind"] in {"rest", "wander"}
        ]
        return [PendingActivity(**{**item, "place": place}) for item in pool[:3]]
