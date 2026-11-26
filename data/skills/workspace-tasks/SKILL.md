---
name: workspace-tasks
description: 把查文件、改文件、跑测试、读网页交给平板上对应工具
---

# 把活派给平板上的工具

先看一遍平板上有什么（`see_desktop`），再决定用哪个；参数写清楚比写得短重要。

## 找东西

- 找文件名：`dispatch_task(agent_id="search", params={query: "test_", mode: "files"})`
- 找正文：`dispatch_task(agent_id="search", params={query: "TurnRunner", mode: "content", glob: "*.py"})`
- 结果里是 `路径:行号: 内容`，要读上下文再用 `read_file` 取那一段。

## 改东西

- 整段重写：`action="write"`，`content` 必须是完整文件内容，不是片段。
- 追加：`action="append"`，适合往日志、清单末尾加一行。
- 局部替换：`action="replace"`，`old` 必须与原文逐字一致（含缩进）；`new` 不填就是删除。
- 不确定原文长什么样时，先 `read_file` 或派检索台确认，不要凭印象写 `old`。
- 替换失败会返回「没找到要替换的原文」，此时不要重复重试，先读文件。

## 跑命令

- 只用于测试、检查、查看：`pytest tests/aliya`、`ruff check core`、`git status`、`cat pyproject.toml`。
- 运行台不接受安装、发布、推送、删除；遇到这类需求直接告诉对方做不了。

## 读网页

- 用 `dispatch_task(agent_id="web", params={url: "https://..."})`，只支持公开页面。
- 抓回来的是纯文本，引用时挑重点，不要把整页念给对方。

## 失败怎么处理

- `invalid_params`：参数缺了或格式不对，补全后再派一次。
- `not_found`：路径或原文不存在，先查清楚再动。
- `timeout` / `network_error`：可以稍后重试一次，仍失败就如实说明。
- 不要把内部 id、错误码原样念给对方，用自己的话说明结果。
