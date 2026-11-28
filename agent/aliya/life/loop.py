"""零 LLM 状态机：到点结束、移动到达、取下一个活动；只负责时间感。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol

from loguru import logger

from agent.aliya.life.models import Activity, Move, PendingActivity
from agent.aliya.life.ship import ShipAdapter
from agent.aliya.life.store import ActivityStore


class PlannerPort(Protocol):
    """生活规划器只需要这一个方法；测试替身与真实实现都按结构匹配。"""

    async def plan(
        self,
        *,
        place: str,
        energy: float,
        now: datetime,
        recent: list[str],
    ) -> list[PendingActivity]: ...

#: 每分钟精力消耗；repair 最费神，read 最省
_ENERGY_PER_MINUTE: dict[str, float] = {
    "repair": 0.003,
    "observe": 0.0015,
    "wander": 0.002,
    "social": 0.0015,
    "organize": 0.0015,
    "read": 0.001,
}
#: 休息每分钟恢复的精力
_REST_GAIN_PER_MINUTE = 0.003


@dataclass(slots=True)
class LifeDecision:
    kind: Literal["start", "move", "finish", "wait"]
    activity: PendingActivity | None = None
    destination: str | None = None
    reason: str = ""


class LifeLoop:
    """每次 tick 只推进一步：到点收尾、移动到站、或开始下一件事。"""

    def __init__(
        self,
        store: ActivityStore,
        planner: PlannerPort,
        adapter: ShipAdapter,
        *,
        plan_minutes: int = 40,
    ) -> None:
        self._store: ActivityStore = store
        self._planner: PlannerPort = planner
        self._adapter: ShipAdapter = adapter
        self._plan_minutes: int = plan_minutes
        self._last_plan_at: datetime | None = None

    async def advance(
        self,
        now: datetime,
        *,
        suggested_next: str | None = None,
        preferred_target: str | None = None,
    ) -> LifeDecision:
        state = await self._store.load()

        if state.moving is not None:
            if now < state.moving.arrive_at:
                return LifeDecision("wait", reason="在路上")
            place = state.moving.to
            state.moving = None
            state.current = None
            state.place = place
            await self._store.save(state)
            logger.info("移动已到达", 地点=place)
            return LifeDecision("move", destination=place, reason="到达")

        if state.current is not None:
            elapsed = (now - state.current.started_at).total_seconds() / 60
            if elapsed < state.current.expected_minutes:
                return LifeDecision("wait", reason="活动未到期")
            finished = state.current
            finished_title = finished.title
            await self._store.append(
                state,
                ended_at=now,
                notes="",
                turns=1 if finished.turn_done else 0,
            )
            state.current = None
            state.energy = _energy_after(finished, state.energy)
            await self._store.save(state)
            logger.info("活动结束", 活动=finished_title)
            # 收尾本身不触发新回合：本 tick 只负责把旧活动送进流水
            return LifeDecision("wait", reason="活动到期")

        # 世界推理给出方向时，先按它插一条就近的活动；只认一步可达的地点。
        # `suggested_next=stay` 时忽略目标，避免一边让她留下一边安排移动。
        if (
            not state.pending
            and preferred_target is not None
            and suggested_next != "stay"
            and preferred_target in self._adapter.neighbors(state.place)
        ):
            state.pending = [
                PendingActivity(
                    title=f"去{self._adapter.name_of(preferred_target)}看看",
                    place=preferred_target,
                    kind="wander",
                    expected_minutes=20,
                )
            ]

        if not state.pending and self._plan_due(now):
            state.pending = await self._planner.plan(
                place=state.place,
                energy=state.energy,
                now=now,
                recent=[str(item.get("notes", "")) for item in self._store.recent(10)],
            )
            self._last_plan_at = now
            await self._store.save(state)

        if not state.pending:
            return LifeDecision("wait", reason="没有可做的事")

        item = state.pending[0]
        if item.place != state.place:
            seconds = self._adapter.travel_seconds(state.place, item.place)
            if seconds is None:
                logger.warning("活动地点不可达，丢弃", 地点=item.place)
                state.pending = state.pending[1:]
                await self._store.save(state)
                return LifeDecision("wait", reason="活动地点不可达")
            state.moving = Move.model_validate(
                {
                    "from": state.place,
                    "to": item.place,
                    "departed_at": now,
                    "arrive_at": now + timedelta(seconds=seconds),
                }
            )
            await self._store.save(state)
            logger.info("开始移动", 起点=state.place, 终点=item.place, 秒=seconds)
            return LifeDecision("move", destination=item.place, reason="去活动地点")

        state.pending = state.pending[1:]
        state.current = Activity(
            id=f"act_{uuid.uuid4().hex[:8]}",
            title=item.title,
            place=item.place,
            kind=item.kind,
            started_at=now,
            expected_minutes=item.expected_minutes,
            interruptibility=item.interruptibility,
            output_hint=item.output_hint,
        )
        await self._store.save(state)
        logger.info("活动开始", 活动=item.title, 地点=item.place)
        return LifeDecision("start", activity=item, reason="开始下一个活动")

    def _plan_due(self, now: datetime) -> bool:
        """规划节流：没有活动时不必每个 tick 都问一次模型。"""
        if self._last_plan_at is None:
            return True
        return now - self._last_plan_at >= timedelta(minutes=self._plan_minutes)


def _energy_after(activity: Activity, energy: float) -> float:
    """按活动类型与叙事时长结算精力；休息回升，其余消耗。"""
    minutes = max(1, activity.expected_minutes)
    if activity.kind == "rest":
        return min(1.0, energy + _REST_GAIN_PER_MINUTE * minutes)
    cost = _ENERGY_PER_MINUTE.get(activity.kind, 0.002) * minutes
    return max(0.0, energy - cost)
