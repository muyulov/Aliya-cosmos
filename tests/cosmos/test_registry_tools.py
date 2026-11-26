"""内置业务工具：编辑台、检索台、运行台、联网台。

所有用例把工作目录切到 tmp_path，确保沙箱判断与真实仓库无关。
"""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from agent.cosmos.contracts import AgentContext, AgentRequest, Artifact
from agent.cosmos.registry import edit, run, search, web
from core.agent import workspace as workspace_module


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
def _workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cwd 与 Aliya 工作区都指向 tmp_path：run 用 cwd，文件工具用工作区。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(workspace_module, "ALIYA_WORKSPACE", tmp_path)


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


async def test_search_keeps_long_line_whole(tmp_path: Path) -> None:
    long_line = "prefix " + "x" * 1_000 + " needle"
    _ = (tmp_path / "long.txt").write_text(long_line, encoding="utf-8")

    result = await search.handle(make_request("search", query="needle"), ctx())
    assert result.content is not None
    assert "x" * 1_000 in result.content


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


async def test_run_defaults_to_aliya_workspace() -> None:
    result = await run.handle(make_request("run", command="pwd"), ctx())
    assert result.content is not None
    assert workspace_module.aliya_workspace_root().as_posix() in result.content


async def test_run_rejects_cwd_outside_workspace() -> None:
    result = await run.handle(make_request("run", command="pwd", cwd="../"), ctx())
    assert result.status == "failed"
    assert result.error is not None
    assert "超出工作区" in result.error.message


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


async def test_run_output_keeps_head_and_tail() -> None:
    stream = asyncio.StreamReader()
    payload = b"A" * 100 + b"B" * 100
    stream.feed_data(payload)
    stream.feed_eof()

    data, truncated = await run.read_output(stream, limit=60)
    assert truncated is True
    assert data.startswith(b"A")
    assert data.endswith(b"B")
    assert "中间省略".encode() in data


async def test_run_output_under_limit_is_intact() -> None:
    stream = asyncio.StreamReader()
    payload = b"hello world"
    stream.feed_data(payload)
    stream.feed_eof()

    data, truncated = await run.read_output(stream, limit=1_000)
    assert truncated is False
    assert data == payload


# ---------- 联网台 ----------


async def test_web_requires_query() -> None:
    result = await web.handle(make_request("web"), ctx())
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "invalid_params"


async def test_web_rejects_non_http_url() -> None:
    result = await web.handle(
        make_request("web", query="读一下", url="file:///etc/passwd"), ctx()
    )
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "invalid_params"


async def test_web_returns_answer_with_citations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_search(_query: str, _url: str | None) -> tuple[str, list[tuple[str, str]]]:
        return "今天有两条相关消息。", [("示例站", "https://example.com/a")]

    monkeypatch.setattr(web, "request_search", fake_search)
    result = await web.handle(make_request("web", query="今天有什么消息"), ctx())
    assert result.status == "ok"
    assert result.content is not None
    assert "两条相关消息" in result.content
    assert "https://example.com/a" in result.content


async def test_web_passes_url_for_read_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, str | None]] = []

    async def fake_search(query: str, url: str | None) -> tuple[str, list[tuple[str, str]]]:
        seen.append((query, url))
        return "读完了。", []

    monkeypatch.setattr(web, "request_search", fake_search)
    _ = await web.handle(
        make_request("web", query="讲了什么", url="https://example.com/doc"), ctx()
    )
    assert seen == [("讲了什么", "https://example.com/doc")]


async def test_web_truncates_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_search(_query: str, _url: str | None) -> tuple[str, list[tuple[str, str]]]:
        return "字" * 500, []

    monkeypatch.setattr(web, "request_search", fake_search)
    result = await web.handle(make_request("web", query="查", max_chars=200), ctx())
    assert result.status == "ok"
    assert result.content is not None
    assert "正文已截断" in result.content


async def test_web_keeps_full_answer_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_search(_query: str, _url: str | None) -> tuple[str, list[tuple[str, str]]]:
        return "字" * 5_000, []

    monkeypatch.setattr(web, "request_search", fake_search)
    result = await web.handle(make_request("web", query="查"), ctx())
    assert result.status == "ok"
    assert result.content is not None
    assert "正文已截断" not in result.content
    assert len(result.content) >= 5_000


async def test_web_reports_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_search(_query: str, _url: str | None) -> tuple[str, list[tuple[str, str]]]:
        raise web.WebNotConfigured

    monkeypatch.setattr(web, "request_search", fake_search)
    result = await web.handle(make_request("web", query="查"), ctx())
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "not_configured"


async def test_web_maps_search_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_search(_query: str, _url: str | None) -> tuple[str, list[tuple[str, str]]]:
        raise web.WebSearchError("rate_limited", "请求太频繁", retriable=True)

    monkeypatch.setattr(web, "request_search", fake_search)
    result = await web.handle(make_request("web", query="查"), ctx())
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "rate_limited"
    assert result.error.retriable is True


def test_web_dedupes_citations() -> None:
    items = [
        {"type": "url_citation", "url_citation": {"url": "https://a", "title": "A"}},
        {"type": "url_citation", "url_citation": {"url": "https://a", "title": "A"}},
        {"type": "url_citation", "url_citation": {"url": "https://b", "title": "B"}},
    ]
    assert web.extract_citations(items) == [("A", "https://a"), ("B", "https://b")]
