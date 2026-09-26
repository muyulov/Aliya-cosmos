# 向量层（文本转向量）设计

日期：2026-09-26
状态：设计定稿，待实施
基线：`main`（service / logger / config / llm 四层）

## 一、目标与范围

新增 `core/embedding/` 层：把「文本转向量」收成一个服务，业务代码不直接接触 SDK。
定位是**本体提供的通用能力**，先在本体落地，归忆（`memory/`）后续自行写适配层对接。

覆盖：**单条向量化**、**批量向量化**（按 `batch_size` 切分，一次请求带多条）。

非目标（本次不做）：

- **向量存储与检索**：不引入向量库、不做相似度检索 / 聚类 / 重排。这些是调用方的事。
- **相似度计算工具**：`cosine` / 归一化之类不需要模型与配置，不塞进服务。
- **本地模型实现**：只留出协议接缝，ONNX 实现等真正要用时再写。
- **多供应商抽象、自研重试、健康检查探测**：与 llm 层同取舍（见决策记录）。
- 归一化：**原样透传**模型输出，不减不除（决策 7）。

## 二、决策记录

| # | 议题 | 结论 | 被否决的方案与原因 |
| --- | --- | --- | --- |
| 1 | 定位 | 本体提供通用文本向量化能力，归忆后续适配 | 直接给归忆做（归忆契约规定不 import 本体，会被迫改它的注入方式）；只给本体内部用（能力面被过度收窄） |
| 2 | 向量来源 | 远程 OpenAI 兼容端点（复用 `openai` SDK 的 `embeddings.create`） | 本地 ONNX（`onnxruntime` + `tokenizers` + 模型文件管理，本次无此需求）；纯 HTTP 直调（把刚删掉的 `httpx` 又加回来） |
| 3 | 内核可替换性 | 抽 `Encoder` 协议，默认实现 `RemoteEncoder`；接入本地实现时**子类覆盖 `_make_encoder()`** | 现在加 `backend: Literal["remote","local"]` 配置项（本地实现不存在，属提前预留）；构造注入（DI 契约不允许，见下） |
| 4 | 能力面 | `embed(text)` + `embed_many(texts)` | 只要单条（调用方自己循环，一条一次请求）；再加相似度工具（混淆服务职责） |
| 5 | 模块形态 | 独立包 + `EmbeddingService(Service)` 注册进 registry，`build_manager()` 的注册顺序为 clock → embedding → llm | 纯对象不进 service 层（不入 registry、无健康检查）；写成 Service 但不注册（默认启动就少一块能力） |
| 6 | 配置 | `Settings.embedding` 独立端点，一套 `base_url` / `api_key` / `model` / `timeout` / `retries` / `batch_size` / `dimensions` | 复用 `llm.chat` 的连接信息（两个能力被强绑到同一供应商，而 DeepSeek 没有 embedding 端点）；抽公共基类（要改已稳定的 llm 层与测试） |
| 7 | 输出语义 | 原样透传，不归一化；`dimensions` 可选（配置项 + 调用覆盖） | 一律 L2 归一化（丢模长信息，且归一化成为隐性行为）；完全不支持 `dimensions`（阿里 v3 / OpenAI 都支持截断，配不上很可惜） |
| 8 | 批量策略 | 按 `batch_size` 切片、逐批 `await`（串行） | 有限并发（多一个 `concurrency` 配置项，并发日志与部分失败语义都要定）；不切分（把上限责任推给调用方，必然有人踩 400） |
| 9 | 批量失败 | 任一批失败 → 整次调用抛错，已完成的批作废 | 部分成功（返回值与入参对不上号，调用方无法定位） |
| 10 | 缺 `api_key` | `start()` 只警告不建 encoder；`health()` unhealthy；调用时抛 `EmbeddingConfigError` | 启动期 fail fast（脚手架默认起不来） |
| 11 | `health()` 是否探测 | 不探测，只看 `_encoder is not None` | 发一次真实请求（有副作用、花钱、受网络抖动影响） |
| 12 | 空输入 | `embed_many([])` 返回 `[]` 且**不发请求**；空串 / 全空白串在本地抛 `EmbeddingInputError` | 空串透传（换不来任何信息，只是白花一次必然 400 的请求） |
| 13 | 错误树 | 照抄 llm 形状，额外加 `EmbeddingInputError`；不继承 `ServiceError` | 直接抛基类 `EmbeddingError`（调用方无法区分「入参错」与「端点错」）；沉 SDK 异常（破坏「业务不碰 SDK」约定） |
| 14 | 日志 | 每批一条；批数 > 1 时再补一条汇总，**都走 `self.log`** | 汇总走模块级 `log`（会为了带服务名而撕开「单服务日志走 `service.log`」的口子） |
| 15 | `batch_size` 默认值 | **10** | 16（阿里 `text-embedding-v3` 列表输入上限就是 10 条，默认值直接踩线，一调就 400） |

