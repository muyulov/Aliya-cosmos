"""对话服务：规则分类、模型回退、上下文压缩、分段与脱敏。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from loguru import logger

from agent.cosmos.contracts import MessageKind, TerminalMessage
from agent.cosmos.services import ConversationService
from core.config import Settings
from core.llm.service import LLMService
from core.service import ClockService


class FakeClock:
    def now(self) -> datetime:
        return datetime(2026, 11, 7, 21, 0, tzinfo=UTC)


class FakeLLM:
    """只实现对话服务用到的摘要能力。"""

    def __init__(self, *, summary: str = "早前聊过工作", fail: bool = False) -> None:
        self.summary: str = summary
        self.fail: bool = fail
        self.calls: int = 0

    async def chat(self, _messages: object, **_kwargs: object) -> str:
        self.calls += 1
        if self.fail:
            raise TimeoutError("模型超时")
        return self.summary


def make_service(settings: Settings, llm: FakeLLM) -> ConversationService:
    return ConversationService(
        cast("LLMService", cast("object", llm)),
        cast("ClockService", cast("object", FakeClock())),
        settings.agent,
    )


def make_message(
    text: str, *, role: str = "user", index: int = 0, kind: MessageKind = "chat"
) -> TerminalMessage:
    return TerminalMessage(
        message_id=f"msg_{index}",
        direction="user_to_aliya" if role == "user" else "aliya_to_user",
        kind=kind,
        text=text,
        at=datetime(2026, 11, 7, 21, 0, tzinfo=UTC),
        session_id="ses_1",
    )


async def test_上下文压缩与轮次上限(settings: Settings) -> None:
    # 压缩窗口显式设定，避免用例随默认值变化而失效
    settings.agent.cosmos.terminal.conversation_turns = 20
    llm = FakeLLM(summary="更早的摘要")
    service = make_service(settings, llm)
    for index in range(24):
        await service.record_user(make_message(f"第 {index} 条", index=index))

    context = await service.build_context("ses_1")
    assert len(context.turns) == settings.agent.cosmos.terminal.conversation_turns
    assert context.summary == "更早的摘要"
    assert context.unanswered_count == 24


async def test_压缩失败时截断并告警(settings: Settings) -> None:
    settings.agent.cosmos.terminal.conversation_turns = 20
    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    service = make_service(settings, FakeLLM(fail=True))
    try:
        for index in range(24):
            await service.record_user(make_message(f"第 {index} 条", index=index))
        context = await service.build_context("ses_1")
    finally:
        logger.remove(sink_id)

    assert context.summary is not None
    assert len(context.summary) <= settings.agent.cosmos.terminal.summary_chars
    assert any("压缩失败" in message for message in messages)


async def test_未回条数回复后归零(settings: Settings) -> None:
    service = make_service(settings, FakeLLM())
    for index in range(3):
        await service.record_user(make_message("在吗", index=index))
    assert (await service.build_context("ses_1")).unanswered_count == 3

    await service.record_aliya(make_message("在啊", role="aliya", index=99))
    assert (await service.build_context("ses_1")).unanswered_count == 0


async def test_分段按段落且超长再按句子(settings: Settings) -> None:
    service = make_service(settings, FakeLLM())
    paragraph = "这是一句话。" * 300
    text = f"{paragraph}\n\n{paragraph}"
    parts = await service.format(text, kind="chat")
    assert len(parts) == 2
    assert all(len(part) <= settings.agent.cosmos.desktop.max_content_chars for part in parts)


async def test_短句保持原样不合并(settings: Settings) -> None:
    service = make_service(settings, FakeLLM())
    assert await service.format("嗯。", kind="chat") == ["嗯。"]
    assert await service.format("在啊。", kind="chat") == ["在啊。"]


async def test_脱敏只记规则名(settings: Settings) -> None:
    service = make_service(settings, FakeLLM())
    result = await service.redact("任务 tsk_01H 失败\nTraceback (most recent call last)")
    assert result.hit is True
    assert "task_id" in result.rules
    assert "stack" in result.rules
    assert result.rules == sorted(set(result.rules))


async def test_无命中不告警(settings: Settings) -> None:
    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    service = make_service(settings, FakeLLM())
    try:
        result = await service.redact("改好了，1 个文件都换过来了。")
    finally:
        logger.remove(sink_id)

    assert result.hit is False
    assert messages == []


async def test_脱敏开关可关闭任务标识(settings: Settings) -> None:
    settings.agent.cosmos.terminal.redaction.mask_task_ids = False
    service = make_service(settings, FakeLLM())
    result = await service.redact("任务 tsk_01H")
    assert "task_id" not in result.rules


async def test_进度消息不进对话轮次(settings: Settings) -> None:
    service = make_service(settings, FakeLLM())
    await service.record_user(make_message("在吗"))
    await service.record_aliya(make_message("还在弄", role="aliya", index=1, kind="progress"))
    context = await service.build_context("ses_1")
    assert len(context.turns) == 1
    assert context.unanswered_count == 1


async def test_固定事实进入上下文(settings: Settings) -> None:
    service = make_service(settings, FakeLLM())
    await service.record_user(make_message("我喜欢喝冰美式"))
    context = await service.build_context("ses_1")
    values = [fact.value for fact in context.pinned_facts]
    assert "喝冰美式" in values


async def test_分段保护代码块与链接(settings: Settings) -> None:
    settings.agent.cosmos.desktop.max_content_chars = 30
    service = make_service(settings, FakeLLM())
    text = "说明\n\n```python\nprint('hello')\nprint('world')\n```\n\n链接 https://example.com/a/very/long/path"
    parts = await service.format(text, kind="chat")
    code = next(part for part in parts if part.startswith("```python"))
    assert "print('hello')" in code
    assert "print('world')" in code
    assert any("https://example.com/a/very/long/path" in part for part in parts)


async def test_分段超过建议上限仍完整投递(settings: Settings) -> None:
    settings.agent.cosmos.desktop.max_content_chars = 10
    settings.agent.cosmos.terminal.max_parts = 2
    service = make_service(settings, FakeLLM())
    parts = await service.format("一二三四五六七八九十" * 6, kind="chat")
    # 分段上限只是建议值，尾部内容不能被丢掉
    assert len(parts) > 2
    assert "九十" in parts[-1]


async def test_脱敏返回遮蔽文本(settings: Settings) -> None:
    service = make_service(settings, FakeLLM())
    result = await service.redact("任务 tsk_01H 完成，邮箱 a@b.com，手机 13800138000")
    assert result.hit is True
    assert "tsk_01H" not in result.text
    assert "[任务标识]" in result.text
    assert "a@b.com" not in result.text
    assert "13800138000" not in result.text
    assert all(span.action == "mask" for span in result.spans)


async def test_密钥被遮蔽(settings: Settings) -> None:
    service = make_service(settings, FakeLLM())
    result = await service.redact("api_key=sk-abcdefghijklmnop")
    assert "sk-abcdefghijklmnop" not in result.text
    assert "[凭证已隐藏]" in result.text


async def test_工程词只标记不改写(settings: Settings) -> None:
    service = make_service(settings, FakeLLM())
    text = "模型还在调度"
    result = await service.redact(text)
    assert "engineering" in result.rules
    assert result.text == text


async def test_上下文超预算会裁剪(settings: Settings) -> None:
    settings.agent.cosmos.terminal.context_max_tokens = 256
    service = make_service(settings, FakeLLM())
    for index in range(20):
        await service.record_user(make_message("这是一段比较长的对话内容。" * 20, index=index))
    context = await service.build_context("ses_1")
    assert context.truncated is True
    assert context.token_estimate <= settings.agent.cosmos.terminal.context_max_tokens


async def test_摘要按批次触发(settings: Settings) -> None:
    settings.agent.cosmos.terminal.conversation_turns = 20
    service = make_service(settings, FakeLLM(summary="摘要"))
    for index in range(21):
        await service.record_user(make_message(f"第 {index} 条", index=index))
    assert (await service.build_context("ses_1")).summary is None

    for index in range(21, 24):
        await service.record_user(make_message(f"第 {index} 条", index=index))
    assert (await service.build_context("ses_1")).summary == "摘要"


async def test_字素完整不拆坏emoji(settings: Settings) -> None:
    settings.agent.cosmos.desktop.max_content_chars = 2
    service = make_service(settings, FakeLLM())
    family = "👨\u200d👩\u200d👧\u200d👦"
    parts = await service.format(family, kind="chat")
    assert parts == [family]


async def test_身份证校验位不通过不遮蔽(settings: Settings) -> None:
    service = make_service(settings, FakeLLM())
    valid = await service.redact("证件 11010519491231002X")
    invalid = await service.redact("证件 110105194912310021")
    assert "[证件号已隐藏]" in valid.text
    assert "[证件号已隐藏]" not in invalid.text


async def test_银行卡按Luhn校验(settings: Settings) -> None:
    service = make_service(settings, FakeLLM())
    result = await service.redact("卡号 4111111111111111")
    assert "[卡号已隐藏]" in result.text
    assert "4111111111111111" not in result.text


async def test_关闭未回提示时不暴露计数(settings: Settings) -> None:
    settings.agent.cosmos.terminal.unanswered_notice = False
    service = make_service(settings, FakeLLM())
    for index in range(3):
        await service.record_user(make_message("在吗", index=index))
    assert (await service.build_context("ses_1")).unanswered_count == 0
