"""任务运行时：并发、排队、幂等、取消、超时、心跳与依赖。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest

import agent.cosmos.services.runtime as runtime_module
from agent.cosmos.contracts import (
    AgentCard,
    AgentContext,
    AgentErrorInfo,
    AgentPermissions,
    AgentRequest,
    AgentResult,
    Artifact,
    CosmosEvent,
    DesktopMeta,
    ExecutionPolicy,
    PlannedTask,
    PresenceInput,
    PresenceSnapshot,
)
from agent.cosmos.errors import AgentBusyError
from agent.cosmos.services import AgentRegistryService, PresenceService
from agent.cosmos.services.runtime import AgentRuntimeService
from agent.cosmos.transport import BusService
from core.config import Settings
from core.decision.service import DecisionService
from core.llm.service import LLMService
from core.service import ClockService


@dataclass
class EventCollector:
    """后台收集事件，用例只关心「某主题出现了什么」。"""

    bus: BusService
    topics: list[str]
    events: list[CosmosEvent] = field(default_factory=list)
    _task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        async for event in self.bus.events.subscribe(self.topics):
            self.events.append(event)

    async def stop(self) -> None:
        if self._task is not None:
            _ = self._task.cancel()
            self._task = None

    async def wait_for(self, topic: str, count: int = 1, timeout: float = 2.0) -> list[CosmosEvent]:
        """等到该主题出现 count 条，返回该主题的全部事件。"""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            matched = [event for event in self.events if event.topic == topic]
            if len(matched) >= count:
                return matched
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError(f"等待 {topic} 超时")
            await asyncio.sleep(0.005)


def make_card(agent_id: str = "echo", **execution: object) -> AgentCard:
    policy = ExecutionPolicy.model_validate({"timeout_seconds": 5, **execution})
    return AgentCard(
        id=agent_id,
        name=f"工具{agent_id}",
        summary="测试用",
        description="用于运行时测试的最小卡片。",
        capabilities=["echo"],
        desktop=DesktopMeta(),
        execution=policy,
    )


@pytest.fixture
async def runtime(
    settings: Settings, clock: ClockService, bus: BusService
) -> AsyncIterator[tuple[AgentRuntimeService, AgentRegistryService]]:
    registry = AgentRegistryService(settings.agent)
    service = AgentRuntimeService(registry, bus, clock, settings.agent)
    await service.start()
    yield service, registry
    await service.stop()


async def test_正常执行(
    runtime: tuple[AgentRuntimeService, AgentRegistryService], bus: BusService
) -> None:
    service, registry = runtime
    reported: list[float | None] = []

    async def handle(request: AgentRequest, ctx: AgentContext) -> AgentResult:
        await ctx.report(progress=50, step="回显")
        reported.append(50)
        return AgentResult.ok(summary=f"收到：{request.params['text']}")

    registry.register(make_card(), handle)
    collector = EventCollector(bus, ["agent.progress", "agent.result", "agent.state.changed"])
    await collector.start()
    try:
        handle_result = await service.submit(
            "echo", {"text": "hi"}, label="回显", session_id="ses_1"
        )
        result = await service.wait(handle_result.task_id)
        assert result.status == "ok"
        assert result.summary == "收到：hi"
        assert reported == [50]
        assert service.agent_state("echo").status == "idle"
        _ = await collector.wait_for("agent.progress")
        results = await collector.wait_for("agent.result")
        payload = results[0].payload
        assert payload["task_id"] == handle_result.task_id
    finally:
        await collector.stop()


async def test_并发已满且不排队时拒绝(
    runtime: tuple[AgentRuntimeService, AgentRegistryService],
) -> None:
    service, registry = runtime
    gate = asyncio.Event()

    async def handle(_request: AgentRequest, _ctx: AgentContext) -> AgentResult:
        _ = await gate.wait()
        return AgentResult.ok(summary="完成")

    registry.register(make_card(max_concurrent=1, queue=False), handle)
    first = await service.submit("echo", {}, label="一", session_id="ses")
    with pytest.raises(AgentBusyError):
        _ = await service.submit("echo", {}, label="二", session_id="ses")

    gate.set()
    assert (await service.wait(first.task_id)).status == "ok"


async def test_排队后自动启动(runtime: tuple[AgentRuntimeService, AgentRegistryService]) -> None:
    service, registry = runtime
    gate = asyncio.Event()
    order: list[str] = []

    async def handle(request: AgentRequest, _ctx: AgentContext) -> AgentResult:
        order.append(request.label)
        if request.label == "一":
            _ = await gate.wait()
        return AgentResult.ok(summary=request.label)

    registry.register(make_card(max_concurrent=1, queue=True), handle)
    first = await service.submit("echo", {}, label="一", session_id="ses")
    second = await service.submit("echo", {}, label="二", session_id="ses")
    assert second.queued is True
    assert service.agent_state("echo").queued == 1

    gate.set()
    assert (await service.wait(first.task_id)).status == "ok"
    assert (await service.wait(second.task_id)).status == "ok"
    assert order == ["一", "二"]


async def test_同一_request_id_只执行一次(
    runtime: tuple[AgentRuntimeService, AgentRegistryService],
) -> None:
    service, registry = runtime
    calls: list[str] = []

    async def handle(request: AgentRequest, _ctx: AgentContext) -> AgentResult:
        calls.append(request.request_id)
        return AgentResult.ok(summary="完成")

    registry.register(make_card(), handle)
    first = await service.submit("echo", {}, label="一", session_id="ses", request_id="req_fixed")
    _ = await service.wait(first.task_id)
    second = await service.submit("echo", {}, label="一", session_id="ses", request_id="req_fixed")
    assert second.task_id == first.task_id
    assert calls == ["req_fixed"]


async def test_取消后结果为_cancelled(
    runtime: tuple[AgentRuntimeService, AgentRegistryService],
) -> None:
    service, registry = runtime
    started = asyncio.Event()

    async def handle(_request: AgentRequest, ctx: AgentContext) -> AgentResult:
        started.set()
        while not ctx.cancelled:
            await asyncio.sleep(0.01)
        return AgentResult.cancelled()

    registry.register(make_card(), handle)
    handle_result = await service.submit("echo", {}, label="长任务", session_id="ses")
    _ = await started.wait()
    await service.cancel(handle_result.task_id)

    result = await service.wait(handle_result.task_id)
    assert result.status == "cancelled"


async def test_超时判_failed_timeout(
    runtime: tuple[AgentRuntimeService, AgentRegistryService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, registry = runtime
    monkeypatch.setattr(runtime_module, "TIMEOUT_GRACE_SECONDS", 0.2)

    async def handle(_request: AgentRequest, _ctx: AgentContext) -> AgentResult:
        await asyncio.sleep(5)
        return AgentResult.ok(summary="不该到这里")

    registry.register(make_card(timeout_seconds=0.05), handle)
    handle_result = await service.submit("echo", {}, label="慢任务", session_id="ses")
    result = await service.wait(handle_result.task_id)

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "timeout"
    assert result.error.retriable is True


async def test_心跳缺失判_stalled(
    runtime: tuple[AgentRuntimeService, AgentRegistryService],
    bus: BusService,
) -> None:
    service, registry = runtime

    async def handle(_request: AgentRequest, _ctx: AgentContext) -> AgentResult:
        # 巡检周期是 1s：跑得比一个 tick 长，才能观测到「没有心跳」
        await asyncio.sleep(1.2)
        return AgentResult.ok(summary="完成")

    registry.register(make_card(heartbeat_seconds=0.05), handle)
    collector = EventCollector(bus, ["agent.abnormal"])
    await collector.start()
    try:
        handle_result = await service.submit("echo", {}, label="无心跳", session_id="ses")
        events = await collector.wait_for("agent.abnormal", timeout=3.0)
        assert events[0].payload["reason"] == "stalled"
        _ = await service.wait(handle_result.task_id)
    finally:
        await collector.stop()


async def test_依赖失败时下游跳过(
    runtime: tuple[AgentRuntimeService, AgentRegistryService],
) -> None:
    service, registry = runtime

    async def bad(_request: AgentRequest, _ctx: AgentContext) -> AgentResult:
        return AgentResult.failed(code="not_found", message="路径不存在")

    async def good(_request: AgentRequest, _ctx: AgentContext) -> AgentResult:
        return AgentResult.ok(summary="完成")

    registry.register(make_card(agent_id="bad"), bad)
    registry.register(make_card(agent_id="good"), good)

    tasks = [
        PlannedTask(agent_id="bad", params={}, label="先做"),
        PlannedTask(agent_id="good", params={}, label="再做", depends_on=[0]),
    ]
    handles = await service.submit_many(tasks, session_id="ses", dispatch_id="dsp_1")
    first = await service.wait(handles[0].task_id)
    second = await service.wait(handles[1].task_id)

    assert first.status == "failed"
    assert second.status == "failed"
    assert second.error is not None
    assert second.error.code == "skipped"


async def test_停止后无残留任务(
    runtime: tuple[AgentRuntimeService, AgentRegistryService],
) -> None:
    service, registry = runtime
    started = asyncio.Event()

    async def handle(_request: AgentRequest, _ctx: AgentContext) -> AgentResult:
        started.set()
        await asyncio.sleep(10)
        return AgentResult.ok(summary="完成")

    registry.register(make_card(), handle)
    handle_result = await service.submit("echo", {}, label="常驻", session_id="ses")
    _ = await started.wait()
    tasks_before = asyncio.all_tasks()

    await service.stop()
    result = await service.wait(handle_result.task_id)
    assert result.status == "cancelled"
    assert service._active is False  # pyright: ignore[reportPrivateUsage]
    assert len(asyncio.all_tasks()) < len(tasks_before)


async def test_运行态带回心跳时间(
    runtime: tuple[AgentRuntimeService, AgentRegistryService],
) -> None:
    service, registry = runtime
    gate = asyncio.Event()

    async def handle(_request: AgentRequest, _ctx: AgentContext) -> AgentResult:
        _ = await gate.wait()
        return AgentResult.ok(summary="完成")

    registry.register(make_card(), handle)
    handle_result = await service.submit("echo", {}, label="等", session_id="ses")
    await asyncio.sleep(0.01)
    state = service.agent_state("echo")
    assert state.last_heartbeat_at is not None
    gate.set()
    _ = await service.wait(handle_result.task_id)


async def test_停止会收口排队任务(
    runtime: tuple[AgentRuntimeService, AgentRegistryService],
) -> None:
    service, registry = runtime
    gate = asyncio.Event()

    async def handle(_request: AgentRequest, _ctx: AgentContext) -> AgentResult:
        _ = await gate.wait()
        return AgentResult.ok(summary="完成")

    registry.register(make_card(max_concurrent=1, queue=True), handle)
    first = await service.submit("echo", {}, label="一", session_id="ses")
    second = await service.submit("echo", {}, label="二", session_id="ses")
    assert second.queued is True

    await service.stop()
    assert (await service.wait(first.task_id)).status == "cancelled"
    assert (await service.wait(second.task_id)).status == "cancelled"


async def test_部分完成也标记为异常(
    runtime: tuple[AgentRuntimeService, AgentRegistryService],
) -> None:
    service, registry = runtime

    async def handle(_request: AgentRequest, _ctx: AgentContext) -> AgentResult:
        return AgentResult.partial(
            summary="改了一半",
            error=AgentErrorInfo(code="conflict", message="文件被占用"),
        )

    registry.register(make_card(), handle)
    handle_result = await service.submit("echo", {}, label="一半", session_id="ses")
    _ = await service.wait(handle_result.task_id)
    assert service.agent_state("echo").status == "abnormal"


async def test_emit_的产物并入结果(
    runtime: tuple[AgentRuntimeService, AgentRegistryService],
) -> None:
    service, registry = runtime
    artifact = Artifact(kind="diff", name="a.patch", uri="file:///tmp/a.patch")

    async def handle(_request: AgentRequest, ctx: AgentContext) -> AgentResult:
        await ctx.emit(artifact)
        return AgentResult.ok(summary="完成")

    registry.register(make_card(), handle)
    handle_result = await service.submit("echo", {}, label="产物", session_id="ses")
    result = await service.wait(handle_result.task_id)
    assert [item.name for item in result.artifacts] == ["a.patch"]


async def test_完成耗时写入任务摘要(
    runtime: tuple[AgentRuntimeService, AgentRegistryService],
) -> None:
    service, registry = runtime

    async def handle(_request: AgentRequest, _ctx: AgentContext) -> AgentResult:
        await asyncio.sleep(0.02)
        return AgentResult.ok(summary="完成")

    registry.register(make_card(), handle)
    task = await service.submit("echo", {}, label="计时", session_id="ses")
    _ = await service.wait(task.task_id)
    brief = service.task_brief(task.task_id)
    assert brief is not None
    assert brief.elapsed_seconds > 0


async def test_同会话新任务取消旧任务(
    runtime: tuple[AgentRuntimeService, AgentRegistryService],
) -> None:
    service, registry = runtime

    async def slow(_request: AgentRequest, _ctx: AgentContext) -> AgentResult:
        await asyncio.sleep(1.0)
        return AgentResult.ok(summary="不该跑完")

    registry.register(make_card(cancel_on_supersede=True, max_concurrent=2), slow)
    first = await service.submit("echo", {}, label="旧", session_id="ses")
    await asyncio.sleep(0.02)
    second = await service.submit("echo", {}, label="新", session_id="ses")
    assert (await service.wait(first.task_id)).status == "cancelled"
    assert (await service.wait(second.task_id)).status == "ok"


async def test_子agent按权限读取状态(
    runtime: tuple[AgentRuntimeService, AgentRegistryService],
    settings: Settings,
    clock: ClockService,
    bus: BusService,
) -> None:
    service, registry = runtime
    presence = PresenceService(
        clock, DecisionService(settings.decision), LLMService(settings.llm), bus, settings.agent
    )
    service.set_presence(presence)
    _ = await presence.apply(PresenceInput(online="online", is_typing=True))
    settings.agent.cosmos.presence.privacy.expose_to_subagents = True
    seen: list[PresenceSnapshot | None] = []

    async def handle(_request: AgentRequest, ctx: AgentContext) -> AgentResult:
        seen.append(await ctx.read_presence())
        return AgentResult.ok(summary="读到了")

    card = make_card().model_copy(
        update={
            "permissions": AgentPermissions(
                read_presence=True,
                presence_fields=["typing"],
            )
        }
    )
    registry.register(card, handle)
    task = await service.submit("echo", {}, label="读状态", session_id="ses")
    _ = await service.wait(task.task_id)
    snapshot = seen[0]
    assert snapshot is not None
    assert snapshot.fields.typing.is_typing is True
    assert snapshot.fields.online.state == "unknown"
    assert snapshot.readable.tone == "unknown"
