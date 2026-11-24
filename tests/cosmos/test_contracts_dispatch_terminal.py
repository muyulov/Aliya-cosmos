"""契约模型（dispatch / terminal / runtime / control）的约束与 round-trip。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from agent.cosmos.contracts import (
    AgentProgress,
    AgentResult,
    AgentRuntimeState,
    ConversationContext,
    CosmosEvent,
    DispatchIntent,
    DispatchOutcome,
    DispatchPlan,
    InboundEnvelope,
    MessageMeta,
    PlannedTask,
    TerminalMessage,
)


def make_plan(dependencies: list[list[int]]) -> DispatchPlan:
    return DispatchPlan(
        decision="parallel",
        tasks=[
            PlannedTask(agent_id="edit", params={}, label=f"任务{i}", depends_on=deps)
            for i, deps in enumerate(dependencies)
        ],
        confidence=0.8,
        rationale="拆分后并行",
    )


def test_依赖环报错() -> None:
    with pytest.raises(ValidationError):
        _ = make_plan([[1], [0]])


def test_依赖越界报错() -> None:
    with pytest.raises(ValidationError):
        _ = make_plan([[5]])


def test_自依赖报错() -> None:
    with pytest.raises(ValidationError):
        _ = make_plan([[0]])


def test_无环依赖通过() -> None:
    plan = make_plan([[], [0]])
    assert plan.tasks[1].depends_on == [0]


def test_终端消息缺_session_id_报错() -> None:
    with pytest.raises(ValidationError):
        _ = TerminalMessage.model_validate(
            {"direction": "user_to_aliya", "kind": "chat", "text": "在吗"}
        )


def test_消息元信息默认非主动() -> None:
    assert MessageMeta().proactive is False


def test_信封默认不带计划与直答字段() -> None:
    envelope = InboundEnvelope(
        message=TerminalMessage(
            message_id="msg_1",
            direction="user_to_aliya",
            kind="chat",
            text="在吗",
            at=datetime.now(UTC),
            session_id="ses_1",
        ),
        conversation=ConversationContext(session_id="ses_1"),
    )
    assert envelope.tasks == []


def test_信封_json_round_trip() -> None:
    envelope = InboundEnvelope(
        message=TerminalMessage(
            message_id="msg_1",
            direction="user_to_aliya",
            kind="task",
            text="改一下 x.py",
            at=datetime.now(UTC),
            session_id="ses_1",
        ),
        conversation=ConversationContext(session_id="ses_1"),
    )
    restored = InboundEnvelope.model_validate_json(envelope.model_dump_json())
    assert restored == envelope


def test_计划_json_round_trip() -> None:
    plan = make_plan([[], [0]])
    assert DispatchPlan.model_validate_json(plan.model_dump_json()) == plan


def test_事件_json_round_trip() -> None:
    event = CosmosEvent(
        event_id="evt_1",
        topic="agent.progress",
        sequence=3,
        at=datetime.now(UTC),
        payload={"progress": 50},
    )
    assert CosmosEvent.model_validate_json(event.model_dump_json()) == event


def test_运行态_json_round_trip() -> None:
    state = AgentRuntimeState(agent_id="edit", status="running", running=1)
    assert AgentRuntimeState.model_validate_json(state.model_dump_json()) == state


def test_进度_json_round_trip() -> None:
    progress = AgentProgress(
        task_id="tsk_1",
        agent_id="edit",
        label="改写 logger",
        progress=50,
        step="解析",
        at=datetime.now(UTC),
    )
    assert AgentProgress.model_validate_json(progress.model_dump_json()) == progress


def test_整批结果_json_round_trip() -> None:
    outcome = DispatchOutcome(
        dispatch_id="dsp_1",
        status="ok",
        results=[AgentResult.ok(summary="完成")],
        elapsed_seconds=1.2,
        summary="1 个任务完成",
    )
    assert DispatchOutcome.model_validate_json(outcome.model_dump_json()) == outcome


def test_意图默认值() -> None:
    intent = DispatchIntent(source="natural_language", session_id="ses_1", user_text="改 x")
    assert intent.agent_id is None
    assert intent.params is None
