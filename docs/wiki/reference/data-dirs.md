# 数据目录

`data/` 下同时存放配置骨架与运行时产物。区分原则：**配置可提交，运行时数据不入库**。

| 路径 | 内容 | 是否入库 |
| --- | --- | --- |
| `data/config/cosmos.yaml` | 配置骨架，不含明文密钥 | 是 |
| `data/mcp.json` | MCP server 清单，密钥用占位符 | 是 |
| `data/prompt/Aliya.md` | Aliya 人设，唯一来源 | 是 |
| `data/prompt/Aliya.charter.md` | 人格章程（可见部分进 prompt，实现约束不进） | 是 |
| `data/skills/<name>/SKILL.md` | 技能目录 | 是 |
| `data/world/ship.json` | 船图：舱室、相邻、步行秒数、信号等级 | 是 |
| `data/state/` | cosmos 本地运行时状态 | 否（仅 README 被跟踪） |
| `data/Aliya/` | Aliya 的记忆、生活状态与工具区 | 否 |
| `data/memory/` | 记忆层数据库数据（Qdrant / TigerGraph） | 否 |

## 配置

`data/config/cosmos.yaml` 是配置的唯一来源，路径相对当前工作目录。完整字段见[配置](../config.md)。

## Skill 目录

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

仓库自带一个示例：`workspace-tasks/SKILL.md`，说明怎么给编辑台、检索台、运行台、联网台填参数。Aliya 侧由 `agent.aliya.tools.skills_enabled` 控制是否加载（默认开启）。

## 运行时状态

`data/state` 的文件清单、写入方、可删性与隐私说明见[隐私边界 § 本地运行时数据](../operations/privacy.md#6-本地运行时数据)。

## Aliya 数据根

`data/Aliya/` 是 Aliya 的数据根：记忆文件、生活状态与工具区，运行时产物，不入库。

- `data/Aliya/workspace/` 是文件与命令工具区（不存在时自动创建）。
- 文件与命令类工具（`read_file` 与子 agent 的 `edit` / `search` / `run`）的根都是这里，她要动的东西不会落到项目源码上。
- `activity.json` 是当前生活状态：所在舱室、当前活动、移动进度、精力与最近一次反思日期。
- `activity.jsonl` 是已完成活动的流水，按时间追加。
- 这两个文件属于隐私边界，不通过 `read_file`、`see_desktop` 或任何 RPC 暴露给对端；沙箱根只到 `workspace/`。

## MCP 清单

`data/mcp.json` 沿用生态通用的 `mcpServers` 结构，字段说明见 [MCP 接入](../development/mcp.md)。
