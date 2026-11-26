"""编辑台：在工作区内新建、追加、精确替换文本文件。

安全边界在本模块内实现（08 章 7 节）：
- 路径限制在仓库根目录内，`..` 与软链接逃逸都会被拒绝；
- 敏感路径（`.git`、`.env`、`data/state` 等）不读写；
- 单次写入上限，超限返回 `invalid_params` 而不是截断后落盘。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
from pathlib import Path

from agent.cosmos.contracts import (
    AgentCard,
    AgentContext,
    AgentExample,
    AgentRequest,
    AgentResult,
    Artifact,
    DesktopMeta,
    ExecutionPolicy,
)
from core.agent.workspace import WorkspacePathError, is_sensitive, resolve_in_workspace

#: 单次写入上限（字符）；再大应改用检索台分批处理
MAX_WRITE_CHARS = 200_000
#: 单个目标文件的大小上限（字节）；读与写都受限，避免大文件进内存
MAX_TARGET_BYTES = 2_000_000

CARD = AgentCard(
    id="edit",
    name="编辑台",
    icon="✏️",
    summary="改写仓库内的文件",
    description=(
        "在仓库根目录内修改文本文件：write 整文件覆盖、append 追加、replace 精确替换。"
        "不做目录外操作，不执行命令，不提交 git；replace 找不到原文时不写入任何内容。"
    ),
    capabilities=["file_write", "file_edit", "code_edit"],
    params_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["write", "append", "replace"],
                "description": "write 覆盖、append 追加、replace 精确替换",
            },
            "path": {"type": "string", "description": "相对仓库根的路径"},
            "content": {"type": "string", "description": "write/append 要写入的正文"},
            "old": {"type": "string", "description": "replace 要被替换的原文"},
            "new": {"type": "string", "description": "replace 替换后的文本，缺省为空串"},
            "count": {"type": "integer", "minimum": 1, "description": "replace 最多替换几处"},
        },
        "required": ["action", "path"],
    },
    examples=[
        AgentExample(input="把 README 里那句过时的话改掉"),
        AgentExample(input="新建一个 notes/todo.md，写上三条待办"),
    ],
    desktop=DesktopMeta(group="dev", order=10),
    execution=ExecutionPolicy(
        timeout_seconds=60, max_concurrent=1, queue=True, retryable=True, heartbeat_seconds=20
    ),
)


async def handle(request: AgentRequest, ctx: AgentContext) -> AgentResult:
    action = _text(request.params.get("action"))
    raw_path = _text(request.params.get("path"))
    # 正文与替换串保留原样：strip 会吃掉尾随换行与缩进，精确替换就失效了
    content = request.params.get("content")
    old = request.params.get("old")
    new = request.params.get("new")
    count = max(1, _int(request.params.get("count"), default=1))

    for key, value in (("content", content), ("old", old), ("new", new)):
        if value is not None and not isinstance(value, str):
            return _failed(request, f"{key} 必须是字符串", "参数类型不对")

    text_content = _raw(content)
    text_old = _raw(old)
    text_new = _raw(new)

    if action not in {"write", "append", "replace"}:
        return _failed(request, "缺少或未知 action", "需要 write、append 或 replace")
    if not raw_path:
        return _failed(request, "缺少 path", "需要指定要改的文件")
    # write 允许空正文（清空文件），但必须显式给 content；append 写空等于没写
    if action == "write" and content is None:
        return _failed(request, "write 缺少 content", "需要给出要写入的内容")
    if action == "append" and not text_content:
        return _failed(request, "append 缺少 content", "需要给出要追加的内容")
    if action == "replace" and not text_old:
        return _failed(request, "replace 缺少 old", "需要给出要被替换的原文")
    if len(text_content) > MAX_WRITE_CHARS:
        return _failed(
            request,
            f"内容过长（{len(text_content)} 字，上限 {MAX_WRITE_CHARS}）",
            "内容太长，先拆成几段再写",
        )

    try:
        target = resolve_in_workspace(raw_path)
    except WorkspacePathError as exc:
        return _failed(request, str(exc), "只能改仓库里的文件")

    if ctx.cancelled:
        return AgentResult.cancelled(request_id=request.request_id)
    await ctx.report(progress=20, step="准备写入")

    try:
        summary, detail = await asyncio.to_thread(
            _apply, action, target, text_content, text_old, text_new, count
        )
    except FileNotFoundError as exc:
        return _failed(request, str(exc), "文件可能被移动或删除了", code="not_found")
    except _ReplaceMiss as exc:
        return _failed(request, str(exc), "原文没找到，先确认要替换的内容", code="not_found")
    except _TooLarge as exc:
        return _failed(request, str(exc), "文件太大，先拆分或改用检索台", code="too_large")
    except OSError as exc:
        return _failed(
            request,
            f"写入失败：{exc}",
            "文件可能被占用或没有权限",
            code="io_error",
            retriable=True,
        )

    await ctx.report(progress=90, step="登记结果")
    artifact = Artifact(
        kind="file",
        name=_display(target),
        uri=target.as_uri(),
        size=target.stat().st_size if target.exists() else None,
    )
    await ctx.emit(artifact)
    return AgentResult.ok(
        summary=summary,
        content=detail,
        artifacts=[artifact],
        request_id=request.request_id,
    )


class _ReplaceMiss(Exception):
    """replace 的原文不存在。"""


class _TooLarge(Exception):
    """目标文件超过大小上限。"""


def _apply(
    action: str,
    target: Path,
    content: str,
    old: str,
    new: str,
    count: int,
) -> tuple[str, str]:
    """真正落盘的同步部分，由 `asyncio.to_thread` 调用。"""
    if is_sensitive(target):
        raise OSError("这个路径不允许访问")
    shown = _display(target)

    if action == "write":
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, content)
        return f"写好了 {target.name}", f"覆盖写入 {shown}\n字符数：{len(content)}"

    if action == "append":
        target.parent.mkdir(parents=True, exist_ok=True)
        size = target.stat().st_size if target.exists() else 0
        if size + len(content.encode("utf-8")) > MAX_TARGET_BYTES:
            raise _TooLarge(f"追加后超过 {MAX_TARGET_BYTES} 字节")
        with target.open("a", encoding="utf-8") as handle:
            _ = handle.write(content)
        return f"追加到 {target.name}", f"追加写入 {shown}\n字符数：{len(content)}"

    if not target.is_file():
        raise FileNotFoundError(f"文件不存在：{shown}")
    size = target.stat().st_size
    if size > MAX_TARGET_BYTES:
        raise _TooLarge(f"文件超过 {MAX_TARGET_BYTES} 字节，不能整块替换")
    text = target.read_text(encoding="utf-8")
    total = text.count(old)
    if total == 0:
        raise _ReplaceMiss(f"没找到要替换的原文：{old[:60]}")
    replaced = text.replace(old, new, count)
    _atomic_write(target, replaced)
    changed = min(count, total)
    return (
        f"改好了 {target.name}",
        f"替换 {shown}\n匹配次数：{changed}\n原长：{len(text)} 字符\n新长：{len(replaced)} 字符",
    )


def _atomic_write(target: Path, text: str) -> None:
    """同目录临时文件 + `os.replace`：进程被杀或磁盘满不会留下半截文件。"""
    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            delete=False,
        ) as handle:
            temp_name = handle.name
            _ = handle.write(text)
        os.replace(temp_name, target)
    except BaseException:
        if temp_name:
            with contextlib.suppress(OSError):
                os.unlink(temp_name)
        raise


def _display(target: Path) -> str:
    """展示与产物名用相对路径，避免把主机绝对路径回给模型。"""
    try:
        return target.relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return target.name


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _raw(value: object) -> str:
    """正文/替换串取值：不裁剪空白，保留换行与缩进。"""
    return value if isinstance(value, str) else ""


def _int(value: object, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return default


def _failed(
    request: AgentRequest,
    message: str,
    hint: str,
    *,
    code: str = "invalid_params",
    retriable: bool = False,
) -> AgentResult:
    return AgentResult.failed(
        code=code,
        message=message,
        retriable=retriable,
        user_hint=hint,
        request_id=request.request_id,
    )
