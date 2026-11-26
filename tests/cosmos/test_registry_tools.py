"""内置业务工具：编辑台、检索台、运行台、联网台。

所有用例把工作目录切到 tmp_path，确保沙箱判断与真实仓库无关。
"""

from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import httpx2
import pytest

from agent.cosmos.contracts import AgentContext, AgentRequest, Artifact
from agent.cosmos.registry import edit, run, search, web


class FakeContext:
    """记录上报与产物；取消标志由用例按需置位。"""

    def __init__(self) -> None:
        self.request_id: str = "r1"
        self.cancelled: bool = False
        self.remaining_seconds: float | None = None
        self.reports: list[dict[str, object]] = []
        self.artifacts: list[Artifact] = []

    async def report(
        self,
        *,
        progress: float | None = None,
        step: str | None = None,
        message: str | None = None,
    ) -> None:
        self.reports.append({"progress": progress, "step": step, "message": message})

    async def heartbeat(self) -> None:
        return None

    async def emit(self, artifact: Artifact) -> None:
        self.artifacts.append(artifact)

    async def read_presence(self) -> None:
        return None

    def child_scope(self, name: str) -> object:
        _ = name
        return nullcontext()


def make_request(agent_id: str, **params: object) -> AgentRequest:
    return AgentRequest(
        request_id="r1",
        agent_id=agent_id,
        params=params,
        label="测试任务",
        created_at=datetime.now(UTC),
    )


def ctx() -> AgentContext:
    return cast("AgentContext", cast("object", FakeContext()))


@pytest.fixture(autouse=True)
def _cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


# ---------- 编辑台 ----------


async def test_edit_write_creates_file(tmp_path: Path) -> None:
    result = await edit.handle(
        make_request("edit", action="write", path="notes/todo.md", content="一\n二\n"),
        ctx(),
    )
    assert result.status == "ok"
    assert "todo.md" in result.summary
    assert (tmp_path / "notes" / "todo.md").read_text(encoding="utf-8") == "一\n二\n"


async def test_edit_replace_misses_original() -> None:
    result = await edit.handle(
        make_request("edit", action="replace", path="missing.md", old="a", new="b"),
        ctx(),
    )
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "not_found"


async def test_edit_rejects_path_escape() -> None:
    result = await edit.handle(
        make_request("edit", action="write", path="../outside.txt", content="x"),
        ctx(),
    )
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "invalid_params"
    assert "超出工作区" in result.error.message


async def test_edit_rejects_sensitive_path() -> None:
    result = await edit.handle(
        make_request("edit", action="write", path=".env", content="TOKEN=1"),
        ctx(),
    )
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "invalid_params"
    assert "不允许访问" in result.error.message


async def test_edit_rejects_non_string_content(tmp_path: Path) -> None:
    result = await edit.handle(
        make_request("edit", action="write", path="a.txt", content=123),
        ctx(),
    )
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "invalid_params"
    assert not (tmp_path / "a.txt").exists()


