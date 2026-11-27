# 日志

定位问题最快的方式是看结构化字段，而不是把变量拼进字符串。传进 `log.info` 的额外 kwargs 会自动渲染成结构化字段：

```python
from core.logger import log

log.info("用户回合已入队", 参与者="qq:6329133635628374381", 已取消旧计划=0)
```

默认布局是单行（`log.layout: line`）：字段内联成 `键=值`，一条记录（除堆栈外）只占一行，便于 grep 与按行采集：

```text
2026-08-27 23:09:52 [I] dispatcher.py:42 (^_^)/ 用户回合已入队 | 参与者=qq:6329133635628374381 | 已取消旧计划=0
```

`log.layout: tree` 换成缩进树，人读更清楚：

```text
2026-08-27 23:09:52 [I] dispatcher.py:42 (^_^)/ 用户回合已入队
    ├─ 参与者: qq:6329133635628374381
    └─ 已取消旧计划: 0
```

级别后面的 `dispatcher.py:42` 是调用点来源（文件:行号），由 `log.source` 控制，默认开启。它由日志门面自动跳过自身与包装层得到，不需要调用点手写；`Service.log_error` 这类包装也会把来源落在真正调用它的业务代码上。压日志体积或对接固定格式采集时设 `log.source: false`。

颜文字按级别自动选择，也可用 `face=` 指定，常量表在 `core/logger/faces.py`。`log.json: true` 时改为单行 JSON，字段平铺，便于日志采集；`time` / `level` / `message` / `face` / `exception` / `source` 是保留键，业务字段与它们同名时以保留键为准（单行与树形格式没有这个限制——业务字段独立于元数据，不与之混排）。

`log.color` 控制控制台着色（时间戳暗灰、级别与消息按级别上色、字段名暗灰），`always` 可在 IDE 输出窗、CI 面板这类拿不到 `isatty` 的地方强制打开。**颜色只进控制台**：文件 sink 永远不写着 ANSI 转义序列。

`context.request_scope` 作用域内的 `request_id` 会自动携带，调用点无需手写字段；标准库与第三方库（`sqlalchemy` / `httpx` / `asyncio`）经桥接后的日志同样携带这些上下文：

```python
from core.logger import context, log

with context.request_scope():
    log.info("处理订单", 订单号="A001")  # request_id 自动出现在字段里
```

需要临时附加业务维度时用上下文管理器，退出自动还原：

```python
with log.context(会话="qq:123"):
    log.info("会话内")
```

异常堆栈会渲染成独立块，不与字段混淆：

```text
2026-09-12 10:00:00 [E] (x_x) 未捕获异常
    ├─ 阶段: 装配
    ├─ 异常: KeyError
    └─ 堆栈
       Traceback (most recent call last):
         ...
       KeyError: 'item_id'
```

日志文件有两个：`logs/cosmos-main-<启动时间戳>.log`（跟随 `log.level`）与 `logs/cosmos-error-<启动时间戳>.log`（固定 ERROR 级），均按天轮转。文件名默认带 `{time}` 模板，**每次启动写一组独立文件**，两次启动的内容不会混在一起；`setup_logging()` 返回的 `LogFiles(app=…, error=…)` 就是本次实际写入的路径。

保留 `{…}` 占位符不只是为了命名：loguru 的 `retention` 靠它识别同族文件，改成固定名（`app.log`）后旧文件将永远清不掉。

## 日志约定

各层共同遵守的约定：

| 约定 | 说明 |
| --- | --- |
| 不记正文 | 只记标识、耗时、状态与错误类型，不记消息正文、工具参数与返回值、用户状态原始值、文件内容 |
| 统一错误格式 | 服务内用 `self.log_error("消息", exc)`，自动拆出「错误类型 / 错误消息」两个字段 |
| 来源自动 | 调用点 `文件名:行号` 由门面自动计算，不手写 |
| 上下文复用 | 跨调用链的维度用 `log.context(...)`，退出自动还原 |

各层的具体日志字段见：

- [LLM 层](../capabilities/llm.md)
- [向量层](../capabilities/embedding.md)
- [判断层](../capabilities/decision.md)
- [Agent 工具层](../capabilities/tools.md)
- [cosmos 各服务](../agents/cosmos/overview.md)