### 为什么内核不能从构造器注入

`ServiceManager._validate_contract()`（`core/service/manager.py:478`）会逐个遍历 `__init__` 形参，
注解必须是 `Settings`、`Service` 子类或 `Settings` 顶层配置节点，否则直接抛
`ServiceContractError("… 容器只支持 Service 与配置节点")`。`Encoder` 三者都不是，因此
`__init__(self, config, encoder=None)` 会让**整个容器装配失败**。

结论：`__init__` 只吃 `EmbeddingSettings`；换内核算「覆盖受保护工厂方法」，测试替身走白盒注入 `_encoder`
（与 llm 层测试注入 `_chat` / `_vision` 同法）。

## 三、配置层改动

`core/config/settings.py` 新增（与 `ClockSettings` 同级）：

```python
class EmbeddingSettings(BaseModel):
    """文本向量化端点配置。字段先例与 LLMEndpointSettings 一致。"""

    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    api_key: str = ""  # YAML 里写 ${DASHSCOPE_API_KEY:}，留空表示该端点未启用
    model: str = "text-embedding-v3"
    timeout: float = Field(default=60.0, gt=0)  # 单次请求超时（秒）
    retries: int = Field(default=2, ge=0)  # SDK 重试次数，0 关闭
    batch_size: int = Field(default=10, gt=0)  # 单次请求最多几条文本
    dimensions: int | None = None  # None = 用模型原始维度；设值则请求截断
```

`Settings` 增加顶层字段 `embedding: EmbeddingSettings = EmbeddingSettings()`；
`core/config/__init__.py` 导出 `EmbeddingSettings`。

`data/config/app.yaml` 补一段（风格与 `llm:` 段一致，带说明注释）：

```yaml
# 文本向量化端点。与 llm 分开配：DeepSeek 没有 embedding 端点，两家通常不是同一个服务。
# 没配 api_key 时只警告、健康检查 unhealthy，进程照常启动（不 fail fast）。
embedding:
  base_url: https://dashscope.aliyuncs.com/compatible-mode/v1 # OpenAI 兼容端点
  api_key: ${DASHSCOPE_API_KEY:} # 冒号后为空：.env 里没配时取空串，该端点不可用
  model: text-embedding-v3
  timeout: 60 # 单次请求超时（秒），必须为正数
  retries: 2 # SDK 重试次数，0 关闭
  batch_size: 10 # 单次请求最多几条文本。阿里 v3 上限 10（v1/v2 为 25，OpenAI 为 2048），超了会 400
  dimensions: null # null = 用模型原始维度（v3 默认 1024）；设值则请求截断，做不到的模型会报错
```

**`Settings` 与子模型都是 `extra="ignore"`**：段名写错或缩进错不会报错、只会静默失效，
改完必须实测 `load_settings()`（llm 层已踩过一次）。

## 四、模块结构

```
core/embedding/
  __init__.py    # 对外出口：EmbeddingService、Encoder、RemoteEncoder、错误树
  encoder.py     # Encoder 协议 + RemoteEncoder + build_encoder()：唯一 new 出 SDK 客户端的地方
  errors.py      # EmbeddingError 树
  service.py     # EmbeddingService：进 DI 容器，持有 encoder
```

依赖方向：`embedding → (config, logger, service)`，子模块引用基类必须写
`from core.service.base import Service`（子模块绝对路径），不能走包门面。

无导入环：`core/service/__init__.py` 已在 llm 层摘掉 registry 导出，链路是
`core.service.__init__ → (base / clock_service / manager)`，不再触碰 registry 与本层。

## 五、内核协议（`encoder.py`）

协议出口带结构而不是纯向量：纯向量下调用方拿不到 `index` 与 `usage`，归位只能塞进内核，
而乱序恰恰是端点的行为，不该由可替换的内核替服务层兜（见实施补记第 1 条）。

