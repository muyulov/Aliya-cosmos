"""终端通道的打印行为：系统提示不能冒用 Aliya 的前缀。"""

import pytest

from agent.cosmos.cli import ConsoleUserChannel
from agent.cosmos.contracts import MessageKind, TerminalMessage


def _message(kind: MessageKind, text: str) -> TerminalMessage:
    return TerminalMessage(
        direction="aliya_to_user",
        kind=kind,
        text=text,
        session_id="cli",
    )


async def test_系统提示使用系统前缀(capsys: pytest.CaptureFixture[str]) -> None:
    channel = ConsoleUserChannel()
    await channel.send(_message("system", "回复失败，请稍后再试。"))

    out = capsys.readouterr().out
    assert "系统> 回复失败，请稍后再试。" in out
    assert "Aliya>" not in out


async def test_普通回复使用Aliya前缀(capsys: pytest.CaptureFixture[str]) -> None:
    channel = ConsoleUserChannel()
    await channel.send(_message("chat", "在啊，怎么了？"))

    out = capsys.readouterr().out
    assert "Aliya> 在啊，怎么了？" in out
