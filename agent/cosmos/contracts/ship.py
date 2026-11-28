"""船的只读事实：舱室、相邻、步行秒数、信号等级。

cosmos 与 aliya 共用这份契约，双方都只读；模型放这里是为了守住
`agent.aliya -> agent.cosmos.contracts` 这条既有边界。
"""

from __future__ import annotations

import heapq
import json
from pathlib import Path
from typing import ClassVar, Literal

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

SignalLevel = Literal["strong", "weak", "none"]
SIGNAL_ORDER: tuple[SignalLevel, ...] = ("none", "weak", "strong")


class ShipPlace(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    id: str
    name: str
    signal: SignalLevel = "strong"
    activities: list[str] = Field(default_factory=list)


class ShipEdge(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    a: str
    b: str
    seconds: int = Field(gt=0)


class ShipGraph(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")

    version: int = 1
    ship: str = ""
    start: str = "cabin"
    places: list[ShipPlace] = Field(min_length=1)
    edges: list[ShipEdge] = Field(default_factory=list)

    @field_validator("places")
    @classmethod
    def _unique_places(cls, value: list[ShipPlace]) -> list[ShipPlace]:
        ids = [place.id for place in value]
        if len(ids) != len(set(ids)):
            raise ValueError("舱室 id 不能重复")
        return value

    def place(self, place_id: str) -> ShipPlace | None:
        return next((place for place in self.places if place.id == place_id), None)

    def neighbors(self, place_id: str) -> list[str]:
        result: list[str] = []
        for edge in self.edges:
            if edge.a == place_id:
                result.append(edge.b)
            elif edge.b == place_id:
                result.append(edge.a)
        return list(dict.fromkeys(result))

    def travel_seconds(self, source: str, target: str) -> int | None:
        if source == target:
            return 0
        if self.place(source) is None or self.place(target) is None:
            return None
        adjacency: dict[str, list[tuple[str, int]]] = {}
        for edge in self.edges:
            adjacency.setdefault(edge.a, []).append((edge.b, edge.seconds))
            adjacency.setdefault(edge.b, []).append((edge.a, edge.seconds))
        frontier: list[tuple[int, str]] = [(0, source)]
        best: dict[str, int] = {source: 0}
        while frontier:
            cost, current = heapq.heappop(frontier)
            if cost > best.get(current, cost):
                continue
            for neighbor, seconds in adjacency.get(current, []):
                candidate = cost + seconds
                if neighbor not in best or candidate < best[neighbor]:
                    best[neighbor] = candidate
                    heapq.heappush(frontier, (candidate, neighbor))
        return best.get(target)

    def signal(self, place_id: str) -> SignalLevel:
        place = self.place(place_id)
        return place.signal if place is not None else "none"

    def activities_at(self, place_id: str) -> list[str]:
        place = self.place(place_id)
        return list(place.activities) if place is not None else []


def fallback_ship() -> ShipGraph:
    """船图缺失或损坏时的最小可用图：只有一个寝室。"""
    return ShipGraph(
        ship="海獭号",
        start="cabin",
        places=[ShipPlace(id="cabin", name="寝室", signal="strong", activities=["躺着", "翻旧手册"])],
    )


def load_ship(path: str | Path = "data/world/ship.json") -> ShipGraph:
    """读取船图；缺失、损坏或起点非法时降级为单地点模式。"""
    source = Path(path)
    try:
        graph = ShipGraph.model_validate(json.loads(source.read_text(encoding="utf-8")))
    except (OSError, ValueError, ValidationError) as exc:
        logger.warning("船图缺失或损坏，降级为单地点模式", 路径=str(source), 原因=str(exc))
        return fallback_ship()
    if graph.place(graph.start) is None:
        logger.warning("船图起点不存在，降级为单地点模式", 起点=graph.start)
        return fallback_ship()
    return graph
