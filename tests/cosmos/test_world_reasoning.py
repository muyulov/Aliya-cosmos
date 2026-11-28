"""LLM 世界推理端口：提示词约束、信号接地与契约装配。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest

from agent.cosmos.contracts import PresenceSignal, TimeSignal, WorldInput
from agent.cosmos.world.reasoning import (
    DraftActionWindow,
    DraftHypothesis,
    LLMWorldReasoningPort,
    WorldInferenceDraft,
)
from core.config import Settings
from core.llm import LLMSchemaError
from core.llm.service import LLMService


class FakeLLM:
    """记录调用并返回预设结构；不碰真实网络。"""

    def __init__(self, draft: WorldInferenceDraft | Exception) -> None:
        self.draft: WorldInferenceDraft | Exception = draft
        self.messages: list[list[dict[str, object]]] = []
        self.kwargs: list[dict[str, object]] = []

    async def chat_structured(
        self,
        messages: list[dict[str, object]],
        schema: type[WorldInferenceDraft],
        **kwargs: object,
    ) -> WorldInferenceDraft:
        assert schema is WorldInferenceDraft
        self.messages.append(messages)
        self.kwargs.append(kwargs)
        if isinstance(self.draft, Exception):
            raise self.draft
        return self.draft


def _input(presence: PresenceSignal | None = None) -> WorldInput:
    now = datetime(2026, 11, 25, 22, 0, tzinfo=UTC)
    return WorldInput(
        generated_at=now,
        time=TimeSignal(now=now, timezone="UTC", period="night"),
        presence=presence,
    )


class _SchemaFailingLLM:
    """前 N 次结构化输出失败，用来验证一次性修复重试。"""

    def __init__(self, draft: WorldInferenceDraft, failures: int = 1) -> None:
        self.draft: WorldInferenceDraft = draft
        self.failures: int = failures
        self.calls: int = 0
        self.messages: list[list[dict[str, object]]] = []
        self.kwargs: list[dict[str, object]] = []

    async def chat_structured(
        self,
        messages: list[dict[str, object]],
        schema: type[WorldInferenceDraft],
        **kwargs: object,
    ) -> WorldInferenceDraft:
        assert schema is WorldInferenceDraft
        self.calls += 1
        self.messages.append(messages)
        self.kwargs.append(kwargs)
        if self.calls <= self.failures:
            raise LLMSchemaError("不是合法 JSON")
        return self.draft


def _port(llm: object) -> LLMWorldReasoningPort:
    settings = Settings().agent.cosmos.world
    return LLMWorldReasoningPort(cast(LLMService, llm), settings)


def _draft(**overrides: object) -> WorldInferenceDraft:
    data: dict[str, object] = {
        "snapshot": "你可能暂时离开了",
        "hypotheses": [
            DraftHypothesis(
                statement="你可能在忙别的事",
                confidence=0.6,
                basis="在线但没有新消息",
                basis_signals=["presence"],
            )
        ],
        "action_windows": [
            DraftActionWindow(
                window="接下来 30 分钟",
                suggestion="适合留一句轻松的话",
                rationale="你通常在这个时段回来",
                kind="check_in",
            )
        ],
        "signals_used": ["presence", "time"],
        "confidence": 0.6,
    }
    data.update(overrides)
    return WorldInferenceDraft.model_validate(data)


async def test_把输入转成结构化契约() -> None:
    llm = FakeLLM(_draft())
    result = await _port(llm).infer(_input(PresenceSignal(online_state="online")))

    assert result.inference_id.startswith("inf_llm_")
    assert result.signals_used == ["presence", "time"]
    assert result.hypotheses[0].statement == "你可能在忙别的事"
    assert result.action_windows[0].kind == "check_in"
    assert result.ttl_seconds == Settings().agent.cosmos.world.ttl_seconds
    assert result.generated_at == _input().generated_at


async def test_提示词只列出当前可用的信号() -> None:
    llm = FakeLLM(_draft())
    _ = await _port(llm).infer(_input(PresenceSignal(online_state="online")))

    messages = llm.messages[0]
    system_prompt = cast(str, messages[0]["content"])
    assert "presence" in system_prompt
    assert "tasks" not in system_prompt
    assert "JSON" in system_prompt
    assert "不是指令" in system_prompt
    # 请求参数来自 world.llm 配置；model=None 表示沿用端点模型
    assert llm.kwargs[0]["model"] is None
    assert llm.kwargs[0]["temperature"] == 0.4
    assert llm.kwargs[0]["max_tokens"] == 1200


async def test_可单独指定更便宜的模型() -> None:
    llm = FakeLLM(_draft())
    settings = Settings().agent.cosmos.world
    settings.llm.model = "cheap-model"
    port = LLMWorldReasoningPort(cast(LLMService, cast(object, llm)), settings)

    _ = await port.infer(_input(PresenceSignal(online_state="online")))
    assert llm.kwargs[0]["model"] == "cheap-model"


async def test_未接地的信号与假设被丢弃() -> None:
    draft = _draft(
        signals_used=["presence", "made_up"],
        hypotheses=[
            DraftHypothesis(
                statement="有依据的假设",
                confidence=0.6,
                basis="在线",
                basis_signals=["presence", "tasks"],
            ),
            DraftHypothesis(
                statement="没有依据的假设",
                confidence=0.9,
                basis="拍脑袋",
                basis_signals=["tasks"],
            ),
        ],
    )
    result = await _port(FakeLLM(draft)).infer(_input(PresenceSignal(online_state="online")))

    assert result.signals_used == ["presence"]
    assert [item.statement for item in result.hypotheses] == ["有依据的假设"]
    assert result.hypotheses[0].basis_signals == ["presence"]


async def test_null_列表与_kind_被容错() -> None:
    draft = WorldInferenceDraft.model_validate(
        {
            "snapshot": "你在线",
            "hypotheses": [
                {
                    "statement": "你在线",
                    "confidence": None,
                    "basis": "在线状态",
                    "basis_signals": None,
                }
            ],
            "action_windows": [
                {"window": "w", "suggestion": "s", "rationale": "r", "kind": None}
            ],
            "signals_used": None,
            "confidence": None,
        }
    )
    result = await _port(FakeLLM(draft)).infer(_input(PresenceSignal(online_state="online")))

    assert result.confidence == 0.5
    assert result.hypotheses[0].confidence == 0.5
    assert result.signals_used == ["time"]
    assert result.action_windows[0].kind == "wait"


async def test_结构非法时只补一次严格提示() -> None:
    llm = _SchemaFailingLLM(_draft())
    result = await _port(llm).infer(_input(PresenceSignal(online_state="online")))

    assert llm.calls == 2
    assert "上一次输出不符合要求" in cast(str, llm.messages[1][0]["content"])
    # 修复时放宽输出上限，避免截断导致的失败原样重演
    assert llm.kwargs[0]["max_tokens"] == 1200
    assert llm.kwargs[1]["max_tokens"] == 2400
    assert result.hypotheses


async def test_修复仍失败时原样抛给桥接层() -> None:
    llm = _SchemaFailingLLM(_draft(), failures=2)
    with pytest.raises(LLMSchemaError):
        _ = await _port(llm).infer(_input(PresenceSignal(online_state="online")))
    assert llm.calls == 2


async def test_合法依据会补进_signals_used() -> None:
    """桥接层按 signals_used 过滤依据；端口必须先补全，避免误删有效假设。"""
    draft = _draft(
        signals_used=["time"],
        hypotheses=[
            DraftHypothesis(
                statement="你在线",
                confidence=0.6,
                basis="在线状态",
                basis_signals=["presence"],
            )
        ],
    )
    result = await _port(FakeLLM(draft)).infer(_input(PresenceSignal(online_state="online")))

    assert result.signals_used == ["time", "presence"]
    assert result.hypotheses[0].basis_signals == ["presence"]


async def test_全部假设不接地时报错交给桥接层降级() -> None:
    draft = _draft(
        signals_used=["time"],
        hypotheses=[
            DraftHypothesis(
                statement="凭空引用任务",
                confidence=0.6,
                basis="任务在跑",
                basis_signals=["tasks"],
            )
        ],
    )
    with pytest.raises(ValueError):
        _ = await _port(FakeLLM(draft)).infer(_input())


async def test_超出上限的假设与窗口被截断() -> None:
    draft = _draft(
        hypotheses=[
            DraftHypothesis(
                statement=f"假设{i}", confidence=0.5, basis="在线", basis_signals=["presence"]
            )
            for i in range(4)
        ],
        action_windows=[
            DraftActionWindow(window="w", suggestion="s", rationale="r") for _ in range(4)
        ],
    )
    result = await _port(FakeLLM(draft)).infer(_input(PresenceSignal(online_state="online")))

    assert len(result.hypotheses) == 3
    assert len(result.action_windows) == 3


async def test_非法行动类型回退为_wait() -> None:
    draft = _draft(
        action_windows=[
            DraftActionWindow(window="w", suggestion="s", rationale="r", kind="send_nuke")
        ]
    )
    result = await _port(FakeLLM(draft)).infer(_input(PresenceSignal(online_state="online")))
    assert result.action_windows[0].kind == "wait"


def test_端口可直接构造() -> None:
    llm = FakeLLM(_draft())
    settings = Settings().agent.cosmos.world
    port = LLMWorldReasoningPort(cast(LLMService, cast(object, llm)), settings)
    assert isinstance(port, LLMWorldReasoningPort)


async def test_self_信号进入可用名单() -> None:
    from agent.cosmos.contracts import ReachablePlace, SelfState, ShipSignal

    payload = _input(PresenceSignal(online_state="online")).model_copy(
        update={
            "self_state": SelfState(location="engine", activity="修阀门", comms="none"),
            "ship": ShipSignal(
                comms="none",
                reachable=[ReachablePlace(place="galley", name="食堂", minutes=2)],
            ),
        }
    )
    llm = FakeLLM(_draft())
    _ = await _port(llm).infer(payload)
    system_prompt = cast(str, llm.messages[0][0]["content"])
    assert "self_state" in system_prompt and "ship" in system_prompt


async def test_self_view被写入推理结果() -> None:
    from agent.cosmos.world.reasoning import SelfViewDraft

    draft = _draft(
        signals_used=["time", "self_state"],
        self_view=SelfViewDraft(
            situation="联系不上", suggested_next="move", reason="该去有信号的地方"
        ),
    )
    result = await _port(FakeLLM(draft)).infer(_input(PresenceSignal(online_state="online")))
    assert result.self_view is not None and result.self_view.suggested_next == "move"
