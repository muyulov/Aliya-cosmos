# LLM 层（OpenAI）设计

日期：2026-09-26
状态：已实施（见 `2026-09-26-llm-layer-plan.md`）
基线：`main`（service / logger / config 三层，web 层已移除）

**实施补记（两处与本文的偏差）**：

1. 「不传参数」的哨兵用 `omit` 而非 `NOT_GIVEN`。SDK 3.19 的 `create()` 签名是
   `temperature: float | Omit | None`，`NotGiven` 不在其中（basedpyright 报
   `reportArgumentType`）；而 `Omit` 与 `NotGiven` 一样会在请求体转换时被剔除
   （`is_given()` 同时排除两者），语义与类型都对得上。显式 `None` 会被序列化成
   JSON `null` 这一点不变（已用 `async_maybe_transform` 实测确认）。
2. `chat_tools` 只收 function 类型的 tool_call。`message.tool_calls` 在 3.x 是
   function 与 custom 两种调用的联合，custom 没有 `function` 结构，直接取属性
   `reportAttributeAccessIssue`。
3. **配置改成 `llm.chat` / `llm.vision` 双端点、embedding 整体移除**（2026-09-26
   用户要求）。本文决策 4 的「单端点 + 三个模型槽位」作废：端点配置抽成
   `LLMEndpointSettings`（`base_url` / `api_key` / `model` / `timeout` / `retries` /
   `structured_mode` / `temperature` / `max_tokens`），`LLMSettings` 持有 `chat` 与
   `vision` 两份，服务各建一个客户端。路由规则：默认走 chat，传
   `model=svc.vision_model` 且 vision 已启用时走 vision；vision 未启用时不接管自己的
   模型（两头 model 常常同名，否则 chat 会被 vision 的缺失密钥拖垮）。`embed()`
   与 `embed_model` 一并删除。

## 一、目标与范围

新增 `core/llm/` 层：把「调大模型」这件事收成一个服务，业务代码不直接接触 SDK。

覆盖六项能力：**对话补全**、**流式输出**、**结构化输出**、**工具调用（单轮）**、**embedding**、**多模态（图片输入）**。

非目标（本次不做）：

- **多供应商抽象**：不做 Provider 协议 / 适配器。靠 `base_url` 覆盖 OpenAI 兼容端点（DeepSeek、Moonshot、vLLM、Ollama），非兼容协议（Anthropic 原生）不在范围内。
- **Responses API**：SDK 3.x 的主推接口，但第三方兼容端点普遍只实现 Chat Completions，故不用。
- **自动多轮工具循环**：只做单轮，返回 `tool_calls`，回传与循环由调用方写。
- embedding 分批、重试之外的容错（熔断 / 限流 / 配额）、Token 计数、Realtime / 语音、微调。

## 二、决策记录

