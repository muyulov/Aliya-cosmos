"""活动回合工具面测试：文件工具存在、写入限制在工作区内。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from agent.aliya.turn.tools import build_tools


def _runtime() -> Any:
    class FakeRuntime:
        def now(self) -> datetime:
            return datetime(2026, 11, 28, 14, 0, tzinfo=UTC)

        async def send(self, text: str) -> None:
            return None

    return FakeRuntime()


async def test_活动工具面包含文件工具但不派活(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.chdir(tmp_path)
    registry = build_tools(_runtime(), mode="activity")
    names = set(registry.names)
    assert {"read_file", "write_file", "list_files", "remember", "send_message"} <= names
    assert not ({"dispatch_task", "cancel_task", "retry_task"} & names)


async def test_写入与列举都在工作区内(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.chdir(tmp_path)
    tools = {item.name: item for item in build_tools(_runtime(), mode="activity").tools}
    write_func = tools["write_file"].func
    listing_func = tools["list_files"].func
    assert write_func is not None and listing_func is not None
    write = cast("Callable[[str, str], Awaitable[str]]", write_func)
    listing = cast("Callable[[str], Awaitable[str]]", listing_func)
    assert "写入成功" in await write("notes/today.md", "# 观测\n今天风很大")
    assert "today.md" in await listing("notes")
    assert "超出工作区" in await write("../escape.md", "不行")
    assert (tmp_path / "data/Aliya/workspace/notes/today.md").exists()


async def test_关闭工作区写入时不注册写文件工具(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.chdir(tmp_path)
    registry = build_tools(_runtime(), mode="activity", workspace_writes=False)
    assert "write_file" not in registry.names
    assert "list_files" not in registry.names
    assert "read_file" in registry.names
