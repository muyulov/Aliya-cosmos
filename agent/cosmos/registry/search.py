"""检索台：在工作区内按文件名或内容查找，只读、不修改任何文件。

跳过版本库、依赖与缓存目录；单文件超过大小上限时跳过，避免把大文件读进内存。
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
from pathlib import Path

from agent.cosmos.contracts import (
    AgentCard,
    AgentContext,
    AgentExample,
    AgentRequest,
    AgentResult,
    DesktopMeta,
    ExecutionPolicy,
)
from core.agent.workspace import (
    WorkspacePathError,
    aliya_workspace_root,
    is_sensitive,
    resolve_in_workspace,
)

#: 遍历时按目录名跳过
SKIP_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
    }
)
#: 按相对根的路径跳过（目录名比较覆盖不到的多级路径）
SKIP_RELATIVE: frozenset[str] = frozenset({"data/models"})
#: 单文件扫描上限（字节）
MAX_FILE_BYTES = 1_000_000
#: 单行最多回显的字符数
MAX_LINE_CHARS = 160


CARD = AgentCard(
    id="search",
    name="检索台",
    icon="🔍",
    summary="在工作区里找文件或内容",
    description=(
        "在 Aliya 的工作区（data/Aliya/workspace）内按文件名或正文查找："
        "mode=files 匹配文件名，mode=content 匹配文件内容。"
        "只读，不改文件、不执行命令；跳过依赖、缓存与版本库目录，不返回二进制内容。"
    ),
    capabilities=["file_search", "code_search"],
    params_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "要找的关键词或文件名片段"},
            "mode": {
                "type": "string",
                "enum": ["content", "files"],
                "description": "content 找正文，files 找文件名",
            },
            "path": {"type": "string", "description": "从哪个子目录开始，默认工作区根"},
            "glob": {"type": "string", "description": "只查匹配的文件，如 *.py"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "required": ["query"],
    },
    examples=[
        AgentExample(input="找一下笔记里提到的 TODO"),
        AgentExample(input="列出所有 md 文件"),
    ],
    desktop=DesktopMeta(group="dev", order=20),
    execution=ExecutionPolicy(
        timeout_seconds=60, max_concurrent=2, queue=True, retryable=True, heartbeat_seconds=20
    ),
)


async def handle(request: AgentRequest, ctx: AgentContext) -> AgentResult:
    query = _text(request.params.get("query"))
    mode = _text(request.params.get("mode")) or "content"
    raw_path = _text(request.params.get("path")) or "."
    pattern = _text(request.params.get("glob")) or None
    limit = min(max(_int(request.params.get("max_results"), default=20), 1), 100)

    if not query:
        return _failed(request, "缺少 query", "需要给出要找的词")
    if mode not in {"content", "files"}:
        return _failed(request, f"未知 mode：{mode}", "只能用 content 或 files")
    base = aliya_workspace_root()
    try:
        root = resolve_in_workspace(raw_path, root=base)
    except WorkspacePathError as exc:
        return _failed(request, str(exc), "只能查你工作区里的目录")
    if not root.exists():
        return _failed(request, f"路径不存在：{raw_path}", "目录可能被移动了")
    if not root.is_dir():
        return _failed(request, f"不是目录：{raw_path}", "path 需要指向一个目录")

    if ctx.cancelled:
        return AgentResult.cancelled(request_id=request.request_id)
    await ctx.report(progress=40, step="查找中")

    hits = await asyncio.to_thread(_scan, root, base, query, mode, pattern, limit)

    await ctx.report(progress=90, step="整理结果")
    if not hits:
        return AgentResult.ok(
            summary=f"没找到「{query}」",
            content=f"在 {root} 下没有匹配项。",
            request_id=request.request_id,
        )
    body = "\n".join(hits)
    return AgentResult.ok(
        summary=f"找到 {len(hits)} 处" + ("（已到上限）" if len(hits) >= limit else ""),
        content=body,
        request_id=request.request_id,
    )


def _scan(
    root: Path,
    base: Path,
    query: str,
    mode: str,
    pattern: str | None,
    limit: int,
) -> list[str]:
    """同步遍历：先按目录剪枝，再按 glob 与内容匹配。

    `root` 是本次搜索起点，`base` 是工作区根：敏感判断必须相对工作区根，
    否则从子目录起搜时，`.env` 这类规则会被当成普通文件名。
    """
    needle = query.lower()
    hits: list[str] = []
    for current, dirs, files in os.walk(root):
        # 就地改 dirs 才能让 os.walk 不再进入这些目录
        dirs[:] = [
            name
            for name in dirs
            if name not in SKIP_DIR_NAMES
            and _relative(Path(current) / name, root).casefold() not in SKIP_RELATIVE
            and not is_sensitive(Path(current) / name, root=base)
        ]
        for name in files:
            path = Path(current) / name
            if is_sensitive(path, root=base):
                continue
            if pattern and not fnmatch.fnmatch(name, pattern):
                continue
            if mode == "files":
                if needle in name.lower():
                    hits.append(f"{_relative(path, root)}")
            else:
                hits.extend(_match_content(path, root, needle))
            if len(hits) >= limit:
                return hits[:limit]
    return hits[:limit]


def _match_content(path: Path, root: Path, needle: str) -> list[str]:
    """逐行找子串；二进制与大文件直接跳过。"""
    try:
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            return []
        with path.open("rb") as handle:
            head = handle.read(4096)
            if b"\x00" in head:
                return []
            _ = handle.seek(0)
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []

    found: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if needle in line.lower():
            snippet = line.strip()[:MAX_LINE_CHARS]
            found.append(f"{_relative(path, root)}:{number}: {snippet}")
    return found


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _int(value: object, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return default


def _failed(request: AgentRequest, message: str, hint: str) -> AgentResult:
    return AgentResult.failed(
        code="invalid_params",
        message=message,
        retriable=False,
        user_hint=hint,
        request_id=request.request_id,
    )