| # | 议题 | 结论 | 被否决的方案与原因 |
| --- | --- | --- | --- |
| 1 | 客户端形态 | 官方 `openai` SDK（3.x）的 `AsyncOpenAI`，`base_url` 可配 | 自研领域模型 + 适配层（六项能力下消息结构映射面太大，收益只在换非兼容协议时才兑现）；httpx 直调（SSE 解析、错误分类、重试全自写，且要把刚删掉的 httpx 加回来） |
| 2 | 用哪套 API | **Chat Completions**（`client.chat.completions.create`） | Responses API（SDK 主推，但兼容端点未必实现，会直接失去 DeepSeek 等） |
| 3 | 配置类放哪 | `core/config/settings.py` 新增 `LLMSettings`，挂成 `Settings.llm` 顶层字段，容器按类型注入 | 放在 `core/llm/settings.py`（会造成 `config → llm` 反向依赖） |
| 4 | 配置结构 | 单端点 + 三个模型槽位（`chat_model` / `embed_model` / `vision_model`，后两个留空则复用 `chat_model`） | 多端点预设（配置与注入都复杂化，当前没有同时连两家的需求）；只放连接参数（model 散落到业务代码） |
| 5 | 工具调用深度 | 单轮：`chat_tools` 返回 `AssistantReply`（content + tool_calls） | 内部自动多轮循环（并发、错误、截断策略会变成隐式行为） |
| 6 | 缺 `api_key` 时 | `start()` 记警告、不建客户端；`health()` 报 unhealthy；调用时抛 `LLMConfigError` | 启动期 fail fast（脚手架默认起不来）；默认不注册该服务（多一步手动装配） |
| 7 | 重试与超时 | 全部交给 SDK：`AsyncOpenAI(max_retries=…, timeout=…)`，不再包一层 | 自写重试（与 SDK 重试叠成乘积，且要重复实现退避与可重试判定） |
| 8 | `health()` 是否探测 | 不探测，只报状态 / 未配 key | 发轻量请求（健康检查不该有副作用、不该花钱、不该受网络抖动影响） |
| 9 | 结构化输出模式 | 配置 `structured_mode`：`json_schema`（默认）/ `json_object` | 只支持 `json_schema`（DeepSeek 等兼容端点不支持，一换端点就全崩） |
| 10 | 日志是否记消息正文 | 不记。只记模型 / 端点 / 耗时 / token 用量 | 记录正文（与「不做脱敏，由调用方传值前处理」的既有约定一致，正文该不该记由调用方自己决定并自己打） |
| 11 | `registry` 是否留在 `core.service` 门面 | **摘出去**：`core/service/__init__.py` 不再导出 `build_manager` / `default_manager`，装配入口一律 `from core.service.registry import …` | 留在门面（会形成 `service.__init__ → registry → llm.service → service.base` 的模块环，先 import `core.llm` 时撞部分初始化而 ImportError）。仓库内无调用方从门面取这两个名字，摘除零成本 |
| 12 | `embed_model` 留空时 | `embed()` 抛 `LLMConfigError` | 回退 `chat_model`（chat 模型几乎都不提供 embedding 端点，DeepSeek 甚至没有，回退必错且错得晚） |

## 三、配置层改动

`core/config/settings.py` 新增（与 `LogSettings` 同级）：

```python
class LLMSettings(BaseModel):
    """LLM 层配置。"""

    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""          # YAML 里写 ${OPENAI_API_KEY:}，留空表示未启用
    chat_model: str = "gpt-4o-mini"
    embed_model: str = ""      # 留空则 embed() 抛 LLMConfigError
    vision_model: str = ""     # 留空复用 chat_model
    timeout: float = 60.0      # 单次请求超时（秒）
    retries: int = 2           # SDK 重试次数，0 关闭
    structured_mode: Literal["json_schema", "json_object"] = "json_schema"
    temperature: float | None = None   # None = 不传该参数（转成 SDK 的 NOT_GIVEN）
    max_tokens: int | None = None      # 同上
```

`Settings` 增加顶层字段 `llm: LLMSettings = LLMSettings()`；`core/config/__init__.py` 导出 `LLMSettings`。

`data/config/app.yaml` 补一段（值等于默认，保持"骨架即全貌"）：

```yaml
llm:
  base_url: https://api.openai.com/v1   # 接 DeepSeek：https://api.deepseek.com/v1
  api_key: ${OPENAI_API_KEY:}           # 冒号后为空：不配时取空串，由服务警告而非报错
  chat_model: gpt-4o-mini
  embed_model: ""                        # 留空 = 未启用 embedding，调用即报错
  vision_model: ""                       # 留空 = 复用 chat_model
  timeout: 60
  retries: 2
  structured_mode: json_schema           # 兼容端点不支持时改 json_object
  temperature: null                      # null = 不传该参数，用服务端默认
  max_tokens: null
```

`${VAR:}` 的空默认值由 `loader._resolve` 支持（`default` 为 `""` 而非 `None`），因此不配 `.env` 时取值空串、不触发 `ConfigError`。