```python
@dataclass(frozen=True, slots=True)
class EncodedVector:
    index: int
    vector: list[float]


@dataclass(frozen=True, slots=True)
class EncodeResult:
    items: tuple[EncodedVector, ...]
    prompt_tokens: int | None = None


class Encoder(Protocol):
    async def encode(
        self, texts: Sequence[str], *, dimensions: int | None = None
    ) -> EncodeResult: ...

    async def aclose(self) -> None: ...
```

四条约定（写进 docstring，实现者必须遵守）：

1. **一次调用 = 一次请求一批文本**，`items` 条数必须与入参相等。
2. **归位责任在调用方**：每条向量带 `index`（入参位置），内核不得自行重排或丢弃。
3. **`dimensions` 非 None 时必须兑现**：拿不到该维度就抛错，**禁止静默忽略**。
   本地 bge 这类没有截断能力的实现，遇到该参数应当显式报错而不是返回原始维度——
   否则调用方以为拿到 512 维、实际是 1024 维，错得很晚。
4. `aclose()` 是资源回收入口；没有资源的本地实现写空实现即可（协议化的小代价）。

`prompt_tokens` 为 None 表示端点没返回用量：`CreateEmbeddingResponse.usage` 在 SDK 类型里
是必填，但兼容端点不返回时 `construct_type` 会把它填成 `None`（实测），实现侧要按可空处理。

`RemoteEncoder(AsyncOpenAI, model)`：`encode()` 里 `dimensions` 走 `omit` 表达「不传」
（显式 `None` 会被 SDK 序列化成 JSON `null`）。`build_encoder(config)` 是全仓库除
`core/llm/client.py` 外唯一 `AsyncOpenAI(...)` 的地方，`timeout` / `max_retries` 都显式给值。

## 六、服务接口（`service.py`）

```python
class EmbeddingService(Service):
    name: ClassVar[str] = "embedding"

    def __init__(self, config: EmbeddingSettings) -> None: ...

    @property
    def model(self) -> str: ...

    async def embed(self, text: str, *, dimensions: int | None = None) -> list[float]: ...
    async def embed_many(
        self, texts: Sequence[str], *, dimensions: int | None = None
    ) -> list[list[float]]: ...

    def _make_encoder(self, config: EmbeddingSettings) -> Encoder: ...
```

生命周期：

```python
async def start(self) -> None:
    if self._encoder is not None:
        return                      # 幂等：已持有内核即早退
    if not self._config.api_key:
        self.log.warning(NO_API_KEY, 端点=self._config.base_url)
        return                      # 只警告：脚手架不该因为没密钥就起不来
    self._encoder = self._make_encoder(self._config)

async def stop(self) -> None:
    encoder, self._encoder = self._encoder, None
    if encoder is not None:
        await encoder.aclose()      # 幂等 + 可安全作用在从未启动过的实例上
```

`health()`：`healthy = self._encoder is not None`，`detail` 为空或 `NO_API_KEY`，不发请求。

## 七、数据流

`embed(text, *, dimensions=None)`：

1. 校验 `text.strip()` 非空，否则抛 `EmbeddingInputError`。
2. 解析维度：`_pick(dimensions, config.dimensions)`（调用方覆盖 > 配置 > 不传）。
3. `_require_encoder()`（未配 key 时抛 `EmbeddingConfigError`）。
4. 一次请求（`texts=[text]`）→ 按 `index` 归位、校验条数与维度（`_reorder` + `_check_vectors`）。
5. 打一条日志 → 返回。

`embed_many(texts, *, dimensions=None)`：

1. `len(texts) == 0` → 直接返回 `[]`，**不发请求、不检查密钥**（空输入不需要服务）。
2. **先全量校验**所有文本非空，再发第一批：否则传到第 5 条才发现空串，前几批白花钱。
3. 解析维度、取内核。
4. 按 `batch_size` 切片，逐批 `await`（串行）；每批完成打一条日志。
5. 每批结果在**服务层**按 `item.index` 归位后拼接（`_reorder`；**不依赖端点返回顺序**，
   兼容端点可能乱序）；`index` 越界、重复或条数不符抛 `EmbeddingResponseError`。
6. 任一批抛错则整次调用抛错，已完成的批作废。
7. 批数 > 1 时补一条汇总日志。

