"""契约模型（agent / desktop）的字段约束与 JSON round-trip。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from agent.cosmos.contracts import (
    AgentCard,
    AgentErrorInfo,
    AgentExample,
    AgentResult,
    DesktopEntry,
    DesktopGroup,
    DesktopLayoutPatch,
    DesktopMeta,
    DesktopSnapshot,
    ExecutionPolicy,
    ReadableState,
)


def make_card(**overrides: object) -> AgentCard:
    """构造一张合法卡片，用例只覆盖关心的字段。"""
    payload: dict[str, object] = {
        "id": "edit",
        "name": "编辑台",
        "icon": "✏️",
        "summary": "编辑代码与文件",
        "description": "按指令修改仓库内文件：新建、改写、局部替换、批量重命名。",
        "capabilities": ["code_edit", "file_write"],
        "params_schema": {"type": "object"},
        "desktop": DesktopMeta(group="dev", order=10),
        "execution": ExecutionPolicy(timeout_seconds=300, max_concurrent=2, queue=True),
        "examples": [AgentExample(input="把 log 改成 logger")],
    }
    payload.update(overrides)
    return AgentCard.model_validate(payload)


@pytest.mark.parametrize("agent_id", ["Edit", "1edit", "a", "with-dash"])
def test_非法_id_报错(agent_id: str) -> None:
    with pytest.raises(ValidationError):
        _ = make_card(id=agent_id)


def test_合法_id_通过() -> None:
    assert make_card(id="code_edit").id == "code_edit"


def test_能力标签非法报错() -> None:
    with pytest.raises(ValidationError):
        _ = make_card(capabilities=["Code Edit"])


def test_能力不能为空() -> None:
    with pytest.raises(ValidationError):
        _ = make_card(capabilities=[])


def test_ok_不能带_error() -> None:
    with pytest.raises(ValidationError):
        _ = AgentResult(
            status="ok",
            summary="完成",
            error=AgentErrorInfo(code="x", message="x"),
        )


@pytest.mark.parametrize("status", ["partial", "failed"])
def test_部分完成与失败必须带_error(status: str) -> None:
    with pytest.raises(ValidationError):
        _ = AgentResult(status=status, summary="没全成")  # pyright: ignore[reportArgumentType]


def test_取消必须带_cancelled_错误码() -> None:
    with pytest.raises(ValidationError):
        _ = AgentResult(
            status="cancelled",
            summary="取消",
            error=AgentErrorInfo(code="timeout", message="x"),
        )


def test_摘要不能为空() -> None:
    with pytest.raises(ValidationError):
        _ = AgentResult.ok(summary="   ")


def test_构造辅助产出合法结果() -> None:
    ok = AgentResult.ok(summary="改写了 3 个文件")
    failed = AgentResult.failed(code="not_found", message="路径不存在", retriable=False)
    cancelled = AgentResult.cancelled(summary="用户取消了任务")
    assert ok.status == "ok"
    assert ok.error is None
    assert failed.status == "failed"
    assert failed.error is not None
    assert failed.error.code == "not_found"
    assert cancelled.status == "cancelled"
    assert cancelled.error is not None
    assert cancelled.error.code == "cancelled"


def test_卡片_json_round_trip() -> None:
    card = make_card()
    restored = AgentCard.model_validate_json(card.model_dump_json())
    assert restored == card


def test_结果_json_round_trip() -> None:
    result = AgentResult.ok(summary="完成", content="详情")
    restored = AgentResult.model_validate_json(result.model_dump_json())
    assert restored == result


def test_桌面快照_json_round_trip() -> None:
    snapshot = DesktopSnapshot(
        revision=1,
        generated_at=datetime.now(UTC),
        groups=[DesktopGroup(key="general", title="通用")],
        entries=[
            DesktopEntry(
                card=make_card(),
                status="idle",
                readable=ReadableState(summary="空闲，还没用过"),
            )
        ],
        summary="1 个空闲",
    )
    restored = DesktopSnapshot.model_validate_json(snapshot.model_dump_json())
    assert restored == snapshot


def test_布局补丁只给_pin_时其余为_None() -> None:
    patch = DesktopLayoutPatch(pin={"edit": True})
    assert patch.reorder is None
    assert patch.move is None
    assert patch.collapse is None