## 四、模块结构

```
core/llm/
  __init__.py    # 对外出口：LLMService、错误树、消息/工具/图片构造器
  client.py      # build_client(cfg) -> AsyncOpenAI：唯一 new 出 SDK 对象的地方
  errors.py      # LLMError 树
  messages.py    # system / user / user_with_images / image_url / image_base64
                 # assistant / tool_result / tool
  service.py     # LLMService：进 DI 容器，持有客户端
```

依赖方向：`llm → (config, logger, service)`。

- `core/llm/service.py` 必须写 **`from core.service.base import Service`（子模块绝对路径）**，不写 `from core.service import Service`：后者会触发父包 `core/service/__init__.py`，一旦门面将来新增依赖就会复现下面的环。
- `core/service/registry.py` 里 `register(LLMService)`：registry 是组合根，import 具体服务类（无论在哪一层）是它的职责。**但前提是把 registry 从包门面里摘出去**（决策 11）：

```
core.service.__init__ → core.service.registry → core.llm.service → core.service.base
                                                                   └→ 触发 core.service.__init__（循环！）
```

`core/service/__init__.py` 当前第 15 行 `from core.service.registry import build_manager, default_manager`，保留它就会成环：入口先 `import core.llm` 时，`core.llm.service` 尚未执行完，`registry` 反向取 `LLMService` 会撞上部分初始化的模块而 `ImportError`；basedpyright 也会报 `reportImportCycles`。

摘除后链路变为 `core.service.__init__ → (base / clock_service / manager)`，不再触碰 registry 与 llm，环消失。仓库内 `core/main.py` 与 `tests/test_service_injection.py` 本来就写 `from core.service.registry import build_manager`，不受影响。

## 五、消息与工具构造器（`messages.py`）

调用方只碰 `core.llm` 的构造器，不 import SDK：

```python
system(text: str) -> ChatCompletionMessageParam
user(text: str) -> ChatCompletionMessageParam
assistant(text: str) -> ChatCompletionMessageParam
tool_result(call_id: str, content: str) -> ChatCompletionMessageParam

image_url(url: str) -> ImagePart
image_base64(data: str, media_type: str = "image/png") -> ImagePart
user_with_images(text: str, *images: ImagePart) -> ChatCompletionMessageParam

tool(name: str, description: str, schema: type[BaseModel] | Mapping[str, object])
    -> ChatCompletionToolParam
```

- `system()` 生成 `role="system"`。OpenAI 新模型推荐 `developer`，但第三方兼容端点普遍只认 `system`，统一用 `system`。
- `tool()` 接受 pydantic 模型（内部取 `model_json_schema()`）或手写 JSON Schema 字典。
- 返回值类型是 SDK 的 TypedDict：构造器只是免得调用方 import SDK，不做二次封装。**每个构造器返回具体的 TypedDict**（如 `ChatCompletionUserMessageParam`）而不是联合体 `ChatCompletionMessageParam`——后者是 Union，basedpyright 对构造出来的字面量校验更松，容易放过拼错的键。

## 六、服务接口（`service.py`）

```python
class LLMService(Service):
    name: ClassVar[str] = "llm"

    def __init__(self, config: LLMSettings) -> None: ...

    async def chat(self, messages, *, model=None, temperature=None,
                   max_tokens=None) -> str
    async def stream(self, messages, *, model=None, temperature=None,
                     max_tokens=None) -> AsyncIterator[str]
    async def chat_structured(self, messages, schema: type[T], *, model=None,
                              temperature=None) -> T
    async def chat_tools(self, messages, tools, *, model=None,
                         temperature=None) -> AssistantReply
    async def embed(self, texts: Sequence[str], *, model=None) -> list[list[float]]
```

要点：

