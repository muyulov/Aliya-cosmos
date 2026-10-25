# Skill 目录

每个技能一个子目录，入口固定为 `SKILL.md`：

```text
data/skills/
  pdf/
    SKILL.md
  docx/
    SKILL.md
```

`SKILL.md` 必须以 YAML frontmatter 开头，`name` 与 `description` 必填：

```markdown
---
name: pdf
description: 处理 PDF 文件：抽取文本、合并拆分、填表单
---

# 使用说明

具体步骤……
```

约定：

- 只扫描一级子目录；没有 `SKILL.md` 的子目录会被忽略。
- 系统提示里只出现「名称 + 一句话描述」，正文由模型调用 `load_skill(name)` 按需读取。
- 目录里除 `SKILL.md` 之外的脚本与附件不会被读取或执行。
- frontmatter 非法、缺字段、重名会在加载期报错（`SkillError`）。