`dimensions` 护栏：解析后的值非 None 时，校验每条向量长度是否等于它，
不符抛 `EmbeddingResponseError`——否则端点静默忽略该参数时无人察觉。

## 八、错误处理与日志

`errors.py`（全部继承 `EmbeddingError`，SDK 原始异常挂 `__cause__`，不进 `ServiceError` 树）：

| 场景 | 异常 |
| --- | --- |
| 未配 `api_key` / 内核未创建 | `EmbeddingConfigError` |
| 空串 / 全空白串 | `EmbeddingInputError` |
| `APIStatusError`（4xx / 5xx） | `EmbeddingRequestError`（带 `status_code` / `endpoint` / `model` / `request_id`） |
| `APITimeoutError` | `EmbeddingTimeoutError` |
| `APIConnectionError` | `EmbeddingConnectionError` |
| 条数不符 / `index` 越界或重复 / 向量为空 / 声明维度不符 | `EmbeddingResponseError` |

映射收在模块级 `_wrap_errors` 上下文管理器里。**`except` 顺序是硬要求**：
`APITimeoutError` 是 `APIConnectionError` 的子类，必须先捕超时。

日志（不记正文，与 llm 一致）：

- 每批一条 `self.log.info("向量化完成", 模型=…, 端点=…, 文本数=…, 维度=…, 耗时毫秒=…)`，
  `EncodeResult.prompt_tokens` 非 None 时补 `输入token`（兼容端点未必返回，取不到就省略）。
- 批数 > 1 时再打一条 `self.log.info("批量向量化完成", 批数=…, 文本数=…, 耗时毫秒=…)`。
- 失败走 `self.log_error`（自动带 `错误=类型: 消息`）。
- `**fields` 展开会逐个形参对账，里面若含 `face` 会撞类型：与 llm 层一致，显式传 `face=None`。

## 九、改动清单

| 文件 | 动作 |
| --- | --- |
| `core/config/settings.py` | 新增 `EmbeddingSettings`，`Settings` 加 `embedding` 字段 |
| `core/config/__init__.py` | 导出 `EmbeddingSettings` |
| `core/embedding/__init__.py` | 新增，门面导出 |
| `core/embedding/encoder.py` | 新增，`Encoder` / `RemoteEncoder` / `build_encoder` |
| `core/embedding/errors.py` | 新增，`EmbeddingError` 树 |
| `core/embedding/service.py` | 新增，`EmbeddingService` |
| `core/service/registry.py` | `register(EmbeddingService)`，插在 clock 与 llm 之间 |
| `data/config/app.yaml` | 补 `embedding:` 段 |
| `tests/test_embedding_service.py` | 新增 |
| `tests/test_service_injection.py` | 注册表顺序断言 `["clock"]` / `["clock", "llm"]` → 含 `embedding` |
| `README.md` | 配置项表格补 `embedding` 段；新增「向量层」章节 |

`pyproject.toml` **不动**（复用已有的 `openai>=3.0`）。不新增任何依赖。

## 十、测试策略（`tests/test_embedding_service.py`）

假内核白盒注入（构造器不能收 `Encoder`）：

```python
service._encoder = cast("Encoder", FakeEncoder(...))  # pyright: ignore[reportPrivateUsage]
```

| 用例 | 断言 |
| --- | --- |
| 单条正常返回 | 返回 `str` 对应的向量，条数 1 |
| `dimensions` 三级优先 | 调用覆盖 > 配置 > 不传；假内核记账收到的 `dimensions` |
| 批量切分次数 | `n=25`、`batch_size=10` → 内核被调 3 次（10 / 10 / 5） |
| 批量乱序归位 | 假内核乱序返回 `EncodedVector(index=…)` → 结果仍按入参顺序 |
| `index` 越界 / 重复 / 条数不符 | 三种都抛 `EmbeddingResponseError` |
| 日志带 `输入token` | 假内核给 `prompt_tokens` 时有该字段；给 `None` 时无 |
| 空输入不打请求 | `embed_many([])` 返回 `[]` 且内核零调用（含未配 key 的场景） |
| 空串与全空白串 | 两者都抛 `EmbeddingInputError`；批量中第 3 条为空 → 内核零调用 |
| 未配 key | `start()` 不抛错只警告、`_encoder is None`、`health().healthy is False`；调用抛 `EmbeddingConfigError` |
| 有内核时 health | `healthy is True` 且内核零调用 |
| 声明维度不符 | 解析出的 `dimensions=512`、内核返回 1024 维 → `EmbeddingResponseError` |
| `start` / `stop` 幂等 | 连续两次不抛错，内核只被 `aclose()` 一次 |
| SDK 异常映射 | 超时 / 4xx（带 `status_code`、`request_id`）/ 连接失败 / 条数不符 各自映射到对应错误 |
| 配置校验 | `timeout=0`、`retries=-1`、`batch_size=0` → `ValidationError` |
| 日志 | 批数 > 1 时两条日志、`文本数` / `耗时毫秒` 是数值；单批时无汇总行 |