- **模型解析**：`chat` / `stream` / `chat_tools` / `chat_structured` 用 `chat_model`；`embed` 用 `embed_model`（留空 → `LLMConfigError`）。多模态**不新增方法**，由调用方传 `model=svc.vision_model`。因此服务暴露三个只读属性 `chat_model` / `embed_model` / `vision_model`（返回已解析回退后的值）。
- **`temperature` / `max_tokens` 传给 SDK 前必须把 `None` 换成 `NOT_GIVEN`**：SDK 的 `_transform_typeddict` 只剔除 `NotGiven`，显式 `None` 会被原样序列化成 JSON `null`（端点多半 400 或当 0 处理）。配置层保留 `None` 表达"未设置"，转换只发生在调用边界（`from openai import NOT_GIVEN`）。
- **`chat` 返回 `str`**：`choices[0].message.content` 为 `None`（如被工具调用占用）时抛 `LLMResponseError`。
- **`stream` 是 async generator**：逐 `delta.content` yield 非空片段；不自动拼接（调用方要整串自己 `join`）。
- **`chat_structured`**：`response_format` 按 `structured_mode` 构造，`json_schema` 模式传 `{"name": schema.__name__, "schema": schema.model_json_schema()}`，然后用 `schema.model_validate_json(content)`；`ValidationError` → `LLMSchemaError`。**不传 `strict`**（strict 要求所有字段 required，默认关掉更不容易踩坑）。`model_json_schema()` 返回 `dict[str, Any]`，必须用 `cast("dict[str, object]", …)` 收敛后再塞进请求体，否则 `Any` 会顺着 SDK 参数渗进类型检查。
- **`chat_tools` 返回 `AssistantReply`**（dataclass）：`content: str | None`、`tool_calls: tuple[ToolCall, ...]`；`ToolCall` 含 `id` / `name` / `arguments`（原始 JSON 字符串，解析交给调用方）。
- **`embed`** 传 `input=list(texts)`，按返回顺序输出 `list[list[float]]`；不内置分批。
- 客户端未创建时（即未配 `api_key`）所有方法抛 `LLMConfigError`，消息里写清"未配置 api_key"，无 cause。

### 生命周期

```python
async def start(self) -> None:
    if self.state is ServiceState.RUNNING:
        return
    if not self._config.api_key:
        self.log.warning("未配置 api_key，LLM 功能不可用", 端点=self._config.base_url)
        return
    self._client = build_client(self._config)

async def stop(self) -> None:
    if self._client is None:
        return
    await self._client.close()
    self._client = None          # 幂等：重复 stop 不会二次 close
```

`stop()` 必须能安全作用在"从未建过客户端"的实例上（容器回滚会调它）。

### 健康检查

```python
async def health(self) -> HealthStatus:
    # healthy = self._client is not None
    # detail = "" / "未配置 api_key，LLM 功能不可用"
```

不发任何网络请求。

## 七、客户端工厂（`client.py`）

```python
def build_client(config: LLMSettings) -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=config.api_key,
        base_url=config.base_url,
        timeout=config.timeout,
        max_retries=config.retries,
    )
```

SDK 默认 `timeout` 是 10 分钟、`max_retries` 是 2，这里都显式给值。客户端对象只在 `start()` 里由这个工厂创建——这是测试注入替身的唯一接缝。

## 八、错误处理与日志

`errors.py` 的错误树（全部继承 `LLMError`，原始 SDK 异常挂在 `__cause__`）：

| 场景 | 异常 |
| --- | --- |
| 未配 `api_key` / 客户端未创建 | `LLMConfigError` |
| `APIStatusError`（4xx / 5xx） | `LLMRequestError`（带 `status_code` / `endpoint` / `model` / `request_id`） |
| `APITimeoutError` | `LLMTimeoutError` |
| `APIConnectionError` | `LLMConnectionError` |
| 结构化输出校验失败 | `LLMSchemaError` |
| 返回体缺内容（`content` 为 `None`、choices 为空） | `LLMResponseError` |

