"""Agent 桌面：排序、可解读状态、节流与布局持久化。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from agent.cosmos.contracts import (
    AgentCard,
    AgentContext,
    AgentErrorInfo,
    AgentRequest,
    AgentResult,
    AgentRuntimeState,
    AgentTaskView,
    DesktopLayoutPatch,
    DesktopMeta,
    ExecutionPolicy,
    RuntimeStats,
    TaskBrief,
)
from agent.cosmos.services import AgentRegistryService, DesktopService
from agent.cosmos.services.desktop import EMPTY_SUMMARY
from agent.cosmos.services.runtime import AgentRuntimeService
from agent.cosmos.transport import BusService
from core.config import Settings
from core.service import ClockService

#: 分类测试用的组名与标题
GROUP = DesktopMeta(group="dev", order=10)


class FakeRuntime:
    """只提供桌面需要的读接口。"""

    def __init__(self) -> None:
        self.states: dict[str, AgentRuntimeState] = {}
        self.tasks: dict[str, AgentTaskView] = {}
        self.stats_map: dict[str, RuntimeStats] = {}
        self.recent: dict[str, list[TaskBrief]] = {}

    def agent_state(self, agent_id: str) -> AgentRuntimeState:
        return self.states.get(agent_id, AgentRuntimeState(agent_id=agent_id))

    def task_view(self, agent_id: str) -> AgentTaskView | None:
        return self.tasks.get(agent_id)

    def stats(self, agent_id: str) -> RuntimeStats:
        return self.stats_map.get(agent_id, RuntimeStats())

    async def list_tasks(self, agent_id: str | None = None, limit: int = 20) -> list[TaskBrief]:
        if agent_id is None:
            return []
        return self.recent.get(agent_id, [])[:limit]


def make_card(
    agent_id: str = "edit", *, desktop: DesktopMeta = GROUP, **execution: object
) -> AgentCard:
    policy = ExecutionPolicy.model_validate({"timeout_seconds": 300, **execution})
    return AgentCard(
        id=agent_id,
        name=f"工具{agent_id}",
        summary="测试用",
        description="用于桌面测试的最小卡片。",
        capabilities=["code_edit"],
        desktop=desktop,
        execution=policy,
    )


def make_desktop(
    settings: Settings,
    clock: ClockService,
    bus: BusService,
    runtime: FakeRuntime,
) -> DesktopService:
    registry = AgentRegistryService(settings.agent)
    return DesktopService(
        registry,
        cast("AgentRuntimeService", cast("object", runtime)),
        bus,
        clock,
        settings.agent,
    )


async def test_空注册表摘要非空(settings: Settings, clock: ClockService, bus: BusService) -> None:
    desktop = make_desktop(settings, clock, bus, FakeRuntime())
    snapshot = await desktop.describe()
    assert snapshot.entries == []
    assert snapshot.summary == EMPTY_SUMMARY


async def test_置顶优先再按顺序与_id(
    settings: Settings, clock: ClockService, bus: BusService
) -> None:
    desktop = make_desktop(settings, clock, bus, FakeRuntime())
    registry = desktop._registry  # pyright: ignore[reportPrivateUsage]
    registry.register(
        make_card("a1", desktop=DesktopMeta(group="dev", order=100, pinned=True)), _handler
    )
    registry.register(make_card("b1", desktop=DesktopMeta(group="dev", order=10)), _handler)
    registry.register(make_card("c1", desktop=DesktopMeta(group="dev", order=5)), _handler)
    registry.register(make_card("d1", desktop=DesktopMeta(group="media", order=1)), _handler)

    snapshot = await desktop.describe()
    assert [entry.card.id for entry in snapshot.entries] == ["a1", "c1", "b1", "d1"]


async def _handler(_request: AgentRequest, _ctx: AgentContext) -> AgentResult:
    raise NotImplementedError


def _task(elapsed: float, progress: float | None, step: str | None = "改写") -> AgentTaskView:
    return AgentTaskView(
        task_id="tsk_1",
        label="改写 logger",
        progress=progress,
        step=step,
        started_at=datetime.now(UTC),
        elapsed_seconds=elapsed,
    )


def _readable_case(
    settings: Settings,
    clock: ClockService,
    bus: BusService,
    *,
    enabled: bool = True,
    state: AgentRuntimeState | None = None,
    task: AgentTaskView | None = None,
    stats: RuntimeStats | None = None,
    error: AgentErrorInfo | None = None,
) -> tuple[str, str, str | None]:
    runtime = FakeRuntime()
    agent_state = state or AgentRuntimeState(agent_id="edit")
    if error is not None:
        agent_state = agent_state.model_copy(update={"last_error": error})
    runtime.states["edit"] = agent_state
    if task is not None:
        runtime.tasks["edit"] = task
    if stats is not None:
        runtime.stats_map["edit"] = stats

    desktop = make_desktop(settings, clock, bus, runtime)
    registry = desktop._registry  # pyright: ignore[reportPrivateUsage]
    registry.register(make_card(), _handler)
    if not enabled:
        registry.disable("edit")
    readable = desktop.readable("edit")
    return readable.summary, readable.tone, readable.hint


def test_可解读状态覆盖优先级表(settings: Settings, clock: ClockService, bus: BusService) -> None:
    now = clock.now()
    assert _readable_case(settings, clock, bus, enabled=False)[:2] == ("已停用", "muted")

    summary, tone, _ = _readable_case(
        settings,
        clock,
        bus,
        state=AgentRuntimeState(agent_id="edit", status="abnormal"),
        stats=RuntimeStats(error_streak=2),
        error=AgentErrorInfo(code="internal", message="x"),
    )
    assert summary == "连续 2 次失败"
    assert tone == "alarming"

    summary, tone, _ = _readable_case(
        settings,
        clock,
        bus,
        state=AgentRuntimeState(agent_id="edit", status="abnormal"),
        error=AgentErrorInfo(code="timeout", message="x"),
    )
    assert summary == "上次任务超时（超过 300 秒）"
    assert tone == "alarming"

    summary, tone, _ = _readable_case(
        settings,
        clock,
        bus,
        state=AgentRuntimeState(agent_id="edit", status="abnormal"),
        error=AgentErrorInfo(code="not_found", message="x"),
    )
    assert summary == "上次任务失败：not_found"
    assert tone == "alarming"

    summary, tone, _ = _readable_case(
        settings,
        clock,
        bus,
        state=AgentRuntimeState(
            agent_id="edit", status="stalled", last_heartbeat_at=now - timedelta(seconds=42)
        ),
    )
    assert summary == "42 秒没有心跳，可能卡住"
    assert tone == "strained"

    summary, tone, hint = _readable_case(
        settings,
        clock,
        bus,
        state=AgentRuntimeState(agent_id="edit", status="running"),
        task=_task(elapsed=30, progress=10),
        stats=RuntimeStats(p50_seconds=10),
    )
    assert summary == "正在改写，比平时慢"
    assert tone == "strained"
    assert hint is not None

    summary, tone, _ = _readable_case(
        settings,
        clock,
        bus,
        state=AgentRuntimeState(agent_id="edit", status="running"),
        task=_task(elapsed=1, progress=60),
        stats=RuntimeStats(p50_seconds=10),
    )
    assert summary == "正在改写，进度 60%"
    assert tone == "busy"

    summary, _, _ = _readable_case(
        settings,
        clock,
        bus,
        state=AgentRuntimeState(agent_id="edit", status="running"),
        task=_task(elapsed=1, progress=None),
    )
    assert summary == "正在改写"

    summary, _, _ = _readable_case(
        settings,
        clock,
        bus,
        state=AgentRuntimeState(agent_id="edit", status="queued", queued=2),
    )
    assert summary == "有 2 个任务排队"

    summary, _, _ = _readable_case(
        settings,
        clock,
        bus,
        state=AgentRuntimeState(agent_id="edit", last_used_at=now - timedelta(seconds=60)),
        task=_task(elapsed=1, progress=100),
    )
    assert "刚完成一次" in summary

    summary, _, _ = _readable_case(
        settings,
        clock,
        bus,
        state=AgentRuntimeState(agent_id="edit", last_used_at=now - timedelta(minutes=30)),
    )
    assert "最近使用在 30 分钟前" in summary

    summary, _, _ = _readable_case(settings, clock, bus)
    assert summary == "空闲，还没用过"


async def test_无基线时不下_慢_的结论(
    settings: Settings, clock: ClockService, bus: BusService
) -> None:
    summary, _, _ = _readable_case(
        settings,
        clock,
        bus,
        state=AgentRuntimeState(agent_id="edit", status="running"),
        task=_task(elapsed=100, progress=None),
        stats=RuntimeStats(p50_seconds=None),
    )
    assert "慢" not in summary


async def _collect_events(bus: BusService, topic: str) -> tuple[list[object], asyncio.Task[None]]:
    events: list[object] = []

    async def run() -> None:
        async for event in bus.events.subscribe([topic]):
            events.append(event)

    return events, asyncio.create_task(run())


async def test_进度事件尾沿节流(settings: Settings, clock: ClockService, bus: BusService) -> None:
    settings.agent.cosmos.desktop.progress_throttle_ms = 50
    runtime = FakeRuntime()
    runtime.states["edit"] = AgentRuntimeState(agent_id="edit", status="running")
    desktop = make_desktop(settings, clock, bus, runtime)
    desktop._registry.register(make_card(), _handler)  # pyright: ignore[reportPrivateUsage]
    await desktop.start()
    events, task = await _collect_events(bus, "desktop.layout.changed")
    await asyncio.sleep(0)
    try:
        for index in range(5):
            _ = await bus.events.publish("agent.progress", {"progress": index})
        await asyncio.sleep(0.15)
        assert len(events) == 1
    finally:
        _ = task.cancel()
        _ = await asyncio.gather(task, return_exceptions=True)
        await desktop.stop()


async def test_布局补丁幂等且写盘可重读(
    settings: Settings, clock: ClockService, bus: BusService, tmp_path: Path
) -> None:
    settings.agent.cosmos.contract.state_dir = str(tmp_path)
    desktop = make_desktop(settings, clock, bus, FakeRuntime())
    desktop._registry.register(make_card(), _handler)  # pyright: ignore[reportPrivateUsage]
    await desktop.start()
    try:
        first = await desktop.patch_layout(DesktopLayoutPatch(pin={"edit": True}))
        assert first.revision == 1
        second = await desktop.patch_layout(DesktopLayoutPatch(pin={"edit": True}))
        assert second.revision == 1
    finally:
        await desktop.stop()

    reopened = make_desktop(settings, clock, bus, FakeRuntime())
    await reopened.start()
    try:
        assert (await reopened.describe()).revision == 1
    finally:
        await reopened.stop()


async def test_损坏布局文件不阻断启动(
    settings: Settings, clock: ClockService, bus: BusService, tmp_path: Path
) -> None:
    settings.agent.cosmos.contract.state_dir = str(tmp_path)
    _ = (tmp_path / "desktop.json").write_text("{not json", encoding="utf-8")

    desktop = make_desktop(settings, clock, bus, FakeRuntime())
    await desktop.start()
    try:
        assert (await desktop.describe()).revision == 0
    finally:
        await desktop.stop()


async def test_单卡详情带最近任务(settings: Settings, clock: ClockService, bus: BusService) -> None:
    runtime = FakeRuntime()
    runtime.recent["edit"] = [
        TaskBrief(task_id="tsk_1", agent_name="编辑台", label="改写", status="idle")
    ]
    desktop = make_desktop(settings, clock, bus, runtime)
    desktop._registry.register(make_card(), _handler)  # pyright: ignore[reportPrivateUsage]

    detail = await desktop.get("edit")
    assert detail.card.id == "edit"
    assert [item.task_id for item in detail.recent_tasks] == ["tsk_1"]
