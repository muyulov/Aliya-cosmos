"""Aliya 侧船图适配：只暴露生活层需要的只读查询。"""

from __future__ import annotations

from agent.aliya.life.models import Move
from agent.cosmos.contracts.ship import SIGNAL_ORDER, ShipGraph, SignalLevel


class ShipAdapter:
    """把共享船图收窄成生活层会用到的几个查询。"""

    def __init__(self, graph: ShipGraph) -> None:
        self._graph = graph

    def neighbors(self, place: str) -> list[str]:
        return self._graph.neighbors(place)

    def travel_seconds(self, source: str, target: str) -> int:
        seconds = self._graph.travel_seconds(source, target)
        return seconds if seconds is not None else 0

    def signal(self, place: str) -> SignalLevel:
        return self._graph.signal(place)

    def activities_at(self, place: str) -> list[str]:
        return self._graph.activities_at(place)

    def name_of(self, place: str) -> str:
        found = self._graph.place(place)
        return found.name if found is not None else place

    def comms_at(self, place: str, *, moving: Move | None = None) -> SignalLevel:
        """移动中按两端较差的信号算，避免在路上假装有信号。"""
        if moving is not None:
            return min(
                (self.signal(moving.from_), self.signal(moving.to)),
                key=SIGNAL_ORDER.index,
            )
        return self.signal(place)