映射收在一个模块级函数里（如 `_wrap_errors`），五个公开方法统一 `try/except openai.APIError` 后转换。`request_id` 从 `exc.request_id` 取，便于对账。

**`except` 顺序是硬要求**：`APITimeoutError` 是 `APIConnectionError` 的**子类**（SDK 源码：`class APITimeoutError(APIConnectionError)`），因此必须先捕 `APITimeoutError` 再捕 `APIConnectionError`，否则超时会被全部误判成连接错误。同理 `APIStatusError` 与 `APIConnectionError` 是兄弟类，互不包含，顺序随意但都排在 `APIError` 兜底之前。

日志：每次调用成功打一条 `self.log.info("LLM 调用完成", 模型=…, 端点=…, 耗时=f"{ms}ms", 输入token=…, 输出token=…)`；失败走 `self.log_error`（自动带 `错误=类型: 消息`）。`usage` 可能为 `None`，取不到就省略 token 字段。

## 九、改动清单

| 文件 | 动作 |
| --- | --- |
| `pyproject.toml` | `dependencies` 加 `openai>=3.0` |
| `core/config/settings.py` | 新增 `LLMSettings`，`Settings` 加 `llm` 字段 |
| `core/config/__init__.py` | 导出 `LLMSettings` |
| `core/llm/__init__.py` | 新增，导出 `LLMService`、错误树、构造器 |
| `core/llm/client.py` | 新增，`build_client` |
| `core/llm/errors.py` | 新增，`LLMError` 树 |
| `core/llm/messages.py` | 新增，消息 / 图片 / 工具构造器 |
| `core/llm/service.py` | 新增，`LLMService` |
| `core/service/registry.py` | `register(LLMService)` |
| `core/service/__init__.py` | 移除 `build_manager` / `default_manager` 的导入与 `__all__` 两项（决策 11，断开导入环） |
| `data/config/app.yaml` | 补 `llm:` 段 |
| `tests/test_llm_service.py` | 新增 |
| `README.md` | 配置项表格补 llm 段；新增「LLM 层」章节 |

## 十、测试策略（`tests/test_llm_service.py`）

容器只认配置节点，`__init__` 不能塞 `client` 参数，因此**测试不走 `start()`**，而是把最小鸭子类型假客户端塞进 `service._client`：

```python
service._client = cast("AsyncOpenAI", FakeClient(...))  # pyright: ignore[reportPrivateUsage]
```

两处都不能省：`cast` 是因为 `FakeClient` 与 `AsyncOpenAI` 无继承关系（鸭子类型，注解挡不住）；`reportPrivateUsage` 是因为容器无法注入客户端，替身只能白盒注入。不起 mock server、不 mock HTTP。

假客户端实现 `chat.completions.create`、`embeddings.create`、`close()`，按预设返回 SDK 风格的假对象。

| 用例 | 断言 |
| --- | --- |
| 未配 key 时 start 不建客户端 | `start()` 不抛错、`state is RUNNING`、`health().healthy is False`、`detail` 含"未配置" |
| 调用时才报错 | 未配 key 调 `chat()` → `LLMConfigError` |
| `embed_model` 留空 | → `embed()` 抛 `LLMConfigError`（不再回退 chat_model，决策 12） |
| `vision_model` 回退 | `vision_model=""` → 只读属性 `svc.vision_model` 等于 `chat_model` |
| `temperature` 未设置 | 配置 `temperature=None` → 假客户端收到的入参是 `NOT_GIVEN`（不是 `None`，否则会序列化成 JSON null） |
| `chat` 正常返回 | 返回 `str`，等于假对象的 `content` |
| `content` 为 `None` | → `LLMResponseError` |
| 流式拼接 | 假客户端 yield 三个片段 → `async for` 依次拿到，空片段被跳过 |
| 结构化输出成功 | `chat_structured(..., Item)` 返回 `Item` 实例 |
| 结构化输出校验失败 | 返回不合 schema 的 JSON → `LLMSchemaError` |
| `json_object` 模式 | `structured_mode="json_object"` 时 `response_format` 入参为 `{"type": "json_object"}` |
| 工具调用单轮 | 返回 `AssistantReply`，`tool_calls` 的 `id` / `name` / `arguments` 正确；不自动发起第二轮（假客户端记录调用次数 == 1） |
| 状态码映射 | 假客户端抛 `APIStatusError` → `LLMRequestError`，`status_code` / `request_id` 正确 |
| 超时 / 连接错映射 | `APITimeoutError` → `LLMTimeoutError`；`APIConnectionError` → `LLMConnectionError` |
| embedding 顺序 | 三條输入 → 返回三段向量，顺序与输入一致 |
| `stop` 幂等 | 连续 `stop()` 两次不抛错，客户端只被 `close()` 一次 |
| 多模态消息结构 | `user_with_images("描述", image_url(u))` 产出的 content 是 `[{type: text}, {type: image_url}]` |

