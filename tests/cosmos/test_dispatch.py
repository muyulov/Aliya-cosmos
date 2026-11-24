"""调度服务：显式入口、检索精排、拆解、幂等、并行与降级。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import cast

import pytest

from agent.cosmos.contracts import (
    AgentCard,
    AgentContext,
    AgentMatch,
    AgentRequest,
    AgentResult,
    CosmosEvent,
    DesktopMeta,
    DispatchIntent,
    DispatchPlan,
    ExecutionPolicy,
    PlannedTask,
)
from agent.cosmos.errors import AgentNotFoundError
from agent.cosmos.services import (
    AgentRegistryService,
    AgentRuntimeService,
    ConversationService,
    DispatchService,
    PresenceService,
)
from agent.cosmos.transport import BusService
from core.config import Settings
from core.decision.service import DecisionService
from core.llm.service import LLMService
from core.service import ClockService


class FakeClock:
    def now(self) -> datetime:
        return datetime(2026, 11, 7, 21, 0, tzinfo=UTC)


class FakeLLM:
    """只实现结构化调用；`plan` 为 None 时抛超时。"""

    def __init__(self, plan: DispatchPlan | None = None) -> None:
        self.plan: DispatchPlan | None = plan
        self.calls: int = 0

    async def chat_structured(
        self, _messages: object, _schema: object, **_kwargs: object
    ) -> DispatchPlan:
        self.calls += 1
        if self.plan is None:
            raise TimeoutError("模型超时")
        return self.plan

    async def chat(self, _messages: object, **_kwargs: object) -> str:
        return "摘要"


def make_card(agent_id: str, capability: str, **execution: object) -> AgentCard:
    policy = ExecutionPolicy.model_validate({"timeout_seconds": 5, **execution})
    return AgentCard(
        id=agent_id,
        name=f"工具{agent_id}",
        summary="测试用",
        description="用于调度测试的最小卡片。",
        capabilities=[capability],
        params_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}, "paths": {"type": "array"}},
            "required": ["text"],
        },
        desktop=DesktopMeta(),
        execution=policy,
    )


async def ok_handler(request: AgentRequest, _ctx: AgentContext) -> AgentResult:
    return AgentResult.ok(summary=f"完成：{request.label}")


async def fail_handler(_request: AgentRequest, _ctx: AgentContext) -> AgentResult:
    return AgentResult.failed(code="not_found", message="没找到")


class EventCollector:
    def __init__(self, bus: BusService) -> None:
        self.bus: BusService = bus
        self.events: list[CosmosEvent] = []
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.task = asyncio.create_task(self._run())
        await asyncio.sleep(0)

    async def _run(self) -> None:
        async for event in self.bus.events.subscribe(["dispatch.finished", "agent.submitted"]):
            self.events.append(event)

    async def stop(self) -> None:
        if self.task is not None:
            _ = self.task.cancel()
            _ = await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def wait(self, topic: str, timeout: float = 2.0) -> CosmosEvent:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            for event in self.events:
                if event.topic == topic:
                    return event
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError(f"等待 {topic} 超时")
            await asyncio.sleep(0.005)


@pytest.fixture
async def services(
    settings: Settings, clock: ClockService, bus: BusService
) -> AsyncIterator[tuple[DispatchService, AgentRegistryService, AgentRuntimeService, FakeLLM]]:
    llm = FakeLLM()
    registry = AgentRegistryService(settings.agent)
    runtime = AgentRuntimeService(registry, bus, clock, settings.agent)
    await runtime.start()
    conversation = ConversationService(
        cast("LLMService", cast("object", llm)), clock, settings.agent
    )
    presence = PresenceService(
        clock,
        cast("DecisionService", cast("object", None)),
        cast("LLMService", cast("object", llm)),
        bus,
        settings.agent,
    )
    dispatch = DispatchService(
        registry,
        runtime,
        conversation,
        presence,
        cast("LLMService", cast("object", llm)),
        bus,
        clock,
        settings.agent,
    )
    registry.register(make_card("echo", "echo"), ok_handler)
    yield dispatch, registry, runtime, llm
    await dispatch.stop()
    await runtime.stop()


def intent(**overrides: object) -> DispatchIntent:
    payload: dict[str, object] = {
        "source": "desktop",
        "session_id": "ses_1",
        "agent_id": "echo",
        "params": {"text": "hi"},
    }
    payload.update(overrides)
    return DispatchIntent.model_validate(payload)


async def test_桌面入口直接单任务(
    services: tuple[DispatchService, AgentRegistryService, AgentRuntimeService, FakeLLM],
) -> None:
    dispatch, _registry, _runtime, llm = services
    plan = await dispatch.plan(intent())
    assert plan.decision == "single"
    assert plan.tasks[0].agent_id == "echo"
    assert "engineer" not in plan.rationale
    assert llm.calls == 0


async def test_目标不存在报错(
    services: tuple[DispatchService, AgentRegistryService, AgentRuntimeService, FakeLLM],
) -> None:
    dispatch, *_ = services
    with pytest.raises(AgentNotFoundError):
        _ = await dispatch.plan(intent(agent_id="ghost"))


async def test_参数缺必填转_clarify(
    services: tuple[DispatchService, AgentRegistryService, AgentRuntimeService, FakeLLM],
) -> None:
    dispatch, *_ = services
    plan = await dispatch.plan(intent(params={}))
    assert plan.decision == "clarify"
    assert plan.missing == ["text"]


async def test_自然语言单候选高分不调模型(
    services: tuple[DispatchService, AgentRegistryService, AgentRuntimeService, FakeLLM],
) -> None:
    dispatch, *_ = services
    plan = await dispatch.plan(
        intent(source="natural_language", agent_id=None, params=None, user_text="帮我回显一句话")
    )
    assert plan.decision == "single"
    assert plan.tasks[0].agent_id == "echo"


async def test_多候选时模型拆解(
    services: tuple[DispatchService, AgentRegistryService, AgentRuntimeService, FakeLLM],
) -> None:
    dispatch, registry, _runtime, llm = services
    registry.register(make_card("editor", "code_edit"), ok_handler)
    llm.plan = DispatchPlan(
        decision="parallel",
        tasks=[
            PlannedTask(agent_id="echo", params={"text": "先回显"}, label="先回显"),
            PlannedTask(agent_id="editor", params={"text": "再改"}, label="再改", depends_on=[0]),
        ],
        confidence=0.8,
        rationale="拆成两步",
    )
    plan = await dispatch.plan(
        intent(
            source="natural_language",
            agent_id=None,
            params=None,
            user_text="改一下结构并回显结果",
        )
    )
    assert plan.decision == "parallel"
    assert [task.agent_id for task in plan.tasks] == ["echo", "editor"]
    assert plan.tasks[1].depends_on == [0]
    assert llm.calls == 1


async def test_模型超时回退检索(
    services: tuple[DispatchService, AgentRegistryService, AgentRuntimeService, FakeLLM],
) -> None:
    dispatch, registry, _runtime, llm = services
    registry.register(make_card("editor", "code_edit"), ok_handler)
    llm.plan = None
    plan = await dispatch.plan(
        intent(
            source="natural_language",
            agent_id=None,
            params=None,
            user_text="改一下结构并回显结果",
        )
    )
    assert "超时" in plan.rationale
    assert plan.decision in {"single", "clarify"}


async def test_低置信度转_clarify(
    services: tuple[DispatchService, AgentRegistryService, AgentRuntimeService, FakeLLM],
) -> None:
    dispatch, registry, _runtime, llm = services
    registry.register(make_card("editor", "code_edit"), ok_handler)
    llm.plan = DispatchPlan(
        decision="single",
        tasks=[PlannedTask(agent_id="echo", params={"text": "x"}, label="随便")],
        confidence=0.2,
        rationale="不确定",
    )
    plan = await dispatch.plan(
        intent(
            source="natural_language",
            agent_id=None,
            params=None,
            user_text="改一下结构并回显结果",
        )
    )
    assert plan.decision == "clarify"
    assert plan.missing


async def test_并行写同一文件自动串行(
    services: tuple[DispatchService, AgentRegistryService, AgentRuntimeService, FakeLLM],
) -> None:
    dispatch, registry, _runtime, llm = services
    registry.register(make_card("editor", "code_edit"), ok_handler)
    llm.plan = DispatchPlan(
        decision="parallel",
        tasks=[
            PlannedTask(agent_id="echo", params={"text": "一", "paths": ["core/x.py"]}, label="一"),
            PlannedTask(
                agent_id="editor", params={"text": "二", "paths": ["core/x.py"]}, label="二"
            ),
        ],
        confidence=0.9,
        rationale="并行",
    )
    plan = await dispatch.plan(
        intent(
            source="natural_language",
            agent_id=None,
            params=None,
            user_text="改一下结构并回显结果",
        )
    )
    assert 0 in plan.tasks[1].depends_on
    assert "串行" in plan.rationale


async def test_执行后聚合与幂等(
    services: tuple[DispatchService, AgentRegistryService, AgentRuntimeService, FakeLLM],
    bus: BusService,
) -> None:
    dispatch, _registry, _runtime, _llm = services
    plan = await dispatch.plan(intent())
    collector = EventCollector(bus)
    await collector.start()
    try:
        handle = await dispatch.execute(plan)
        assert len(handle.task_ids) == 1
        _ = await collector.wait("agent.submitted")
        finished = await collector.wait("dispatch.finished")
        outcome = cast("dict[str, object]", finished.payload["outcome"])
        assert outcome["status"] == "ok"

        again = await dispatch.execute(plan)
        assert again.task_ids == handle.task_ids
    finally:
        await collector.stop()


async def test_部分失败聚合为_partial(
    services: tuple[DispatchService, AgentRegistryService, AgentRuntimeService, FakeLLM],
    bus: BusService,
) -> None:
    dispatch, registry, _runtime, _llm = services
    registry.register(make_card("bad", "echo"), fail_handler)
    plan = DispatchPlan(
        decision="parallel",
        tasks=[
            PlannedTask(agent_id="echo", params={"text": "x"}, label="成"),
            PlannedTask(agent_id="bad", params={"text": "y"}, label="败"),
        ],
        confidence=1.0,
        rationale="两个都试试",
        session_id="ses_1",
    )
    collector = EventCollector(bus)
    await collector.start()
    try:
        handle = await dispatch.execute(plan)
        finished = await collector.wait("dispatch.finished")
        outcome = cast("dict[str, object]", finished.payload["outcome"])
        assert outcome["status"] == "partial"
        assert (await dispatch.outcome(handle.dispatch_id)).status == "partial"
    finally:
        await collector.stop()


async def test_auto_模式高置信度直接提交(
    services: tuple[DispatchService, AgentRegistryService, AgentRuntimeService, FakeLLM],
    settings: Settings,
) -> None:
    dispatch, _registry, _runtime, _llm = services
    settings.agent.cosmos.dispatch.mode = "auto"
    settings.agent.cosmos.dispatch.auto_threshold = 0.5
    handle = await dispatch.dispatch(
        intent(source="natural_language", agent_id=None, params=None, user_text="帮我回显一句话")
    )
    assert len(handle.task_ids) == 1


async def test_clarify_永不自动提交(
    services: tuple[DispatchService, AgentRegistryService, AgentRuntimeService, FakeLLM],
    settings: Settings,
) -> None:
    dispatch, _registry, _runtime, _llm = services
    settings.agent.cosmos.dispatch.mode = "auto"
    handle = await dispatch.dispatch(intent(params={}))
    assert handle.task_ids == []


def test_候选结构可直接构造() -> None:
    match = AgentMatch(agent_id="echo", score=0.9, reason="capabilities: echo")
    assert match.score == 0.9


async def test_去重窗口可配置为关闭(
    services: tuple[DispatchService, AgentRegistryService, AgentRuntimeService, FakeLLM],
    settings: Settings,
) -> None:
    dispatch, _registry, _runtime, _llm = services
    settings.agent.cosmos.dispatch.dedupe_window_seconds = 0
    plan = await dispatch.plan(intent())
    first = await dispatch.execute(plan)
    second = await dispatch.execute(plan)
    assert first.task_ids != second.task_ids


async def test_全局并发不超过_max_parallel(
    services: tuple[DispatchService, AgentRegistryService, AgentRuntimeService, FakeLLM],
    settings: Settings,
) -> None:
    dispatch, registry, runtime, _llm = services
    settings.agent.cosmos.dispatch.max_parallel = 1
    active = 0
    peak = 0

    async def handle(_request: AgentRequest, _ctx: AgentContext) -> AgentResult:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return AgentResult.ok(summary="完成")

    registry.register(make_card("editor", "code_edit", max_concurrent=1), handle)
    registry.register(make_card("writer", "file_write", max_concurrent=1), handle)
    plan = DispatchPlan(
        decision="parallel",
        tasks=[
            PlannedTask(agent_id="editor", params={"text": "一"}, label="一"),
            PlannedTask(agent_id="writer", params={"text": "二"}, label="二"),
        ],
        confidence=1.0,
        rationale="并行",
        session_id="ses_1",
    )
    handle_result = await dispatch.execute(plan)
    for task_id in handle_result.task_ids:
        _ = await runtime.wait(task_id)
    assert peak == 1


async def test_提交回执标出被拒任务(
    services: tuple[DispatchService, AgentRegistryService, AgentRuntimeService, FakeLLM],
) -> None:
    dispatch, _registry, _runtime, _llm = services
    plan = DispatchPlan(
        decision="parallel",
        tasks=[
            PlannedTask(agent_id="echo", params={"text": "一"}, label="一"),
            PlannedTask(agent_id="missing", params={"text": "二"}, label="二"),
        ],
        confidence=1.0,
        rationale="一好一坏",
        session_id="ses_1",
    )
    handle = await dispatch.execute(plan)
    assert handle.status == "partial"
    assert [item.agent_id for item in handle.rejected] == ["missing"]
