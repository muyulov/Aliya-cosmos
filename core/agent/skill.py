"""Skill 库：扫描 `data/skills/*/SKILL.md`，按需披露正文。

约定：
- 路径固定为 data/skills（模块常量），不做成配置：技能来源可预期，
  部署时不会因为某处写了个绝对路径就去扫描仓库外目录。
- 渐进式披露：system 只放「名称 + 一句话描述」的索引，正文由 load_skill 工具按需读取。
- 目录里的脚本与附件一律不执行、不读取，只认 SKILL.md 正文。
- frontmatter 非法、缺字段、重名都是配置错误，加载期直接抛。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import yaml

from core.agent.errors import SkillError, SkillNotFoundError
from core.agent.tool import Tool, tool
from core.logger import log

#: 技能根目录，相对当前工作目录
SKILLS_ROOT = Path("data/skills")
#: 每个技能目录里的入口文件
SKILL_FILE = "SKILL.md"


@dataclass(frozen=True, slots=True)
class Skill:
    """一份技能说明。"""

    name: str
    description: str
    body: str
    path: Path


class SkillLibrary:
    """已加载的技能集合：索引拼进 system，正文由内置工具按需读取。"""

    def __init__(self, skills: Sequence[Skill] = ()) -> None:
        self._skills: dict[str, Skill] = {}
        for skill in skills:
            if skill.name in self._skills:
                raise SkillError(f"skill 重名：{skill.name}（{skill.path}）")
            self._skills[skill.name] = skill

    @classmethod
    def load(cls, root: Path = SKILLS_ROOT) -> SkillLibrary:
        """扫描技能目录。

        目录不存在按空库处理（data/skills 是可选的，零配置也能跑）；
        子目录里没有 SKILL.md 时静默跳过，留给其它用途。
        """
        if not root.is_dir():
            log.warning("skill 目录不存在，按空库处理", 目录=str(root))
            return cls()
        skills: list[Skill] = []
        for directory in sorted(root.iterdir()):
            path = directory / SKILL_FILE
            if not path.is_file():
                continue
            skills.append(_read_skill(path))
        log.debug("skill 加载完成", 数量=len(skills))
        return cls(skills)

    def __len__(self) -> int:
        return len(self._skills)

    @property
    def skills(self) -> tuple[Skill, ...]:
        """按名称排序的技能列表，顺序稳定便于比对快照。"""
        return tuple(sorted(self._skills.values(), key=lambda skill: skill.name))

    def get(self, name: str) -> Skill:
        """按名取技能；不存在抛 SkillNotFoundError。"""
        skill = self._skills.get(name)
        if skill is None:
            raise SkillNotFoundError(name)
        return skill

    def index(self) -> str:
        """拼进 system 的技能清单；空库返回空串（system_prompt 会自动跳过）。"""
        if not self._skills:
            return ""
        lines = ["可用 skill：需要时调用 load_skill 读取完整说明。"]
        lines.extend(f"- {skill.name}：{skill.description}" for skill in self.skills)
        return "\n".join(lines)

    def tools(self) -> list[Tool]:
        """内置工具：空库不导出 load_skill（模型没有可读的东西，声明只是噪音）。"""
        if not self._skills:
            return []
        return [self._load_tool()]

    def _load_tool(self) -> Tool:
        def load_skill(name: str) -> str:
            """读取指定 skill 的完整说明。

            Args:
                name: skill 名称，取自系统提示里的 skill 清单。
            """
            return self.get(name).body

        return tool(load_skill)


def _read_skill(path: Path) -> Skill:
    """读取并校验一份 SKILL.md。"""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SkillError(f"skill 文件读取失败：{path}：{exc}") from exc
    frontmatter, body = _split_frontmatter(text, path)
    try:
        raw = cast("object", yaml.safe_load(frontmatter))
    except yaml.YAMLError as exc:
        raise SkillError(f"skill frontmatter 解析失败：{path}：{exc}") from exc
    if not isinstance(raw, dict):
        raise SkillError(f"skill frontmatter 必须是映射：{path}")
    meta = cast("dict[str, object]", raw)
    return Skill(
        name=_required_text(meta, "name", path),
        description=_required_text(meta, "description", path),
        body=body.strip(),
        path=path,
    )


def _split_frontmatter(text: str, path: Path) -> tuple[str, str]:
    """切成 frontmatter 与正文；用 ---- 行做分隔（CRLF 也能处理）。"""
    lines = text.lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillError(f"skill 缺少 frontmatter（首行应为 ---）：{path}")
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "\n".join(lines[1:index]), "\n".join(lines[index + 1 :])
    raise SkillError(f"skill frontmatter 没有结束标记 ---：{path}")


def _required_text(meta: dict[str, object], key: str, path: Path) -> str:
    """取必填的文本字段；缺失或空白即配置错误。"""
    value = meta.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SkillError(f"skill frontmatter 缺少 {key}：{path}")
    return value.strip()
