# aliya-cosmos

Python + uv 通用脚手架。三层结构：`service` / `logger` / `config`，开箱可跑，后续往里填业务。

技术栈：Python 3.12+、uv、pydantic、PyYAML、loguru、openai、ruff、pytest。

## 快速开始

```bash
# 安装依赖
uv sync

# 启动服务（Ctrl-C 优雅关闭）
uv run python -m core.main
```

无需任何配置即可启动，所有配置项都有默认值。要自定义时改 `data/config/cosmos.yaml`；密钥类配置写在 `.env`（参考 `.env.example`）。

进程常驻，`SIGINT` / `SIGTERM` 触发优雅关闭。信号注册优先交给事件循环（POSIX 的标准做法）；Windows 的事件循环不实现 `add_signal_handler`，会自动退回 `signal.signal`，并经 `call_soon_threadsafe` 置位。

## 常用命令

| 目的 | 命令 |
| --- | --- |
| 安装依赖 | `uv sync` |
| 启动服务 | `uv run python -m core.main` |
| 跑测试 | `uv run pytest` |
| 覆盖率 | `uv run pytest --cov=core` |
| 代码检查 | `uv run ruff check .` |
| 代码格式化 | `uv run ruff format .` |

## 目录结构

```text
core/
  main.py        入口：装配服务容器并驱动生命周期
  service/       业务层：服务基类、容器、注册表、具体服务实现
  logger/        日志层：颜文字、结构化上下文、单行/树形/JSON 格式化、sink 装配
  config/        配置层：YAML 骨架加载与占位符插值
  embedding/     向量层：单条 / 批量文本向量化
  llm/           LLM 层：对话 / 流式 / 结构化 / 工具调用 / 多模态
  decision/      判断层：批量提问与 choice / score / noul 三原语
data/           配置与运行数据：config/cosmos.yaml 为配置骨架，可安全提交
tests/           测试
```

依赖方向单向：`service → (config, logger)`、`embedding / llm / decision → (config, logger, service)`。`service` 层不引用任何框架，可被 CLI、定时任务、测试直接复用。唯一的例外是 `core/service/registry.py`：它要 import 具体服务类才能完成注册，因此这个模块反向依赖 `embedding` / `llm` / `decision`；它也因此不挂在 `core/service/__init__.py` 上（会成环），装配入口直接从 `core.service.registry` 取。

## 配置

配置以 `data/config/cosmos.yaml` 为唯一来源（路径相对当前工作目录）。该文件可安全提交，因为它不含任何明文密钥。要调整配置，改这个文件即可。

```yaml
app:
  env: ${APP_ENV:dev}        # 部署时可用环境变量顶掉
log:
  level: INFO
  json: false
  layout: line               # line（默认，字段内联成一行）/ tree（字段成树）
  color: auto                # auto（仅终端）/ always / never
```

### 占位符

