"""Skill 库测试：扫描、frontmatter 校验、索引与按需读取。"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.agent import (
    SKILLS_ROOT,
    SkillError,
    SkillLibrary,
    SkillNotFoundError,
    ToolRegistry,
)
from core.llm import ToolCall


def _write(root: Path, folder: str, text: str) -> Path:
    path = root / folder / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(text, encoding="utf-8")
    return path


def _skill(name: str, description: str, body: str = "正文") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n"


def test_加载索引与读取(tmp_path: Path) -> None:
    _ = _write(tmp_path, "pdf", _skill("pdf", "处理 PDF 文件", "# 步骤\n先拆页"))
    _ = _write(tmp_path, "docx", _skill("docx", "编辑 Word 文档"))

    library = SkillLibrary.load(tmp_path)

    assert len(library) == 2
    assert library.index() == (
        "可用 skill：需要时调用 load_skill 读取完整说明。\n"
        "- docx：编辑 Word 文档\n"
        "- pdf：处理 PDF 文件"
    )
    assert library.get("pdf").body == "# 步骤\n先拆页"


def test_空目录不导出工具(tmp_path: Path) -> None:
    library = SkillLibrary.load(tmp_path)

    assert len(library) == 0
    assert library.index() == ""
    assert library.tools() == []


def test_目录不存在按空库处理(tmp_path: Path) -> None:
    library = SkillLibrary.load(tmp_path / "missing")

    assert len(library) == 0


def test_没有SKILL文件的子目录被忽略(tmp_path: Path) -> None:
    _ = (tmp_path / "other").mkdir()
    _ = (tmp_path / "other" / "notes.md").write_text("不是技能", encoding="utf-8")
    _ = (tmp_path / "loose.md").write_text("顶层文件", encoding="utf-8")

    assert len(SkillLibrary.load(tmp_path)) == 0


async def test_通过注册表读取skill正文(tmp_path: Path) -> None:
    _ = _write(tmp_path, "pdf", _skill("pdf", "处理 PDF 文件", "第一步：拆页"))
    library = SkillLibrary.load(tmp_path)
    registry = ToolRegistry()
    registry.register(*library.tools())

    body = await registry.execute(ToolCall(id="1", name="load_skill", arguments='{"name": "pdf"}'))
    missing = await registry.execute(
        ToolCall(id="2", name="load_skill", arguments='{"name": "nope"}')
    )

    assert body == "第一步：拆页"
    assert missing == "工具执行失败：[SkillNotFoundError] 未找到 skill：nope"


def test_按名取不存在的skill抛错(tmp_path: Path) -> None:
    library = SkillLibrary.load(tmp_path)

    with pytest.raises(SkillNotFoundError, match="nope"):
        _ = library.get("nope")


def test_缺少frontmatter报错(tmp_path: Path) -> None:
    _ = _write(tmp_path, "bad", "# 只有正文\n")

    with pytest.raises(SkillError, match="缺少 frontmatter"):
        _ = SkillLibrary.load(tmp_path)


def test_frontmatter没有结束标记报错(tmp_path: Path) -> None:
    _ = _write(tmp_path, "bad", "---\nname: bad\ndescription: 缺尾巴\n")

    with pytest.raises(SkillError, match="结束标记"):
        _ = SkillLibrary.load(tmp_path)


def test_frontmatter不是映射报错(tmp_path: Path) -> None:
    _ = _write(tmp_path, "bad", "---\n- 列表\n---\n正文\n")

    with pytest.raises(SkillError, match="必须是映射"):
        _ = SkillLibrary.load(tmp_path)


def test_frontmatter语法错误报错(tmp_path: Path) -> None:
    _ = _write(tmp_path, "bad", "---\nname: [未闭合\n---\n正文\n")

    with pytest.raises(SkillError, match="解析失败"):
        _ = SkillLibrary.load(tmp_path)


@pytest.mark.parametrize(
    ("text", "missing"),
    [
        pytest.param("---\ndescription: 没有名字\n---\n正文\n", "name", id="缺name"),
        pytest.param("---\nname: no_desc\n---\n正文\n", "description", id="缺description"),
    ],
)
def test_缺必填字段报错(tmp_path: Path, text: str, missing: str) -> None:
    _ = _write(tmp_path, "bad", text)

    with pytest.raises(SkillError, match=missing):
        _ = SkillLibrary.load(tmp_path)


def test_重名报错(tmp_path: Path) -> None:
    _ = _write(tmp_path, "one", _skill("same", "第一个"))
    _ = _write(tmp_path, "two", _skill("same", "第二个"))

    with pytest.raises(SkillError, match="重名"):
        _ = SkillLibrary.load(tmp_path)


def test_文件不是合法utf8报错(tmp_path: Path) -> None:
    path = tmp_path / "bad" / "SKILL.md"
    path.parent.mkdir()
    _ = path.write_bytes(b"\xff\xfe\xfa")

    with pytest.raises(SkillError, match="读取失败"):
        _ = SkillLibrary.load(tmp_path)


def test_默认根目录是data下的skills() -> None:
    assert SKILLS_ROOT.as_posix() == "data/skills"