async def test_edit_replace_writes_file(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    _ = path.write_text("log.x\nlog.y\n", encoding="utf-8")
    result = await edit.handle(
        make_request("edit", action="replace", path="a.txt", old="log.", new="logger.", count=5),
        ctx(),
    )
    assert result.status == "ok"
    assert path.read_text(encoding="utf-8") == "logger.x\nlogger.y\n"


async def test_edit_replace_count_zero_treated_as_one(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    _ = path.write_text("x x\n", encoding="utf-8")
    result = await edit.handle(
        make_request("edit", action="replace", path="a.txt", old="x", new="y", count=0),
        ctx(),
    )
    assert result.status == "ok"
    assert path.read_text(encoding="utf-8") == "y x\n"


async def test_edit_requires_content_for_write() -> None:
    result = await edit.handle(make_request("edit", action="write", path="a.txt"), ctx())
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "invalid_params"


# ---------- 检索台 ----------


async def test_search_finds_content(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    _ = (tmp_path / "src" / "demo.py").write_text(
        "class TurnRunner:\n    pass\n", encoding="utf-8"
    )

    result = await search.handle(
        make_request("search", query="turnrunner", mode="content", glob="*.py"),
        ctx(),
    )
    assert result.status == "ok"
    assert result.content is not None
    assert "src/demo.py:1" in result.content


async def test_search_finds_file_names(tmp_path: Path) -> None:
    _ = (tmp_path / "test_one.py").write_text("", encoding="utf-8")
    result = await search.handle(make_request("search", query="test_", mode="files"), ctx())
    assert result.status == "ok"
    assert result.content is not None
    assert "test_one.py" in result.content


async def test_search_rejects_outside_path() -> None:
    result = await search.handle(
        make_request("search", query="x", path="../"), ctx()
    )
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "invalid_params"


async def test_search_skips_sensitive_dir(tmp_path: Path) -> None:
    hidden = tmp_path / "data" / "state"
    hidden.mkdir(parents=True)
    _ = hidden.joinpath("secret.txt").write_text("needle", encoding="utf-8")

    result = await search.handle(make_request("search", query="needle"), ctx())
    assert result.status == "ok"
    assert "secret.txt" not in (result.content or "")


# ---------- 运行台 ----------


async def test_run_executes_allowed_command() -> None:
    result = await run.handle(make_request("run", command="pwd"), ctx())
    assert result.status == "ok"
    assert result.content is not None
    assert "退出码：0" in result.content


async def test_run_rejects_unlisted_command() -> None:
    result = await run.handle(make_request("run", command="rm -rf ."), ctx())
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "invalid_params"


async def test_run_rejects_dangerous_git() -> None:
    result = await run.handle(make_request("run", command="git push origin main"), ctx())
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "invalid_params"


async def test_run_rejects_outside_path_argument() -> None:
    result = await run.handle(make_request("run", command="pytest /etc/passwd"), ctx())
    assert result.status == "failed"
    assert result.error is not None
    assert "工作区外" in result.error.message


async def test_run_rejects_parent_path_argument() -> None:
    result = await run.handle(make_request("run", command="ruff check ../other"), ctx())
    assert result.status == "failed"
    assert result.error is not None
    assert "工作区外" in result.error.message


async def test_run_rejects_command_with_path() -> None:
    result = await run.handle(make_request("run", command="/tmp/git status"), ctx())
    assert result.status == "failed"
    assert result.error is not None
    assert "不能带路径" in result.error.message


async def test_run_rejects_git_write_subcommand() -> None:
    result = await run.handle(make_request("run", command="git commit -m msg"), ctx())
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "invalid_params"


def test_validate_allows_uv_run_pytest() -> None:
    assert run.validate_command(["uv", "run", "pytest", "-q"]) is None


def test_validate_allows_ruff_check() -> None:
    assert run.validate_command(["ruff", "check", "core"]) is None


def test_validate_rejects_ruff_format() -> None:
    assert run.validate_command(["ruff", "format", "core"]) is not None


def test_validate_rejects_python_script() -> None:
    assert run.validate_command(["python", "script.py"]) is not None


def test_validate_rejects_uv_arbitrary_command() -> None:
    assert run.validate_command(["uv", "run", "bash", "x.sh"]) is not None


def test_validate_rejects_option_with_outside_value() -> None:
    assert run.validate_command(["pytest", "--basetemp=/tmp/x"]) is not None
    assert run.validate_command(["git", "log", "--output=/tmp/x"]) is not None


def test_validate_rejects_fix_option() -> None:
    assert run.validate_command(["ruff", "check", "--fix"]) is not None


# ---------- 联网台 ----------


async def test_web_rejects_non_http_scheme() -> None:
    result = await web.handle(make_request("web", url="file:///etc/passwd"), ctx())
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "invalid_params"


async def test_web_rejects_localhost() -> None:
    result = await web.handle(make_request("web", url="http://localhost:8000/"), ctx())
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "invalid_params"


async def test_web_rejects_private_ip() -> None:
    result = await web.handle(make_request("web", url="http://127.0.0.1/"), ctx())
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "invalid_params"
    assert "内网" in result.error.message


async def test_web_guard_blocks_redirect_target() -> None:
    """重定向的每一跳都要过同一个守卫，公网跳到内网会被拦下。"""
    target = httpx2.Request("GET", "http://169.254.169.254/latest/meta-data/")
    with pytest.raises(web.BlockedHost):
        await web.guard_request(target)
