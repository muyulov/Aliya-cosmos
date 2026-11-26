"""工作区沙箱：把文件访问限制在仓库根目录内，并挡住敏感路径。

约定：
- 根目录在调用时取 `Path.cwd()`，不做模块级常量：测试与部署可能切换工作目录。
- 只负责「路径能不能碰」，不负责业务语义；读取上限、编码错误由调用方处理。
- 敏感路径按相对根的路径段判断，不跟随软链接逃逸（`resolve()` 之后比较）。
"""

from __future__ import annotations

from pathlib import Path

#: 整段禁区：目录名或目录相对路径命中即拒绝
SENSITIVE_DIRS: frozenset[str] = frozenset(
    {".git", ".env", "data/state", "data/memory", "agent/aliya/data", "logs"}
)
#: 具体文件命中即拒绝（可含相对路径）
SENSITIVE_FILES: frozenset[str] = frozenset({"data/mcp.json", ".git-credentials"})
#: 文件名命中即拒绝
SENSITIVE_NAMES: frozenset[str] = frozenset(
    {".env", ".netrc", ".npmrc", ".pypirc", "id_rsa", "id_ed25519"}
)
#: 后缀命中即拒绝
SENSITIVE_SUFFIXES: frozenset[str] = frozenset({".pem", ".key", ".p12", ".pfx"})

#: 单次读取的字节上限；超出按截断处理，不报错
MAX_READ_BYTES = 200_000


class WorkspacePathError(ValueError):
    """路径不在工作区内，或命中敏感规则。"""


def workspace_root() -> Path:
    """当前工作目录的绝对路径，也是所有相对路径的基准。"""
    return Path.cwd().resolve()


def resolve_in_workspace(raw: str, *, root: Path | None = None) -> Path:
    """把用户给的路径解析成工作区内的绝对路径。

    绝对路径同样要落在根目录内；软链接按解析后的真实路径比较，
    因此 `link -> /etc` 这类逃逸会被拒绝。
    """
    if not raw or not raw.strip():
        raise WorkspacePathError("路径不能为空")
    base = (root or workspace_root()).resolve()
    target = (base / raw.strip()).resolve()
    if not target.is_relative_to(base):
        raise WorkspacePathError(f"路径超出工作区：{raw}")
    if is_sensitive(target, root=base):
        raise WorkspacePathError(f"这个路径不允许访问：{raw}")
    return target


def is_sensitive(path: Path, *, root: Path | None = None) -> bool:
    """判断路径是否命中敏感规则。非工作区内路径一律按敏感处理。

    比较统一 casefold：macOS / Windows 默认大小写不敏感，`.ENV` 与 `.env` 必须同等对待。
    """
    base = (root or workspace_root()).resolve()
    try:
        relative = path.resolve().relative_to(base)
    except ValueError:
        return True
    parts = tuple(part.casefold() for part in relative.parts)
    if any(part in SENSITIVE_NAMES or part.startswith(".env.") for part in parts):
        return True
    posix = relative.as_posix().casefold()
    if posix in SENSITIVE_FILES:
        return True
    # 目录禁区按前缀匹配：data/state/desktop.json 也在禁区内
    if any(posix == item or posix.startswith(f"{item}/") for item in SENSITIVE_DIRS):
        return True
    return path.suffix.casefold() in SENSITIVE_SUFFIXES


def read_text(path: Path, *, max_bytes: int = MAX_READ_BYTES) -> tuple[str, bool]:
    """读文本，超限截断。

    返回 `(正文, 是否截断)`；只读 `max_bytes + 1` 字节，大文件不会整块进内存。
    二进制文件用 `errors="replace"` 兜底，不做编码猜测。
    """
    with path.open("rb") as handle:
        data = handle.read(max_bytes + 1)
    truncated = len(data) > max_bytes
    return data[:max_bytes].decode("utf-8", errors="replace"), truncated
