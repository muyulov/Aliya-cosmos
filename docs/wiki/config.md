# 配置

配置以 `data/config/cosmos.yaml` 为唯一来源（路径相对当前工作目录）。该文件可安全提交，因为它不含任何明文密钥。要调整配置，改这个文件即可。

```yaml
app:
  env: dev                   # 部署时可改成 ${APP_ENV:dev}，由环境变量顶掉
log:
  level: DEBUG               # 仓库骨架用 DEBUG；代码内置默认是 INFO
  json: false
  layout: line               # line（默认，字段内联成一行）/ tree（字段成树）
  color: auto                # auto（仅终端）/ always / never
```

## 占位符

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

## 配置项

下表取 `data/config/cosmos.yaml` 的**当前值**（即进程实际生效的配置）；没有列出的字段回落到 `core/config/settings.py` 的内置默认。两者语义不同：删掉 YAML 里的某项后，取的是代码内置默认，而不是下表的值（例如 `log.level` 的代码默认是 `INFO`，仓库骨架覆盖成了 `DEBUG`）。

| YAML 路径 | 仓库当前值 | 说明 |
| --- | --- | --- |
| `app.app_name` | `Aliya-cosmos` | 应用名 |
| `app.env` | `dev` | 环境，只能是 dev / test / prod |
| `app.debug` | `true` | 调试开关 |
| `log.level` | `DEBUG` | 日志级别；代码内置默认 `INFO` |
| `log.json` | `false` | 是否输出 JSON 日志 |
| `log.layout` | `line` | 输出布局：`line`（字段内联成一行，便于 grep 与按行采集）/ `tree`（字段缩进成树） |
| `log.color` | `auto` | 控制台着色：`auto`（仅终端）/ `always` / `never`；日志文件永远不带颜色 |
| `log.source` | `true` | 是否在每条日志里带上调用点 `文件名:行号` |
| `log.dir` | `logs` | 日志目录 |
| `log.file_name` | `cosmos-main-{time:%Y%m%d-%H%M%S}.log` | 主日志文件名，支持 loguru 时间模板（装配时求值，故每次启动一个独立文件） |
| `log.error_file_name` | `cosmos-error-{time:%Y%m%d-%H%M%S}.log` | 错误日志文件名，同上。两个名字的前缀不能互为包含关系 |
| `log.rotation` | `00:00` | 轮转阈值（默认按天），写进 YAML 时必须加引号 |
| `log.retention` | `7 days` | 保留时长 |
| `log.compression` | `zip` | 归档压缩方式 |
| `service.start_timeout` | `30` | 单个服务 `start()` 的超时秒数，必须为正数；写 `null` 表示不限制 |
| `service.stop_timeout` | `30` | 单个服务 `stop()` 的超时秒数，必须为正数；同上 |
| `clock.tz` | `Asia/Shanghai` | 时钟服务的时区；代码内置默认 `UTC` |

`embedding` 是独立的向量化端点，与 llm 分开配（DeepSeek 没有 embedding 端点，两家通常不是同一个服务）：

| 字段 | 仓库当前值 | 说明 |
| --- | --- | --- |
| `base_url` | `http://127.0.0.1:9080/v1` | OpenAI 兼容端点地址（仓库骨架指向本地 llama.cpp）；代码内置默认是阿里云 compatible-mode |
| `api_key` | `local` | YAML 里写 `${EMBEDDING_API_KEY:local}`；本地端点不校验鉴权，只是需要一个非空占位。留空则该端点不可用（只警告，不影响启动） |
| `model` | `Qwen3-Embedding-4B-Q4_K_M` | 向量化模型；代码内置默认是 `text-embedding-v3` |
| `timeout` | `60` | 单次请求超时秒数，必须为正数 |
| `retries` | `2` | SDK 重试次数，`0` 关闭 |
| `batch_size` | `10` | 单次请求最多几条文本；阿里 v3 上限就是 10（v1/v2 为 25、OpenAI 为 2048），超了会 400 |
| `dimensions` | `null` | `null` 用模型原始维度；设值则请求截断，做不到的模型会报错 |

`llm.chat` / `llm.vision` 是两个端点，字段完全相同（可指向同一服务，也可分开接不同供应商）：

| 端点字段 | 仓库当前值 | 说明 |
| --- | --- | --- |
| `base_url` | `https://api.deepseek.com/v1` | 端点地址，chat 与 vision 相同；换供应商时改这里。代码内置默认是 OpenAI 官方端点 |
| `api_key` | 空 | 密钥，YAML 里写 `${DEEPSEEK_API_KEY:}` 由 `.env` 提供；留空则该端点不可用（只警告，不影响启动） |
| `model` | `deepseek-flash` | 该端点的模型；走 vision 端点需显式传 `endpoint="vision"`。代码内置默认是 `gpt-4o-mini` |
| `timeout` | `60` | 单次请求超时秒数，必须为正数 |
| `retries` | `2` | SDK 重试次数，`0` 关闭 |
| `structured_mode` | `json_object` | 结构化输出模式。仓库骨架用 `json_object`（DeepSeek 不支持 `json_schema`），该模式要求提示词里出现 json 字样；换成支持 `json_schema` 的端点时可改回 |
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
| `retries` | `2` | 429 / 502 / 503 / 504 / 529 的退避重试次数，`0` 关闭 |
| `max_connections` | `null` | 该端点连接池上限；`null` 用 httpx2 默认。并发 `predict_many` 时用它压住对端点的连接数 |

## 失败行为

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