`${VAR}` 与 `${VAR:默认值}` 两种写法，取值来源是进程环境变量与 `.env`（[进程环境变量优先](https://12factor.net/zh_cn/config)）：

| 写法 | 语义 |
| --- | --- |
| `${DEEPSEEK_API_KEY}` | 取不到值就启动失败，进程退出 |
| `${APP_ENV:dev}` | 取不到值时用 `dev` |
| `$${VAR}` | 字面 `${VAR}`，不取值（`$$` 转义成 `$`） |

占位符只作用于字符串值，`cosmos.yaml` 里 dict 的键不会被替换；展开后一律是字符串，类型由 pydantic 转换（如 `"true"` → `bool`）。

默认只展开一趟：变量值里再写 `${B}` 不会继续展开，避免链式取值带来的隐式依赖。确实需要链式引用时，用 `load_settings(..., expand_rounds=2)` 显式开启。

`.env` 的唯一职责就是给占位符喂值，**不能**直接覆盖配置项。变量名大小写敏感：`${App_Key}` 与 `${APP_KEY}` 是两个不同的名字。

取值表包含**全部**进程环境变量，因此 `${PATH}`、`${HOME}` 这类写法会直接命中系统变量——建议用项目自己的变量名（如 `APP_` 前缀）避免撞车。

### 配置项

| YAML 路径 | 默认值 | 说明 |
| --- | --- | --- |
| `app.app_name` | `aliya-cosmos` | 应用名 |
| `app.env` | `dev` | 环境，只能是 dev / test / prod |
| `app.debug` | `true` | 调试开关 |
| `log.level` | `INFO` | 日志级别 |
| `log.json` | `false` | 是否输出 JSON 日志 |
| `log.layout` | `line` | 输出布局：`line`（字段内联成一行，便于 grep 与按行采集）/ `tree`（字段缩进成树） |
| `log.color` | `auto` | 控制台着色：`auto`（仅终端）/ `always` / `never`；日志文件永远不带颜色 |
| `log.dir` | `logs` | 日志目录 |
| `log.file_name` | `app-{time:%Y%m%d-%H%M%S}.log` | 主日志文件名，支持 loguru 时间模板（装配时求值，故每次启动一个独立文件） |
| `log.error_file_name` | `error-{time:%Y%m%d-%H%M%S}.log` | 错误日志文件名，同上 |
| `log.rotation` | `00:00` | 轮转阈值（默认按天），写进 YAML 时必须加引号 |
| `log.retention` | `7 days` | 保留时长 |
| `log.compression` | `zip` | 归档压缩方式 |
| `service.start_timeout` | `30` | 单个服务 `start()` 的超时秒数，必须为正数；写 `null` 表示不限制 |
| `service.stop_timeout` | `30` | 单个服务 `stop()` 的超时秒数，必须为正数；同上 |
| `clock.tz` | `UTC` | 时钟服务的时区，如 `Asia/Shanghai` |

`embedding` 是独立的向量化端点，与 llm 分开配（DeepSeek 没有 embedding 端点，两家通常不是同一个服务）：

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `base_url` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | OpenAI 兼容端点地址 |
| `api_key` | 空 | 密钥，YAML 里写 `${DASHSCOPE_API_KEY:}` 由 `.env` 提供；留空则该端点不可用（只警告，不影响启动） |
| `model` | `text-embedding-v3` | 向量化模型 |
| `timeout` | `60` | 单次请求超时秒数，必须为正数 |
| `retries` | `2` | SDK 重试次数，`0` 关闭 |
| `batch_size` | `10` | 单次请求最多几条文本；阿里 v3 上限就是 10（v1/v2 为 25、OpenAI 为 2048），超了会 400 |
| `dimensions` | `null` | `null` 用模型原始维度；设值则请求截断，做不到的模型会报错 |

`llm.chat` 与 `llm.vision` 是两个端点，字段完全相同（可指向同一服务，也可分开接不同供应商）：

| 端点字段 | 默认 | 说明 |
| --- | --- | --- |
| `base_url` | `https://api.openai.com/v1` | 端点地址，接 DeepSeek 等兼容端点时改成对应地址 |
| `api_key` | 空 | 密钥，YAML 里写 `${DEEPSEEK_API_KEY:}` 由 `.env` 提供；留空则该端点不可用（只警告，不影响启动） |
| `model` | `gpt-4o-mini` | 该端点的模型；给 `chat` 传 `model=svc.vision_model` 即走 vision 端点 |
| `timeout` | `60` | 单次请求超时秒数，必须为正数 |
| `retries` | `2` | SDK 重试次数，`0` 关闭 |
| `structured_mode` | `json_schema` | 结构化输出模式，兼容端点不支持时改 `json_object` |
| `temperature` | `null` | 温度，`null` 表示不传该参数（用服务端默认） |
| `max_tokens` | `null` | 最大输出 token，`null` 表示不传该参数 |

`decision.jev`（云端）与 `decision.laya`（本地 laya-serve）是两个端点，字段完全相同，同一套 `/systemone` 协议；`base_url` 只写到 `/v1` 为止，路径 `/systemone` 由代码补上：

| 端点字段 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `false` | 是否启用该端点；两个端点默认都关着，进程照常启动，调用时才报未启用 |
| `base_url` | jev `https://api.typesafe.ai/v1` / laya `http://127.0.0.1:8000/v1` | 端点地址，只写到 `/v1`，路径 `/systemone` 由代码补 |
| `api_key` | 空 | 密钥，YAML 里写 `${TYPESAFE_API_KEY:}` / `${LAYA_API_KEY:}` 由 `.env` 提供；留空则不发 `Authorization` 头（laya-serve 默认不要求认证） |
| `model` | jev `jev-latest` / laya 空 | 该端点的模型；Jev 必填（空会 422），Laya 留空表示不带 `model` 字段、交给 Router 按语种自动选 checkpoint |
| `timeout` | `60` | 单次请求超时秒数，必须为正数 |
| `retries` | `2` | 429 / 529 的退避重试次数，`0` 关闭 |

### 失败行为

配置在进程启动时加载，出错即退出：

| 场景 | 行为 |
| --- | --- |
| `data/config/cosmos.yaml` 不存在 | 全部走默认值继续启动，启动日志显示 `配置源=内置默认值（未找到 …）` |
| `data/config/cosmos.yaml` 存在但为空 | 同上（全部走默认值），启动日志显示 `配置源=…（空文件，全部走默认值）` |
| 配置文件不是合法 UTF-8，或读不动（权限等） | 报错退出 |
| YAML 语法错误或顶层不是映射 | 报错退出 |
| 占位符取不到值且没写默认值 | 报错退出，并指出是哪个变量 |
| 字段取值非法（如 `env: staging`、`log.level: BOGUS`） | 报错退出 |
| 某个服务的 `start()` 抛错或超时 | 回滚本次启动过的服务，记录 `应用启动失败，进程退出`（带堆栈），以非零码退出 |

启动日志里的 `配置源` 字段会写明本次配置来自哪个文件，路径是相对工作目录解析的，从别处启动时请核对这一项。

## 日志

定位问题最快的方式是看结构化字段，而不是把变量拼进字符串。传进 `log.info` 的额外 kwargs 会自动渲染成结构化字段：

```python
from core.logger import log

log.info("用户回合已入队", 参与者="qq:6329133635628374381", 已取消旧计划=0)
```

默认布局是单行（`log.layout: line`）：字段内联成 `键=值`，一条记录（除堆栈外）只占一行，便于 grep 与按行采集：

```text
2026-08-27 23:09:52 [I] (^_^)/ 用户回合已入队 | 参与者=qq:6329133635628374381 | 已取消旧计划=0
```

`log.layout: tree` 换成缩进树，人读更清楚：

```text
2026-08-27 23:09:52 [I] (^_^)/ 用户回合已入队
    ├─ 参与者: qq:6329133635628374381
    └─ 已取消旧计划: 0
```

颜文字按级别自动选择，也可用 `face=` 指定，常量表在 `core/logger/faces.py`。`log.json: true` 时改为单行 JSON，字段平铺，便于日志采集；`time` / `level` / `message` / `face` / `exception` 是保留键，业务字段与它们同名时以保留键为准（单行与树形格式没有这个限制——业务字段独立于元数据，不与之混排）。

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

日志文件有两个：`logs/app-<启动时间戳>.log`（跟随 `log.level`）与 `logs/error-<启动时间戳>.log`（固定 ERROR 级），均按天轮转。文件名默认带 `{time}` 模板，**每次启动写一组独立文件**，两次启动的内容不会混在一起；`setup_logging()` 返回的 `LogFiles(app=…, error=…)` 就是本次实际写入的路径。

保留 `{…}` 占位符不只是为了命名：loguru 的 `retention` 靠它识别同族文件，改成固定名（`app.log`）后旧文件将永远清不掉。

## LLM 层

`LLMService` 把「调大模型」收成一个服务，业务代码不直接接触 SDK。五项能力：

```python
from pydantic import BaseModel

from core.llm import (
    LLMService,
    assistant,
    image_url,
    system,
    tool,
    tool_result,
    user,
    user_with_images,
)


class Item(BaseModel):
    name: str
    price: float


class WeatherArgs(BaseModel):
    city: str


async def main(svc: LLMService, picture_url: str) -> None:
    # 对话
    reply = await svc.chat([system("你是助手"), user("你好")])

    # 流式：逐段产出，不自动拼接
    parts = [part async for part in svc.stream([user("讲个故事")])]

    # 结构化输出
    item = await svc.chat_structured([user("苹果多少钱")], Item)

    # 工具调用（单轮）：回传与循环由调用方写
    messages = [user("上海天气")]
    call_reply = await svc.chat_tools(messages, [tool("get_weather", "查天气", WeatherArgs)])
    if call_reply.tool_calls:
        # 要第二轮就自己拼：assistant() 原样带回 tool_calls，tool_result() 回填结果
        call = call_reply.tool_calls[0]
        messages += [assistant(tool_calls=call_reply.tool_calls), tool_result(call.id, "晴 26℃")]
        final = await svc.chat(messages)

    # 多模态：不单独开方法，传 vision 端点的模型即走 vision 端点
    seen = await svc.chat(
        [user_with_images("这是什么", image_url(picture_url))], model=svc.vision_model
    )
```

约定：

| 约定 | 说明 |
| --- | --- |
| 不记正文 | 日志只记模型 / 端点 / 耗时 / token 用量；消息正文该不该记由调用方自己决定并自己打 |
| 不自动多轮 | `chat_tools` 只发一轮并返回 `tool_calls`，回传用 `assistant(tool_calls=...)` + `tool_result(...)`，循环由调用方写 |
| chat / vision 双端点 | 两个端点各自带连接与采样参数，可指向同一服务也可分开接；给 `chat` 传 `model=svc.vision_model` 就走 vision 端点，vision 没启用时不会接管（两头的 model 常常同名）。用 Chat Completions 而非 Responses API，因为兼容端点普遍只实现前者 |
| 缺密钥不 fail fast | 端点没配 `api_key` 时只警告、不建客户端；两个端点全没配健康检查才 unhealthy，调用时才抛 `LLMConfigError` |
| 重试交给 SDK | 超时与重试由端点的 `timeout` / `retries` 控制；流式一旦开始消费就不再重试，断流是否重放由调用方决定 |
| `json_object` 要提示词配合 | 该模式要求提示词里出现 json 字样，属调用方责任 |

错误全部继承 `LLMError`，原始 SDK 异常挂在 `__cause__`：

| 类型 | 触发条件 |
| --- | --- |
| `LLMConfigError` | 端点未配 `api_key` |
| `LLMRequestError` | 端点返回 4xx / 5xx（带 `status_code` / `endpoint` / `model` / `request_id`） |
| `LLMTimeoutError` | 请求超时 |
| `LLMConnectionError` | 连不上端点（超时除外） |
| `LLMSchemaError` | 结构化输出不符合给定 schema |
| `LLMResponseError` | 返回体缺内容（choices 为空、`content` 为 `None`） |

## 向量层

`EmbeddingService` 把「文本转向量」收成一个服务，业务代码不直接接触 SDK：

```python
from core.embedding import EmbeddingService


async def main(svc: EmbeddingService, texts: list[str]) -> None:
    # 单条
    vector = await svc.embed("要向量化的文本")

    # 批量：按 batch_size 切片、逐批串行请求，结果顺序与入参一致
    vectors = await svc.embed_many(texts)

    # 截断到指定维度：调用覆盖 > 配置 dimensions > 不传（用模型原始维度）
    short = await svc.embed("要向量化的文本", dimensions=512)
```

约定：

| 约定 | 说明 |
| --- | --- |
| 原样透传、不归一化 | 返回的是模型原始向量，模长信息不丢；要不要 L2 归一化由调用方决定 |
| 空序列早退 | `embed_many([])` 返回 `[]`，不发请求、不查密钥（空输入不需要服务）；空串与全空白串在本地抛 `EmbeddingInputError` |
| `embed_many` 只收序列 | 传裸字符串（`str` 本身就是 `Sequence[str]`）会抛 `EmbeddingInputError`，否则会被静默按字符拆成 N 条向量；单个字符串请用 `embed()` |
| 缺密钥不 fail fast | 没配 `api_key` 时只警告、不建内核，健康检查 unhealthy，调用时才抛 `EmbeddingConfigError` |
| 重试交给 SDK | 超时与重试由端点的 `timeout` / `retries` 控制 |
| 不记正文 | 日志只记模型 / 端点 / 文本数 / 维度 / 耗时 / 输入 token |

错误全部继承 `EmbeddingError`，原始 SDK 异常挂在 `__cause__`：

| 类型 | 触发条件 |
| --- | --- |
| `EmbeddingConfigError` | 端点未配 `api_key` |
| `EmbeddingInputError` | 文本为空或全空白 |
| `EmbeddingRequestError` | 端点返回 4xx / 5xx（带 `status_code` / `endpoint` / `model` / `request_id`） |
| `EmbeddingTimeoutError` | 请求超时 |
| `EmbeddingConnectionError` | 连不上端点（超时除外） |
| `EmbeddingResponseError` | 返回体与请求对不上：条数不符 / `index` 越界或重复 / 空向量 / 声明维度不符 |

## 判断层

`DecisionService` 把「让模型做一次判断」收成一个服务，业务代码不直接接触 HTTP。两个端点（jev 云端 / laya 本地）走**同一套 `/systemone` 协议、字段完全相同**——换 `base_url` 即可切换供应商。`backend` 显式选端点（默认 `laya`，本地优先：免费、数据不出域），**层内不做兜底**。

```python
from core.decision import DecisionService, choice, noul, score


async def main(svc: DecisionService, message: str) -> None:
    # 批量：一次前向把三题问完（协议的延迟优势就在这里）
    result = await svc.predict(
        message,
        {
            "部门": choice("归哪个部门？", {"billing": "账单", "technical": "技术故障"}),
            "愤怒": score("愤怒程度？", ["平静", "不满", "愤怒"]),
            "退款": noul("是否明确要求退款？", true="明确要求", false="没提"),
        },
    )
    department = result.answers["部门"]

    # 便捷方法：一次只问一题
    picked = await svc.choose(message, "归哪个部门？", {"billing": "账单", "technical": "技术"})
    anger = await svc.rate(message, "愤怒程度？", ["平静", "不满", "愤怒"])
    wants_refund = await svc.ask(message, "是否明确要求退款？")

    # 换后端：默认 laya（本地），要云端就传 backend
    on_cloud = await svc.ask(message, "是否明确要求退款？", backend="jev")
```

约定：

| 约定 | 说明 |
| --- | --- |
| 不记正文 | 日志只记模型 / 端点 / 问题数 / 耗时 / token；`state` 与 `questions` 的正文由调用方决定要不要自己记 |
| `confidence` 不能跨后端比较 | Jev 的 `confidence` 是 `(n·p_max−1)/(n−1)`，Laya 是 `1−归一化熵`，两个数不是同一个量；阈值要按当前 `backend` 单独校准 |
| 零样本质量很差 | 官方 model card 自述「Laya 是便于特化的基座，不是零样本决策引擎」（零样本准确率接近随机），判断结果必须配阈值或人工兜底 |
| 两端点同协议 | jev 与 laya 走同一套 `/systemone` 协议、字段完全相同，换 `base_url` 即可切换供应商 |
| `backend` 显式选 | 默认 `laya`（本地优先），要云端显式传 `backend="jev"` |
| 层内不兜底 | 一个端点未启用时直接抛 `DecisionConfigError`，不会静默换另一头；`confidence` 跨端点语义不同，兜底必然出错 |
| 缺端点不 fail fast | 两个端点都没启用时只让健康检查 unhealthy，进程照常启动，调用时才报未启用 |
| 重试按状态码 | 429 / 529 按指数退避重试（次数由端点 `retries` 控制），其余 4xx / 5xx 不重试 |

错误全部继承 `DecisionError`，原始 `httpx2` 异常挂在 `__cause__`：

| 类型 | 触发条件 |
| --- | --- |
| `DecisionConfigError` | 调用的端点未启用 |
| `DecisionRequestError` | 端点返回 4xx / 5xx（带 `status_code` / `endpoint` / `model` / `request_id`） |
| `DecisionTimeoutError` | 请求超时 |
| `DecisionConnectionError` | 连不上端点（超时除外） |
| `DecisionResponseError` | 返回体不合法（非 JSON / 顶层不是对象 / 缺 answers / 答案字段不完整） |

## 如何新增一个服务

服务是继承 `Service` 的类，`ServiceManager` 负责装配、启停与健康检查。
依赖按**类型**声明，容器在构造时注入。

1. 在 `core/service/` 新建文件，继承 `Service`：

```python
from typing import ClassVar

from core.config import CacheSettings
from core.service.base import Service


class CacheService(Service):
    # 依赖的服务类型；容器据此排序并在构造时注入
    dependencies: ClassVar[tuple[type[Service], ...]] = (DbService,)

    def __init__(self, db: DbService, config: CacheSettings) -> None:
        super().__init__()  # 必须调用：负责设置 state 与 self.log
        self._db = db
        self._config = config

    async def start(self) -> None: ...  # 建连接
    async def stop(self) -> None: ...  # 关连接
```

2. 在 `core/service/registry.py` 里注册**类型**（不是实例）：

```python
manager.register(CacheService)  # 支持一次传入多个类型：register(A, B, C)，顺序即同层内的启动先后
```

约定：

| 约定 | 说明 |
| --- | --- |
| `__init__` 只赋值 | 连接、预热、加载这类动资源的活一律留到 `start()`。装配发生在 lifespan 之前，在 `__init__` 里连资源会让「装配失败」和「启动失败」混成一锅，回滚也会失去意义 |
| 声明与签名对账 | `dependencies` 与构造器参数必须一一对应，容器装配时校验，不一致直接报 `ServiceContractError` |
| 依赖写具体类型 | 写**被注册的那个具体类型**，不能拿抽象基类占位，`get()` 按精确类型查找 |
| `name` 只是标签 | 缺省取类名，不参与依赖解析，也不要求全局唯一，仅用于日志与健康检查展示 |
| 自带日志 | `self.log` 的消息已带 `[<label>]` 前缀（服务名不再重复进结构化字段），仍可继续 `bind` / `prefix` |
| 统一错误格式 | 报错用 `self.log_error("消息", exc)`，自动拆出「错误类型 / 错误消息」两个结构化字段，便于采集端按类型聚合 |
| 配置节点注入 | 需要局部配置时，把配置类定义在 `core/config/settings.py` 并挂成 `Settings` 的**顶层字段**，构造器声明该类型即可（如 `config: CacheSettings`），容器按类型注入。字段允许写成 `X \| None`（PEP 604），但只有当前值不是 `None` 时才建索引——可选字段为 `None` 时容器无法注入，装配期报 `ServiceContractError` |
| 整份配置注入 | 需要全局视野时在构造器声明 `settings: Settings`，容器注入应用持有的那份实例 |
| 超时覆盖 | 默认走 `service.start_timeout` / `service.stop_timeout`；单独调整时写 `start_timeout: ClassVar[float \| Unset \| None] = 300.0`（需 `from core.service.base import Unset`），`None` 表示该服务不限制，不写即跟随全局 |
| 判「在跑」用 `running` | `Service.running` 是 `state is ServiceState.RUNNING` 的统一出口，调用点不要再散写状态比较，改判定规则时才不必全仓搜 |
| 健康检查可附带信息 | `health()` 返回的 `HealthStatus.extra` 会作为字段带进启动时的「服务健康」日志（内置服务用它报身份：clock 报 `时区`，embedding 报 `模型` / `端点`，llm 报 `对话模型` / `对话端点` / `多模态模型` / `多模态端点`）。与保留名同名的一律让位：`name` / `healthy` / `state` / `detail`，以及日志侧的 `服务` / `健康` / `状态` / `详情` |

启停语义：`start` / `stop` 需幂等（重复调用不应报错），并且 **`stop()` 必须能安全作用在「从未成功启动过」的服务上**——启动失败者同样会被回滚调用。启动按依赖**分层**：同层并发、层间串行；单个服务超时或抛错都判该服务失败，并逆序回滚本次动过的服务（**含失败者**：`start()` 可能已经申请了部分资源，`stop()` 是它唯一的回收入口；失败者的状态保持 `FAILED`，清理成功不等于它启动成功过；**已 RUNNING、被本次跳过启动的服务也在回滚名单里**——整体启动失败意味着进程即将退出，而那时 `stop_all` 不会被调用），随后抛出 `ServiceStartError`；**被取消时（外层 `asyncio.timeout`、`task.cancel()`）走同一条回滚路径**——`lifespan()` 的 `__aenter__` 抛错时 `__aexit__` 不会执行、`stop_all` 不会被调用，所以回滚必须在 `start_all` 内部完成，已启动的服务不会留在 `RUNNING`。关闭按启动的逆序**串行**执行，单个服务超时或出错只记日志（状态置 `FAILED`），不影响其余服务停下。

超时靠「定时取消 + 标志位」实现（不用 `asyncio.timeout`，好与业务自己抛的 `TimeoutError` 区分开），因此服务的 `start` / `stop` **不要吞掉 `CancelledError`**（写 `except Exception` 是安全的，它不会捕获 `CancelledError`）。框架侧超时抛 `HookTimeoutError`（`TimeoutError` 的**子类**），它会作为 `ServiceStartError.cause` 出现；业务自己抛的 `TimeoutError` 原样保留，记为「启动失败 / 关闭异常」。

装配期的错误都是 `ServiceError` 的子类，只会让启动失败、进程退出（fail fast）。调用 `get()` 查一个未注册的类型属于编程错误，会抛 `ServiceNotRegisteredError`。

| 类型 | 触发条件 |
| --- | --- |
| `ServiceContractError` | 非 `Service` 子类、重复注册（含同一次批量注册内重复）、装配后注册、构造器签名不可解释、声明与签名不一致 |
| `ServiceNotRegisteredError` | `get()` 查的类型未注册 |
| `MissingDependencyError` | `dependencies` 里的类型没注册 |
| `CircularDependencyError` | 依赖成环 |
| `ServiceStartError` | 某个服务 `start()` 抛错（携带 `label` 与 `cause`；超时场景的 `cause` 是 `HookTimeoutError`） |
