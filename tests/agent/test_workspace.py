"""工作区沙箱：根定位、路径解析与敏感判断。"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.agent import workspace
from core.agent.workspace import (
    WorkspacePathError,
    aliya_workspace_root,
    resolve_in_workspace,
)


def test_根落在_Aliya_数据目录下(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(workspace, "ALIYA_WORKSPACE", Path("data/Aliya/workspace"))

    root = aliya_workspace_root()
    assert root == (tmp_path / "data" / "Aliya" / "workspace").resolve()
    assert root.is_dir()


def test_根目录不存在时自动创建(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(workspace, "ALIYA_WORKSPACE", Path("nested/ws"))

    assert aliya_workspace_root().is_dir()


def test_相对路径按工作区解析(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(workspace, "ALIYA_WORKSPACE", Path("ws"))
    root = aliya_workspace_root()

    assert resolve_in_workspace("notes/a.md", root=root) == root / "notes" / "a.md"
    with pytest.raises(WorkspacePathError):
        resolve_in_workspace("../outside.md", root=root)


def test_敏感路径大小写不敏感(tmp_path: Path) -> None:
    assert workspace.is_sensitive(tmp_path / ".ENV", root=tmp_path)
    assert workspace.is_sensitive(tmp_path / "DATA" / "state" / "x.json", root=tmp_path)
    assert workspace.is_sensitive(tmp_path / "DATA" / "mcp.json", root=tmp_path)
