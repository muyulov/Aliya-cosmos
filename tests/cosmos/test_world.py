"""世界推理桥：开关、端口加载、组装、节流、超时与降级。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast, override

import pytest
from loguru import logger

from agent.cosmos.contracts import (
    ActionWindow,
    AgentCard,
    AgentContext,
    AgentRequest,
    AgentResult,
    DesktopMeta,
    ExecutionPolicy,
    HistoryPattern,
    Hypothesis,
    WorldInference,
    WorldInput,
)
from agent.cosmos.services import (
    AgentRegistryService,
    AgentRuntimeService,
    PresenceService,
    WorldBridgeService,
)
from agent.cosmos.transport import BusService
from agent.cosmos.world.reasoning import (
    DraftHypothesis,
    LLMWorldReasoningPort,
    WorldInferenceDraft,
)
from core.config import Settings
from core.decision.service import DecisionService
from core.llm.service import LLMService
from core.service import ClockService


class FakePort:
    """可控的推理端口：记录收到的输入，按配置返回/超时/抛错。"""

    def __init__(
        self,
        *,
        inference: WorldInference | None = None,
        delay: float = 0.0,
        error: Exception | None = None,
        invalid: bool = False,
    ) -> None:
        self.inference: WorldInference | None = inference
        self.delay: float = delay
        self.error: Exception | None = error
        self.invalid: bool = invalid
        self.payloads: list[WorldInput] = []

    async def infer(self, payload: WorldInput) -> WorldInference:
        self.payloads.append(payload)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        if self.invalid:
            return WorldInference.model_construct(
                inference_id="inf_bad",
                generated_at=datetime.now(UTC),
                ttl_seconds=600,
                snapshot="坏结构",
                hypotheses=[],
                action_windows=[],
                signals_used=[],
                confidence=0.1,
                stale=False,
            )
        assert self.inference is not None
        return self.inference


def make_inference(identifier: str = "inf_1") -> WorldInference:
    return WorldInference(
        inference_id=identifier,
        generated_at=datetime.now(UTC),
        snapshot="你离线 2 小时了",
        hypotheses=[
            Hypothesis(statement="可能在忙", confidence=0.5, basis="没有消息"),
            Hypothesis(statement="可能休息了", confidence=0.3, basis="已经晚了"),
        ],
        signals_used=["time", "presence"],
        confidence=0.6,
    )


class _NoopLLM:
    """默认占位：不调用模型，只让桥接层通过启动期可用性检查。"""

    @property
    def chat_model(self) -> str:
        return "test-model"

    @property
    def chat_base_url(self) -> str:
        return "http://test/v1"

    def available(self, _endpoint: str | None = None) -> bool:
        return True


class _UnavailableLLM(_NoopLLM):
    """模拟未配置 api_key 的 LLM：可用性检查返回 False。"""

    @override
    def available(self, _endpoint: str | None = None) -> bool:
        return False


class _BrokenLLM:
    """模型调用永远失败：用来验证桥接层的降级路径。"""

    def available(self, _endpoint: str | None = None) -> bool:
        return True

    async def chat_structured(self, *_args: object, **_kwargs: object) -> WorldInferenceDraft:
        raise RuntimeError("模型不可用")


class FakeWorldLLM:
    """最小结构化模型替身：只回一条在线假设。"""

    @property
    def chat_model(self) -> str:
        return "fake-model"

    @property
    def chat_base_url(self) -> str:
        return "http://fake/v1"

    def available(self, _endpoint: str | None = None) -> bool:
        return True

    async def chat_structured(
        self,
        _messages: list[dict[str, object]],
        schema: type[WorldInferenceDraft],
        **_kwargs: object,
    ) -> WorldInferenceDraft:
        assert schema is WorldInferenceDraft
        return WorldInferenceDraft(
            snapshot="你在线，状态相对平稳",
            hypotheses=[
                DraftHypothesis(
                    statement="你在线",
                    confidence=0.6,
                    basis="在线状态",
                    basis_signals=["presence"],
                )
            ],
            signals_used=["presence", "time"],
            confidence=0.6,
        )


def make_service(
    settings: Settings,
    clock: ClockService,
    bus: BusService,
    llm: LLMService | None = None,
) -> WorldBridgeService:
    registry = AgentRegistryService(settings.agent)
    card = AgentCard(
        id="echo",
        name="回声台",
        summary="测试用",
        description="用于世界推理测试的最小卡片。",
        capabilities=["echo"],
        desktop=DesktopMeta(),
        execution=ExecutionPolicy(),
    )
    registry.register(card, _handler)
    runtime = AgentRuntimeService(registry, bus, clock, settings.agent)
    presence = PresenceService(
        clock,
        cast(DecisionService, cast(object, None)),
        cast(LLMService, cast(object, None)),
        bus,
        settings.agent,
    )
    return WorldBridgeService(
        clock,
        presence,
        runtime,
        registry,
        bus,
        llm if llm is not None else cast(LLMService, cast(object, _NoopLLM())),
        settings.agent,
    )


async def _handler(_request: AgentRequest, _ctx: AgentContext) -> AgentResult:
    raise NotImplementedError


async def test_关闭时恒为_None且不调端口(
    settings: Settings, clock: ClockService, bus: BusService
) -> None:
    settings.agent.cosmos.world.enabled = False
    service = make_service(settings, clock, bus)
    port = FakePort(inference=make_inference())
    service.set_port(port)
    await service.start()
    try:
        assert await service.refresh(force=True) is None
        assert service.view() is None
        assert port.payloads == []
    finally:
        await service.stop()


async def test_未注入端口时使用_LLM_实现(
    settings: Settings, clock: ClockService, bus: BusService
) -> None:
    settings.agent.cosmos.world.enabled = True
    llm = cast(LLMService, cast(object, FakeWorldLLM()))
    service = make_service(settings, clock, bus, llm=llm)

    await service.start()
    try:
        result = await service.refresh(force=True)
        assert result is not None
        assert result.hypotheses
        assert result.action_choice is not None
    finally:
        await service.stop()


async def test_未配_api_key_时启动告警(
    settings: Settings, clock: ClockService, bus: BusService
) -> None:
    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    settings.agent.cosmos.world.enabled = True
    service = make_service(
        settings,
        clock,
        bus,
        llm=cast(LLMService, cast(object, _UnavailableLLM())),
    )
    try:
        await service.start()
    finally:
        await service.stop()
        logger.remove(sink_id)

    assert any("未配置 api_key" in message for message in messages)


async def test_注入自定义端口时不告警_api_key(
    settings: Settings, clock: ClockService, bus: BusService
) -> None:
    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    settings.agent.cosmos.world.enabled = True
    service = make_service(
        settings,
        clock,
        bus,
        llm=cast(LLMService, cast(object, _UnavailableLLM())),
    )
    service.set_port(FakePort(inference=make_inference()))
    try:
        await service.start()
    finally:
        await service.stop()
        logger.remove(sink_id)

    assert not any("未配置 api_key" in message for message in messages)


async def test_注入端口后组装完整输入(
    settings: Settings, clock: ClockService, bus: BusService
) -> None:
    settings.agent.cosmos.world.enabled = True
    service = make_service(settings, clock, bus)
    port = FakePort(inference=make_inference())
    service.set_port(port)

    result = await service.refresh(force=True)
    assert result is not None
    assert port.payloads
    payload = port.payloads[0]
    assert payload.time.timezone
    assert payload.presence is not None
    assert payload.agents.total == 1

    settings.agent.cosmos.presence.enabled = False
    _ = await service.refresh(force=True)
    assert port.payloads[-1].presence is None


async def test_超时保留旧结果并标_stale(
    settings: Settings, clock: ClockService, bus: BusService
) -> None:
    settings.agent.cosmos.world.enabled = True
    settings.agent.cosmos.world.infer_timeout_seconds = 0.05
    service = make_service(settings, clock, bus)
    service.set_port(FakePort(inference=make_inference("inf_old")))
    _ = await service.refresh(force=True)

    service.set_port(FakePort(inference=make_inference("inf_new"), delay=0.2))
    result = await service.refresh(force=True)
    assert result is not None
    assert result.inference_id == "inf_old"
    view = service.view()
    assert view is not None
    assert view.stale is True


async def test_连续三次失败后暂停自动触发(
    settings: Settings, clock: ClockService, bus: BusService
) -> None:
    settings.agent.cosmos.world.enabled = True
    settings.agent.cosmos.world.triggers.low_salience_window_seconds = 0
    service = make_service(settings, clock, bus)
    port = FakePort(error=RuntimeError("炸了"))
    service.set_port(port)

    for _ in range(4):
        _ = await service.refresh(force=False)
    assert len(port.payloads) == 3


async def test_合并窗口内只跑一次(settings: Settings, clock: ClockService, bus: BusService) -> None:
    settings.agent.cosmos.world.enabled = True
    settings.agent.cosmos.world.triggers.low_salience_window_seconds = 60
    service = make_service(settings, clock, bus)
    port = FakePort(inference=make_inference())
    service.set_port(port)

    for _ in range(5):
        _ = await service.refresh(force=False)
    assert len(port.payloads) == 1


async def test_非法结构被丢弃并保留旧值(
    settings: Settings, clock: ClockService, bus: BusService
) -> None:
    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    settings.agent.cosmos.world.enabled = True
    service = make_service(settings, clock, bus)
    service.set_port(FakePort(inference=make_inference("inf_old")))
    _ = await service.refresh(force=True)

    service.set_port(FakePort(invalid=True))
    try:
        result = await service.refresh(force=True)
    finally:
        logger.remove(sink_id)

    assert result is not None
    assert result.inference_id == "inf_old"
    assert any("非法" in message for message in messages)


async def test_阿里亚上下文只存不解读(
    settings: Settings, clock: ClockService, bus: BusService
) -> None:
    from agent.cosmos.contracts import AliyaContext

    settings.agent.cosmos.world.enabled = True
    service = make_service(settings, clock, bus)
    port = FakePort(inference=make_inference())
    service.set_port(port)
    await service.set_aliya_context(AliyaContext(phase="crisis", open_topics=["排障"]))
    _ = await service.refresh(force=True)
    assert port.payloads[0].aliya_context is not None
    assert port.payloads[0].aliya_context.phase == "crisis"


async def test_假设过多时截断而不是丢弃(
    settings: Settings, clock: ClockService, bus: BusService
) -> None:
    settings.agent.cosmos.world.enabled = True
    service = make_service(settings, clock, bus)
    raw = WorldInference.model_construct(
        inference_id="inf_many",
        generated_at=datetime.now(UTC),
        ttl_seconds=600,
        snapshot="一堆假设",
        hypotheses=[
            Hypothesis(statement=f"假设{i}", confidence=0.5, basis="依据") for i in range(4)
        ],
        action_windows=[ActionWindow(window="w", suggestion="s", rationale="r") for _ in range(4)],
        signals_used=[],
        confidence=0.5,
    )
    service.set_port(FakePort(inference=raw))
    result = await service.refresh(force=True)
    assert result is not None
    assert len(result.hypotheses) == 3
    assert len(result.action_windows) == 3


async def test_冷启动直接产出结论(
    settings: Settings, clock: ClockService, bus: BusService
) -> None:
    settings.agent.cosmos.world.enabled = True
    settings.agent.cosmos.world.triggers.low_salience_window_seconds = 0
    service = make_service(settings, clock, bus)
    service.set_port(FakePort(inference=make_inference("inf_deep")))
    iterator = bus.events.subscribe(["world.inference"])
    try:
        result = await service.refresh(force=False)
        event = await asyncio.wait_for(anext(iterator), 1.0)
    finally:
        await iterator.aclose()

    assert result is not None
    assert result.revision == 1
    payload = cast(dict[str, object], event.payload["inference"])
    assert payload["revision"] == 1


async def test_相同输入指纹命中缓存(
    settings: Settings, clock: ClockService, bus: BusService
) -> None:
    settings.agent.cosmos.world.enabled = True
    settings.agent.cosmos.world.triggers.low_salience_window_seconds = 0
    service = make_service(settings, clock, bus)
    port = FakePort(inference=make_inference())
    service.set_port(port)

    _ = await service.refresh(force=False)
    _ = await service.refresh(force=False)
    assert len(port.payloads) == 1


async def test_缓存命中清除上次失败的_stale标记(
    settings: Settings, clock: ClockService, bus: BusService
) -> None:
    settings.agent.cosmos.world.enabled = True
    service = make_service(settings, clock, bus)
    service.set_port(FakePort(inference=make_inference("inf_cached")))
    _ = await service.refresh(force=True, reuse=True)  # 写入缓存

    service.set_port(FakePort(error=RuntimeError("模型挂了")))
    failed = await service.refresh(force=True)
    assert failed is not None
    assert failed.stale is True

    # 同输入命中缓存：不再继承上次失败的新鲜度
    hit = await service.refresh(force=True, reuse=True)
    assert hit is not None
    assert hit.inference_id == "inf_cached"
    assert hit.stale is False


async def test_信念随推演累积(settings: Settings, clock: ClockService, bus: BusService) -> None:
    settings.agent.cosmos.world.enabled = True
    service = make_service(settings, clock, bus)
    service.set_port(FakePort(inference=make_inference()))

    first = await service.refresh(force=True)
    assert first is not None and first.belief is not None
    assert first.belief.evidence_count > 0
    assert first.prediction_error is None

    second = await service.refresh(force=True)
    assert second is not None and second.belief is not None and first.belief is not None
    assert second.belief.evidence_count > first.belief.evidence_count
    assert second.prediction_error is not None
    assert set(second.belief_delta) == {
        "online_prob",
        "engagement",
        "readiness",
        "uncertainty",
    }


async def test_置信度被分解并校准(settings: Settings, clock: ClockService, bus: BusService) -> None:
    settings.agent.cosmos.world.enabled = True
    raw = make_inference().model_copy(update={"confidence": 1.0})
    service = make_service(settings, clock, bus)
    service.set_port(FakePort(inference=raw))

    result = await service.refresh(force=True)
    assert result is not None
    assert result.confidence < 1.0
    assert {"model", "time", "presence", "tasks", "history", "mood"} <= set(
        result.confidence_breakdown
    )
    assert result.interruptibility is not None
    assert result.expected_value is not None
    assert result.action_choice is not None
    assert result.action_choice.efe == pytest.approx(
        result.action_choice.pragmatic + result.action_choice.epistemic - result.action_choice.cost,
        abs=1e-3,
    )


async def test_证据不接地的假设被丢弃(
    settings: Settings, clock: ClockService, bus: BusService
) -> None:
    settings.agent.cosmos.world.enabled = True
    service = make_service(settings, clock, bus)
    inference = WorldInference(
        inference_id="inf_bad_evidence",
        generated_at=datetime.now(UTC),
        snapshot="瞎猜",
        hypotheses=[
            Hypothesis(
                statement="没有证据的推断",
                confidence=0.9,
                basis="拍脑袋",
                basis_signals=["presence"],
            )
        ],
        signals_used=["time"],
        confidence=0.9,
    )
    service.set_port(FakePort(inference=inference))
    assert await service.refresh(force=True) is None


async def test_低置信度假设被丢弃(settings: Settings, clock: ClockService, bus: BusService) -> None:
    settings.agent.cosmos.world.enabled = True
    service = make_service(settings, clock, bus)
    inference = WorldInference(
        inference_id="inf_low",
        generated_at=datetime.now(UTC),
        snapshot="没把握",
        hypotheses=[Hypothesis(statement="不确定", confidence=0.05, basis="证据太弱")],
        signals_used=["time"],
        confidence=0.05,
    )
    service.set_port(FakePort(inference=inference))
    assert await service.refresh(force=True) is None


async def test_推演轨迹落盘(settings: Settings, clock: ClockService, bus: BusService) -> None:
    settings.agent.cosmos.world.enabled = True
    service = make_service(settings, clock, bus)
    service.set_port(FakePort(inference=make_inference()))
    _ = await service.refresh(force=True)

    path = Path(settings.agent.cosmos.contract.state_dir) / "world_trace.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    payload = cast(dict[str, object], cast(object, json.loads(lines[0])))
    assert payload["revision"] == 1
    assert payload["fingerprint"]
    assert "trigger_features" in payload
    assert "action_choice" in payload


async def test_历史提供器与样本不足截断(
    settings: Settings, clock: ClockService, bus: BusService
) -> None:
    settings.agent.cosmos.world.enabled = True
    service = make_service(settings, clock, bus)
    port = FakePort(inference=make_inference())
    service.set_port(port)
    service.set_history_provider(lambda: HistoryPattern(sample_days=5, active_hours=[9, 10, 21]))
    _ = await service.refresh(force=True)
    assert port.payloads[-1].history is not None
    assert port.payloads[-1].history.sample_days == 5
    assert port.payloads[-1].history.active_hours == [9, 10, 21]

    service.set_history_provider(
        lambda: HistoryPattern(sample_days=1, active_hours=[9], typical_session_minutes=20)
    )
    _ = await service.refresh(force=True)
    history = port.payloads[-1].history
    assert history is not None
    assert history.active_hours == []
    assert history.typical_session_minutes is None


async def test_冷启动超时返回_None(
    settings: Settings, clock: ClockService, bus: BusService
) -> None:
    settings.agent.cosmos.world.enabled = True
    settings.agent.cosmos.world.infer_timeout_seconds = 0.05
    service = make_service(settings, clock, bus)
    service.set_port(FakePort(inference=make_inference("inf_slow"), delay=0.2))

    result = await service.refresh(force=False)
    assert result is None


async def test_低显著事件不触发推演(
    settings: Settings, clock: ClockService, bus: BusService
) -> None:
    settings.agent.cosmos.world.enabled = True
    service = make_service(settings, clock, bus)
    port = FakePort(inference=make_inference())
    service.set_port(port)
    await service.start()
    try:
        await service.on_event("tick")
        assert port.payloads == []
        await service.on_event("presence", {"state": "offline", "entity": "online"})
        assert len(port.payloads) == 1
    finally:
        await service.stop()


async def test_窗口内事件延迟补发(settings: Settings, clock: ClockService, bus: BusService) -> None:
    settings.agent.cosmos.world.enabled = True
    settings.agent.cosmos.world.triggers.high_salience_window_seconds = 0.05
    settings.agent.cosmos.world.triggers.low_salience_window_seconds = 0.05
    service = make_service(settings, clock, bus)
    port = FakePort(inference=make_inference())
    service.set_port(port)
    await service.start()
    try:
        await service.on_event("presence", {"state": "offline", "entity": "online"})
        await service.on_event("presence", {"state": "busy", "entity": "busy"})
        assert len(port.payloads) == 1
        await asyncio.sleep(0.15)
        assert len(port.payloads) == 2
    finally:
        await service.stop()


async def test_重复事件被去重(settings: Settings, clock: ClockService, bus: BusService) -> None:
    settings.agent.cosmos.world.enabled = True
    service = make_service(settings, clock, bus)
    port = FakePort(inference=make_inference())
    service.set_port(port)
    await service.start()
    try:
        await service.on_event("presence", {"state": "offline", "entity": "online"})
        await service.on_event("presence", {"state": "offline", "entity": "online"})
        assert len(port.payloads) == 1
    finally:
        await service.stop()


async def test_强制刷新也可以复用缓存(
    settings: Settings, clock: ClockService, bus: BusService
) -> None:
    settings.agent.cosmos.world.enabled = True
    service = make_service(settings, clock, bus)
    port = FakePort(inference=make_inference())
    service.set_port(port)

    _ = await service.refresh(force=True, reuse=True)
    _ = await service.refresh(force=True, reuse=True)
    assert len(port.payloads) == 1


async def test_启动时应用校准报告(
    settings: Settings,
    clock: ClockService,
    bus: BusService,
    tmp_path: Path,
) -> None:
    settings.agent.cosmos.world.enabled = True
    report = tmp_path / "world_calibration.json"
    _ = report.write_text(
        json.dumps({"temperature": 1.8, "min_salience": 0.5}),
        encoding="utf-8",
    )
    settings.agent.cosmos.world.calibration.report_path = str(report)
    service = make_service(settings, clock, bus)
    service.set_port(FakePort(inference=make_inference()))

    await service.start()
    try:
        assert settings.agent.cosmos.world.calibration.temperature == 1.8
        assert settings.agent.cosmos.world.triggers.min_salience == 0.5
    finally:
        await service.stop()


async def test_LLM_失败时保留旧结果并降级(
    settings: Settings, clock: ClockService, bus: BusService
) -> None:
    settings.agent.cosmos.world.enabled = True
    broken = cast(LLMService, cast(object, _BrokenLLM()))
    service = make_service(settings, clock, bus, llm=broken)
    service.set_port(FakePort(inference=make_inference("inf_old")))
    _ = await service.refresh(force=True)

    service.set_port(LLMWorldReasoningPort(broken, settings.agent.cosmos.world))
    result = await service.refresh(force=True)
    assert result is not None
    assert result.inference_id == "inf_old"
    view = service.view()
    assert view is not None
    assert view.stale is True


async def test_定时兜底绕过显著性(settings: Settings, clock: ClockService, bus: BusService) -> None:
    settings.agent.cosmos.world.enabled = True
    settings.agent.cosmos.world.triggers.reconcile_seconds = 1
    service = make_service(settings, clock, bus)
    port = FakePort(inference=make_inference())
    service.set_port(port)
    await service.start()
    try:
        await asyncio.sleep(1.15)
        assert port.payloads
    finally:
        await service.stop()


async def test_任务结束开关可关闭(settings: Settings, clock: ClockService, bus: BusService) -> None:
    settings.agent.cosmos.world.enabled = True
    settings.agent.cosmos.world.triggers.dispatch_finished = False
    service = make_service(settings, clock, bus)
    port = FakePort(inference=make_inference())
    service.set_port(port)
    await service.start()
    try:
        _ = await bus.events.publish("dispatch.finished", {"outcome": {"status": "ok"}})
        await asyncio.sleep(0.05)
        assert port.payloads == []
    finally:
        await service.stop()


async def test_链路与_aliya_时间进入推理输入(
    settings: Settings, clock: ClockService, bus: BusService
) -> None:
    settings.agent.cosmos.world.enabled = True
    service = make_service(settings, clock, bus)
    port = FakePort(inference=make_inference())
    service.set_port(port)
    now = clock.now()
    service.set_link_since_provider(lambda: 7)
    service.set_last_aliya_provider(lambda: now - timedelta(seconds=120))

    _ = await service.refresh(force=True)
    payload = port.payloads[-1]
    assert payload.link.since_minutes == 7
    assert payload.time.since_last_aliya_message_seconds == 120
