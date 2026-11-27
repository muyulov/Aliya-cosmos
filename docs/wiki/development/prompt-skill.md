# 提示拼装与 Skill

## 1. 系统提示拼装

```python
from core.agent import system_prompt

message = system_prompt("你是行程助手。", library.index(), "当前用户在上海。")
```

| 规则 | 说明 |
| --- | --- |
| 去空白 | 每段去掉首尾空白 |
| 跳过空段 | `None` 与纯空白段直接略过 |
| 空行连接 | 非空段之间插入一个空行 |
| 全空报错 | 所有段都为空时抛 `PromptError` |
| 不做模板 | 变量由调用方 f-string 填好再传 |
| 不裁剪 | 上下文长度属会话层 |

返回值就是 SDK 的 system 消息，可直接放进 `messages`。

## 2. Skill

### 2.1 目录与格式

固定目录 `data/skills`，只扫描一级子目录：

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

1. 先读取文件元信息。
2. 再按页抽取文本。
```

| 项 | 规则 |
| --- | --- |
| 必填字段 | `name`、`description`；其它字段忽略 |
| 正文 | frontmatter 之后的全部内容，加载时读进内存 |
| 缺失或非法 frontmatter | 加载期抛 `SkillError`，带文件路径 |
| 重名 | 加载期抛 `SkillError` |
| 目录不存在 | 按空库处理，仅 WARNING |
| 脚本与附件 | 不读取、不执行，只认 `SKILL.md` 正文 |

### 2.2 渐进式披露

`SkillLibrary` 只把「名称 + 一句话描述」放进系统提示，正文由模型调用内置工具 `load_skill(name)` 按需读取：

```python
library = SkillLibrary.load()

library.index()
# 可用 skill：需要时调用 load_skill 读取完整说明。
# - docx：编辑 Word 文档
# - pdf：处理 PDF 文件：抽取文本、合并拆分、填表单

library.tools()   # [load_skill]
```

空库时 `index()` 返回空串，`tools()` 返回空列表，`system_prompt` 会自动跳过空段，因此零配置也能启动。

### 2.3 使用方式

```python
registry = ToolRegistry()
registry.register(*library.tools())
```

模型读到索引后调用 `load_skill`，注册表把正文回填给它。名字不存在时回填：

```text
工具执行失败：[SkillNotFoundError] 未找到 skill：nope
```

模型可据此改成正确名字，无需中断整轮对话。

## 3. 新增一个 Skill

1. 新建 `data/skills/<name>/SKILL.md`。
2. 写 frontmatter 的 `name` 与 `description`。
3. 正文写操作步骤，模型通过 `load_skill` 读取。
4. 重启后 `SkillLibrary.load()` 自动发现。

仓库自带示例：`data/skills/workspace-tasks/SKILL.md`。