## 十一、风险与取舍

- **`batch_size` 是供应商强相关参数**：阿里 v3 上限 10 条、v1/v2 是 25、OpenAI 是 2048。
  默认取最小值 10，换端点后可调大；超过上限时端点是 400，本层不预先熔断。
- **`embed_many([])` 早退意味着未配 key 也「成功」**：这是刻意的（空输入不需要服务），
  写进 README，免得日后被当成 bug 修掉。
- **串行切分在大批量下偏慢**：100 条按 10 切 = 10 次请求串行。当前场景（批量巩固、离线）
  可接受；真要吞吐时再引入并发，那会带来部分失败语义，不在本次范围。
- **`dimensions` 与本地内核存在能力差**：协议已规定「做不到必须抛错」，因此本地 bge 接入后
  调用方传 `dimensions` 会报错而不是拿到错误维度——这是刻意选择。
- **与 `llm/client.py` 的 5 行重复**：`AsyncOpenAI(api_key, base_url, timeout, max_retries)`
  两处各写一遍。抽公共基类会动已稳定的 llm 层，本次接受重复。
- **`request_id` 未必存在**：`APIStatusError.request_id` 可能是 None，字段保留但不保证有值。
- **`EmbeddingResponseError` 的 `index` 归位**：`index` 由端点给出，归位在服务层 `_reorder`，
  越界 / 重复 / 条数不符按异常处理而不是猜测顺序。
- **`Encoder.aclose()` 让本地实现多写一个空方法**：比在 `stop()` 里 `isinstance` 判断实现类型更干净。
- **新层不在 coverage omit 里**：只 omit `core/main.py`，`core/embedding/*` 需要真实测试覆盖。

## 十二、验收标准

1. `uv run pytest` 全绿（基线 183 + 新增用例）。
2. `uv run ruff check .` 与 `uv run ruff format --check .` 无输出。
3. `uvx basedpyright core tests` 报 `0 errors, 0 warnings, 0 notes`。
4. `uv run python -m core.main` 能启动并优雅关闭；未配 key 时打印 `embedding` 的 unhealthy
   健康检查（含「未配置 api_key」说明），不报错退出。
5. 两种 import 顺序均可：`python -c "import core.embedding"` 与 `python -c "import core.service"` 都不抛 ImportError。
6. 实测 `load_settings()` 能读回 `embedding` 段（防 `extra="ignore"` 静默失效）。
7. 配了 `.env` 的 `DASHSCOPE_API_KEY` 后，`embedding` 健康检查为 healthy 且单条 / 批量调用返回正确维度
   （手工冒烟，不写进自动化测试）。
8. README 配置项表格含 `embedding.*` 各字段；新增「向量层」章节含两条调用示例与
   「原样透传、不归一化」「空序列早退」两条约定。

---

## 实施补记（2026-09-26）

1. **协议出口从纯向量改成带结构**（实施中按用户要求回改）。原方案的
   `encode() -> list[list[float]]` 有两个表达不出来的事实：`index` 只有内核拿得到
   （归位被迫下沉进内核，与「内核可替换」的定位冲突），`usage` 也没有出口
   （`输入token` 永远取不到）。现协议返回 `EncodeResult`，归位（`_reorder`）与
   `输入token` 日志都回到服务层，内核只上报位置与用量。
2. **`CreateEmbeddingResponse.usage` 必须按可空处理**：SDK 类型里它是必填，但用
   `construct_type` 实测——数据里没有 `usage` 时该字段被填成 `None`，直接取属性会炸。
   basedpyright 会把变量注解收窄回 `Usage`（`usage is None` 报 `reportUnnecessaryComparison`），
   只能写 `cast("Usage | None", cast("object", response.usage))`——双重 cast 与 llm 层同一踩坑。