## 十一、破坏性变更与风险

**破坏性变更**：仅一条——`core/service/__init__.py` 不再导出 `build_manager` / `default_manager`（决策 11）。仓库内无人从门面取这两个名字（`core/main.py` 与测试都直接 import `core.service.registry`），实际影响为零；外部使用者改成 `from core.service.registry import build_manager` 即可。

其余均为新增：新增层、新增配置段，既有行为不变（`Settings` 是 `extra="ignore"`，旧代码读到 `llm:` 键不报错）。

**风险与已规避项**：

- **`uv run python -m core.main` 的既有冒烟不受影响**：未配 key 时 `LLMService` 只警告，健康检查会多打一条 `llm` 的 unhealthy 记录——这是预期输出，不是回归。
- **SDK 3.x 的 HTTP 客户端是 HTTPX2**：不直接依赖 httpx 名字，但会作为传递依赖装进来。这是 SDK 的选择，不是我们引入的。
- **结构化输出的 `json_schema` 并非所有端点都支持**：已有 `structured_mode` 开关兜底；`json_object` 模式要求提示词里出现"json"字样，属调用方责任，写进 README。
- **流式不重试**：SDK 明确"流已消费则不重试"，因此 `stream()` 中途断流会直接抛错，调用方需要自己决定是否重放。
- **token 用量字段可能缺失**：兼容端点未必返回 `usage`，日志相应省略，不因取不到而报错。
- **`temperature` / `max_tokens` 默认 `None`**：在调用边界转成 `NOT_GIVEN`（等价不传），让服务端决定默认值；显式配值则覆盖。
- **新层不在 coverage omit 里**：当前只 omit `core/main.py`，`core/llm/*` 需要真实测试覆盖，否则覆盖率会明显下滑。
- **import 顺序**：决策 11 断环后，`import core.llm` 与 `import core.service` 两种入口都应可用；验收时两种顺序各跑一次，防止环以另一种顺序复现。

## 十二、验收标准

1. `uv run pytest` 全绿（既有测试无回归 + 新增 `tests/test_llm_service.py`）。
2. `uv run ruff check .` 与 `uv run ruff format --check .` 无输出。
3. `uvx basedpyright core tests` 报 `0 errors, 0 warnings, 0 notes`。
4. `uv run python -m core.main` 能启动并优雅关闭；未配 key 时打印 `llm` 服务的 unhealthy 健康检查（含"未配置"说明），不报错退出。
5. 两种 import 顺序均可：`python -c "import core.llm"` 与 `python -c "import core.service"` 都不抛 ImportError（导入环回归）。
6. 配了 `.env` 的 `OPENAI_API_KEY` 后，`llm` 健康检查为 healthy（手工冒烟，不写进自动化测试）。
7. README 配置项表格含 `llm.*` 各字段；新增「LLM 层」章节含六项能力的调用示例与"不记正文""不自动多轮"两条约定。
