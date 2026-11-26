"""Aliya 的本地轻量工具：时间、读文件、计算。

用最小替身实现 RuntimePort 中真正被调用的方法，其余能力不参与这些用例。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from agent.aliya.turn.tools import RuntimePort, build_tools
from core.agent import workspace as workspace_module
from core.llm import ToolCall


class FakeRuntime:
    def __init__(self, now: datetime) -> None:
        self._now: datetime = now

    def now(self) -> datetime:
        return self._now


def registry(runtime: FakeRuntime):
    return build_tools(cast("RuntimePort", cast("object", runtime)))


async def call(runtime: FakeRuntime, name: str, arguments: str) -> str:
    return await registry(runtime).execute(ToolCall(id="1", name=name, arguments=arguments))


@pytest.fixture(autouse=True)
def _workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(workspace_module, "ALIYA_WORKSPACE", tmp_path)


async def test_current_time_uses_runtime_clock() -> None:
    runtime = FakeRuntime(datetime(2026, 11, 26, 21, 5, tzinfo=UTC))
    result = await call(runtime, "current_time", "{}")
    assert result == "2026-11-26 21:05（星期四）"


async def test_read_file_returns_numbered_slice(tmp_path: Path) -> None:
    _ = (tmp_path / "note.txt").write_text("一\n二\n三\n", encoding="utf-8")
    runtime = FakeRuntime(datetime.now(UTC))
    result = await call(runtime, "read_file", '{"path":"note.txt","start_line":2,"max_lines":1}')
    assert "第 2-2 行" in result
    assert "2: 二" in result


async def test_read_file_rejects_escape() -> None:
    runtime = FakeRuntime(datetime.now(UTC))
    result = await call(runtime, "read_file", '{"path":"../secret.txt"}')
    assert "超出工作区" in result


async def test_read_file_rejects_sensitive() -> None:
    runtime = FakeRuntime(datetime.now(UTC))
    result = await call(runtime, "read_file", '{"path":".env"}')
    assert "不允许访问" in result


async def test_calculate_basic() -> None:
    runtime = FakeRuntime(datetime.now(UTC))
    result = await call(runtime, "calculate", '{"expression":"(12+8)*3-5"}')
    assert result == "55"


async def test_calculate_division_by_zero() -> None:
    runtime = FakeRuntime(datetime.now(UTC))
    result = await call(runtime, "calculate", '{"expression":"1/0"}')
    assert "不能除以零" in result


async def test_calculate_rejects_function_call() -> None:
    runtime = FakeRuntime(datetime.now(UTC))
    result = await call(runtime, "calculate", '{"expression":"__import__(\\"os\\")"}')
    assert "算不了" in result


async def test_calculate_caps_exponent() -> None:
    runtime = FakeRuntime(datetime.now(UTC))
    result = await call(runtime, "calculate", '{"expression":"9**9**9"}')
    assert "指数太大" in result


async def test_calculate_caps_result_size() -> None:
    runtime = FakeRuntime(datetime.now(UTC))
    result = await call(runtime, "calculate", '{"expression":"999999**100"}')
    assert "结果太大" in result


async def test_read_file_caps_max_lines(tmp_path: Path) -> None:
    _ = (tmp_path / "big.txt").write_text(
        "\n".join(str(i) for i in range(300)), encoding="utf-8"
    )
    runtime = FakeRuntime(datetime.now(UTC))
    result = await call(
        runtime,
        "read_file",
        '{"path":"big.txt","start_line":1,"max_lines":99999}',
    )
    assert "第 1-200 行" in result
