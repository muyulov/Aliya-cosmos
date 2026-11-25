"""提示词文件加载：准则只取 visible 区，人格整份读取。"""

from __future__ import annotations

import re
from pathlib import Path

from core.logger import log

_VISIBLE = re.compile(
    r"<!--\s*visible:start\s*-->(.*?)<!--\s*visible:end\s*-->",
    re.S,
)


def load_charter(path: str | Path) -> str:
    """只返回 `visible:start` 与 `visible:end` 之间的内容。"""
    text = Path(path).read_text(encoding="utf-8")
    matched = _VISIBLE.search(text)
    if matched is None:
        raise ValueError(f"准则文件缺少 visible 区：{path}")
    visible = matched.group(1).strip()
    log.debug("准则文件已加载", 文件=str(path), 原文字数=len(text), 可见字数=len(visible))
    return visible


def load_persona(path: str | Path) -> str:
    """人格文件整份读取；空文件直接报错。"""
    text = Path(path).read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"人格文件为空：{path}")
    log.debug("人格文件已加载", 文件=str(path), 字数=len(text))
    return text
