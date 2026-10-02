# 判断层（Laya / Jev）设计

日期：2026-10-02
状态：设计已确认，待实施
基线：`main`（config / logger / service / llm / embedding 各层就绪）

**实施补记（2026-10-02，动工前勘察所得，与本文的偏差）**：

1. **HTTP 客户端用 `httpx2` 而不是 `httpx`**。本文写作时假设「`httpx` 随 `openai` 装在环境里」，实测不成立：`openai` 3.x 的依赖是 `httpx2>=2.12.0,<3`（环境里是 2.13.1），**`httpx` 1.x 根本没装**，才是需要新增安装的那个。两者的客户端 API 与异常层级一致（实测 `TimeoutException` → `TransportError` → `RequestError` → `HTTPError`，`AsyncClient` / `MockTransport` / `Timeout` 齐备），所以只是包名与声明版本的变化，`>=2.12` 与 SDK 的约束相容。本文其余部分已同步改成 `httpx2`。
2. **用 `uv sync --no-install-project`**：worktree 里 `uv sync` 会撞 uv 构建缓存里 hatchling/pluggy 的 `AttributeError: module 'pluggy' has no attribute 'HookimplMarker'`。跑测试只需要依赖 + 工作区里的 `core/`（`python -m pytest` 会把 cwd 加进 `sys.path`），跳过项目自身的 editable 构建即可。

## 〇、背景：Laya 与 Jev 是什么

**Jev**（TypeSafe AI）：闭源、仅云端 API 的「System 1」决策模型。把一段上下文对照一组**带类型的问题**做一次前向，直接给出带概率的答案，不生成文本。

**Laya**（ConvAI Innovations，Apache-2.0）：对标的开源实现。非自回归（基于 ModernBERT / mmBERT 双向编码器），单次前向约 33 ms，输出分类打分与概率。两种用法：`pip install laya` 进程内 Python 库，或 `pip install "laya[serve]"` 起 `laya-serve` 自托管 HTTP 服务（默认 `0.0.0.0:8000`）。

**关键事实：两者共用同一套线协议。** `laya-serve` 官方声称兼容 Jev 线协议，现有 Jev 客户端改 Base URL 即可切换。本设计完全建立在这条事实上。

### 线协议

```
POST {base_url}/systemone
Authorization: Bearer <api_key>          # 服务端要求时
Content-Type: application/json

{
  "state": "...",                        # string | object | array，任意 JSON
  "model": "jev-latest",                 # 省略则交给服务端决定（Laya 走 Router 自动路由）
  "questions": {
    "<问题 id>": { "type": ..., "instructions": ..., "criteria": ... }
  }
}
```

响应：

```json
{
  "model": "jev-1.13.0",
  "answers": {
    "<问题 id>": { "type": "...", ... }
  },
  "usage": { "input_tokens": 296, "output_tokens": 20 }
}
```

`laya-serve` 在此之上多返回一个 `routing` 字段（本次走的哪个 checkpoint、为什么）。

### 三原语

| 原语 | `criteria` | 答案字段 |
| --- | --- | --- |
| `choice` | 映射 `{选项: 描述}`，描述可 `null`；Jev 上限 255 项，Laya HTTP 层上限 100（超限 413） | `choice` / `probabilities` / `confidence` |
| `score` | **有序**描述数组，2–10 级；Laya 要求每级都有描述（`null` 级被 422 拒） | `score` / `legend` / `probabilities` / `confidence` |
| `noul` | 可选 `{true: 描述, false: 描述}`（只能用这两个键） | `noul`（P(true)），**没有 `confidence`** |

顶层的 `questions` 是 map，**一次请求可以把三类问题混在一起算完**——这是该协议最值钱的部分，延迟能被摊薄到每问几毫秒。

### 两个必须记住的差异

1. **`confidence` 不是同一个量**：Jev 是 `(n·p_max − 1)/(n − 1)`，Laya 是 `1 − 归一化熵`。**两个数不能互相比较**，阈值不可跨后端迁移。跨后端可比的只有 Laya 的 `answer_confidence`（`= max(p)`，Jev 不返回）。
2. **鉴权要求不同**：Jev 必须带密钥；`laya-serve` 不设 `LAYA_API_KEY` 时**不需要密钥**。

## 一、目标与范围

新增 `core/decision/` 判断层：把「让模型做一次判断」收成一个服务，业务代码不直接接触 HTTP 与协议细节。与 `llm`（生成）、`embedding`（向量）平级，补上「判断」这一格。

覆盖能力：**批量提问**（`predict`）+ **三个语义化便捷方法**（`choose` / `rate` / `ask`）。

非目标（本次不做）：

- **Laya 进程内推理**：`pip install laya` 的硬依赖是 `torch 2.14` + `transformers 5.x` + `huggingface_hub 1.x`，会把安装体积从几十 MB 拉到 GB 级，并引入 GPU / 显存 / 预热概念。本地部署交给进程外的 `laya-serve`。
- **跨后端兜底**：不做「Laya 判断不放心就转 Jev」。两个后端的 `confidence` 语义不同，层内比较必然出错，兜底逻辑留给调用方。
- **批量端点**：Laya 专有的 `/systemone/batch`（≤64 state）不接，Jev 没有对应能力。
- **`abstention` / `min_confidence`**：Laya 的弃权机制透传在 `answers` 里，但不在层内做阈值决策。
- 熔断、限流、配额、token 计费统计。

## 二、决策记录

| # | 议题 | 结论 | 被否决的方案与原因 |
| --- | --- | --- | --- |
| 1 | 模块命名 | `core/decision/`，`laya` / `jev` 退为**端点名** | 叫 `core/laya` 或 `core/jev`（层与后端混为一谈，加第三个后端就要动目录结构） |
| 2 | 客户端形态 | `httpx2.AsyncClient` 直调 | 官方 `openai` SDK（两家的协议都不是 OpenAI 形状，SDK 的 TypedDict 与结构化输出全用不上，只剩传输层）；自研适配层（没有第二个协议要适配） |
| 3 | Laya 接入形态 | 进程外 `laya-serve` 的 HTTP 端点 | `pip install laya` 进程内（GB 级依赖、破坏秒级 `uv sync`、100% 覆盖率要为它把整个 `laya` 假掉）；预留「可替换内核」插槽（只有一个实现，抽象即预留，与 `RemoteEncoder` 的既有取舍一致） |
| 4 | 两个端点的关系 | **可切换，不做兜底**：显式 `backend=` 指定，`None` 走默认 `laya` | 主备兜底（两后端 `confidence` 语义不同、不可比，层内决策必错）；只配一个端点（放弃「本地优先 + 云端按需」的同时可用） |
| 5 | 端点启用判据 | 显式 `enabled: bool`（默认 `false`） | 「`api_key` 为空即未启用」（`laya-serve` 不设 `LAYA_API_KEY` 时本来就不需要密钥，沿用旧规则会逼用户填假密钥） |
| 6 | 能力面 | 薄层 `predict(state, questions)` + 三个便捷方法 `choose` / `rate` / `ask` | 只暴露便捷方法（每次只能问一类，丢掉「一次前向混问多题」这个协议核心能力）；只暴露 `predict`（已有明确消费者，便捷方法不是预留） |
| 7 | 返回值 | 顶层结构化成 dataclass，**`answers` 原样透传** | 全量 pydantic 建模（两家 answer 并不同构，Laya 多 `answer_confidence` / `abstention` / `low_confidence`，配合 `extra="ignore"` 会**静默吃掉**这些字段） |
| 8 | 端点路由 | 调用方显式传 `backend` | 照抄 llm 的「传的 model 命中另一端点就切过去」（两家的模型名可能撞车，规则隐晦且难排查） |
| 9 | 重试 | 自写退避：只对 `429` / `529` 重试，指数退避，上限 `retries` 次 | 不重试（官方明确建议退避）；解析 `Retry-After`（多一个分支只为一句话文档里的建议，不值）；交给 httpx2（`AsyncHTTPTransport(retries=)` 只重试连接错误，不覆盖 429/529） |
| 10 | 参数上限的本地校验 | **不做**，交给端点返回 422 / 413 | 本地校验（Jev 255 项 / Laya 100 项、score 2–10 级、Laya 要求每级有描述——按任一后端的规则拦都会误伤另一个，与 llm 层「调用级覆盖不做本地校验」一致） |
| 11 | 边界 URL | `base_url` 写到 `/v1` 为止，路径固定常量 `/systemone` | 让 `base_url` 写全（用户容易多写一段而 404）；把路径做成配置项（两家的完整地址本来就是 `…/v1/systemone`，一个常量对两边都成立） |
| 12 | 未启用端点是否警告 | **不警告**，静默跳过 | 沿用 llm 的 `warning`（llm 无法区分「故意留空」与「忘了填密钥」，而 `enabled` 是用户的显式选择，警告是噪音） |
| 13 | `health()` 判定 | 只看有没有客户端，不发请求；**两个端点都没启用才算 unhealthy** | 发探测请求（健康检查不该有副作用、花钱、受网络抖动影响）；任一端点未启用就报 unhealthy（只启用一头也能用） |

## 三、配置层改动

`core/config/settings.py` 新增两个类（与 `LLMEndpointSettings` / `LLMSettings` 同级）：

```python
Backend = Literal["jev", "laya"]   # 服务层与配置层共用


class DecisionEndpointSettings(BaseModel):
    """一个判断端点的配置。

    `model` 为空表示「请求体里不带 model 字段」：Laya 靠这个走 Router 自动路由，
    Jev 则会 422（它的 model 是必填）。传空串或 null 都不等于「不传」。
    """

    enabled: bool = False
    base_url: str = ""
    api_key: str = ""   # 留空则不发 Authorization 头（laya-serve 默认不要求认证）
    model: str = ""
    timeout: float = Field(default=60.0, gt=0)  # 单次请求超时（秒）
    retries: int = Field(default=2, ge=0)       # 429/529 退避重试次数，0 关闭


class DecisionSettings(BaseModel):
    """判断层配置：jev 云端与 laya 本地是两个端点，字段相同。"""

    jev: DecisionEndpointSettings = DecisionEndpointSettings(
        base_url="https://api.typesafe.ai/v1", model="jev-latest"
    )
    laya: DecisionEndpointSettings = DecisionEndpointSettings(
        base_url="http://127.0.0.1:8000/v1", model=""
    )
```

`Settings` 增加顶层字段 `decision: DecisionSettings = DecisionSettings()`；`core/config/__init__.py` 导出两个类与 `Backend`。

`data/config/cosmos.yaml` 补一段（值等于默认，保持「骨架即全貌」）：

```yaml
# 判断层：jev（云端）与 laya（本地 laya-serve）是两个端点，同一套 /systemone 协议。
# base_url 只写到 /v1 为止，路径 /systemone 由代码补上。
# 两个端点默认都关着：不警告、不 fail fast，调用时才报未启用（见决策 12）。
decision:
  jev:
    enabled: false
    base_url: https://api.typesafe.ai/v1 # 官方云端端点
    api_key: ${TYPESAFE_API_KEY:} # 冒号后为空：.env 里没配时不发 Authorization 头
    model: jev-latest # 该端点必填，Jev 不接受空 model
    timeout: 60 # 单次请求超时（秒），必须为正数
    retries: 2 # 429/529 的退避重试次数，0 关闭
  laya:
    enabled: false
    base_url: http://127.0.0.1:8000/v1 # 本地 laya-serve，默认监听 0.0.0.0:8000
    api_key: ${LAYA_API_KEY:} # laya-serve 未设 LAYA_API_KEY 时留空即可
    model: "" # 空 = 不带 model 字段，交给 Laya 的 Router 按语种自动选 checkpoint
    timeout: 60
    retries: 2
```

`.env.example` 补 `TYPESAFE_API_KEY` / `LAYA_API_KEY` 两行。

## 四、模块结构

```
core/decision/
  __init__.py    # 对外出口：DecisionService、三个答案 dataclass、三原语构造器、错误树、Backend
  client.py      # build_client(endpoint) -> httpx2.AsyncClient：唯一 new 出 HTTP 客户端的地方
  errors.py      # DecisionError 树
  questions.py   # choice / score / noul 构造器 + Question TypedDict
  service.py     # DecisionService：进 DI 容器，持有两个端点的客户端
```

依赖方向：`decision → (config, logger, service)`。与既有各层一致，不引入新的例外。

`core/decision/service.py` 里必须写 **`from core.service.base import Service`（子模块绝对路径）**，不写 `from core.service import Service`——与其它层同一条约定。

`core/service/registry.py` 的 `build_manager()` 加一个类型：

```python
manager.register(ClockService, EmbeddingService, LLMService, DecisionService)
```

`DecisionService` 不依赖其它服务，四个服务互不依赖 → 落在同一层并发启动，装配期形状不变。

## 五、三原语构造器（`questions.py`）

对应 `llm/messages.py` 的位置与原则：**只填字面量，不包成自有类型**；返回**具体的 TypedDict** 而不是联合体（联合体会放过拼错的键，具体类型才能在构造处报错）。

```python
#: 问题 id 是调用方自定义的键，只在本地做映射，不参与推理
type Question = ChoiceQuestion | ScoreQuestion | NoulQuestion


class ChoiceQuestion(TypedDict):
    type: Literal["choice"]
    instructions: str
    criteria: dict[str, str | None]


class ScoreQuestion(TypedDict):
    type: Literal["score"]
    instructions: str
    criteria: list[str]


class NoulCriteria(TypedDict):
    true: str
    false: str


class NoulQuestion(TypedDict, total=False):
    type: Required[Literal["noul"]]
    instructions: Required[str]
    criteria: NoulCriteria


def choice(instructions: str, criteria: Mapping[str, str | None]) -> ChoiceQuestion: ...
def score(instructions: str, criteria: Sequence[str]) -> ScoreQuestion: ...
def noul(instructions: str, *, true: str | None = None, false: str | None = None) -> NoulQuestion: ...
```

要点：

- `choice` 的 `criteria` 值是 `str | None`：`None` 表示该选项不需要额外说明（协议允许 `null`）。
- `score` 只收 `str` 序列，天然不会产生 `null` 级——Laya 会对 `null` 级返回 422。
- `noul` **两个都给才带 `criteria`**：协议规定 `criteria` 只能用 `true` / `false` 两个键，只给一个等于给一半。
- **不做任何上限校验**（选项数、级数、空序列）。理由见决策 10。

## 六、服务接口（`service.py`）

```python
#: state 就是任意 JSON：纯文本、业务记录、聊天记录、数组都可以
type State = str | dict[str, object] | list[object]


@dataclass(frozen=True, slots=True)
class DecisionResult:
    """一次 predict 的结果。answers 原样透传协议结构，键与 questions 一一对应。"""

    model: str
    answers: dict[str, dict[str, object]]
    input_tokens: int | None
    output_tokens: int | None
    routing: dict[str, object] | None  # 只有 Laya 返回


@dataclass(frozen=True, slots=True)
class ChoiceAnswer:
    choice: str
    probabilities: dict[str, float]
    confidence: float


@dataclass(frozen=True, slots=True)
class ScoreAnswer:
    score: float
    legend: dict[str, str]
    probabilities: dict[str, float]
    confidence: float


class DecisionService(Service):
    name: ClassVar[str] = "decision"

    def __init__(self, config: DecisionSettings) -> None: ...

    async def predict(
        self, state: State, questions: Mapping[str, Question], *, backend: Backend | None = None
    ) -> DecisionResult: ...
    async def choose(
        self, state: State, instructions: str, criteria: Mapping[str, str | None],
        *, backend: Backend | None = None,
    ) -> ChoiceAnswer: ...
    async def rate(
        self, state: State, instructions: str, criteria: Sequence[str],
        *, backend: Backend | None = None,
    ) -> ScoreAnswer: ...
    async def ask(
        self, state: State, instructions: str, *, true: str | None = None,
        false: str | None = None, backend: Backend | None = None,
    ) -> float: ...
```

要点：

- **`backend=None` 走 `laya`**（本地优先：免费、数据不出域）。要 Jev 就显式 `backend="jev"`。不设 `default_backend` 配置项——与 llm 层「chat 是隐含默认端点、不由配置决定」同一条思路，少一个只为默认值存在的旋钮。
- **`answers` 原样透传、不建模**：两家 answer 并不同构（Laya 多 `answer_confidence` / `abstention` / `low_confidence`，`legend` 只有 `score` 有），一建模加 `extra="ignore"` 就会把这些字段静默吃掉——而「不吃字段」正是选薄层的理由。类型是 `dict[str, dict[str, object]]`，即「答案这一层不做承诺」，调用方按键取。
- **`routing` 只有 Laya 返回**，Jev 时为 `None`。
- **三个便捷方法内部复用 `predict`**：问题 id 用固定常量 `_QID = "answer"`，构造单题 `questions` 后从 `answers[_QID]` 取；键不在、`type` 对不上、字段类型不对 → `DecisionResponseError`（消息里带上实际拿到的键）。
- **`ask` 返回 `float`**（P(true)）而不是 dataclass：Jev 的 `noul` answer 只有 `{type, noul}`，连 `confidence` 都没有，包一层没东西可装。
- **请求体**：`{"state": …, "questions": …}`；`model` 为空时**省略该键**而不是传 `null`（Laya 的自动路由靠「不带 model」触发，传空串或 `null` 可能被 422 拒）。这与 llm 层用 `omit` 表达「不传」是同一条规则。
- `input_tokens` / `output_tokens` 为 `int | None`：`usage` 整体缺失或字段缺失时不报错，日志相应省略。

### 生命周期

```python
async def start(self) -> None:
    if self.running:
        return
    self._clients = {
        name: build_client(endpoint)
        for name, endpoint in self._endpoints()
        if endpoint.enabled
    }

async def stop(self) -> None:
    clients, self._clients = self._clients, {}
    for client in clients.values():
        await client.close()
```

- 只为 `enabled: true` 的端点建客户端。**未启用不警告**（见决策 12）。
- `stop()` 幂等：回滚时它也会作用在从未建过客户端的实例上，字典为空就是空转。

### 健康检查

```python
async def health(self) -> HealthStatus:
    # healthy = 只要有一个端点建了客户端
    # detail  = "" / NO_ENDPOINT（跑着但没启用任何端点）/ not_running_detail(state)
```

- 不发任何网络请求。
- 两个端点都没启用才算 unhealthy；只启用一头也是能用的。
- `NO_ENDPOINT` 措辞统一常量：start 之后的调用报错、health 的 detail 共用。
- 已停止 / 未启动时复用 `core/service/base.not_running_detail(state)`——与 llm / embedding 一样，`stop()` 之后套用「未启用」会误导。
- `extra` 对称报出两端的模型与端点（`jev模型` / `jev端点` / `laya模型` / `laya端点`），缺哪一头一眼可见；未启用时也照报，否则看不出是哪一头没开。

## 七、客户端与重试（`client.py`）

```python
SYSTEMONE_PATH = "/systemone"  # base_url 只写到 /v1，路径由代码补


def build_client(endpoint: DecisionEndpointSettings) -> httpx2.AsyncClient:
    headers = {"Authorization": f"Bearer {endpoint.api_key}"} if endpoint.api_key else {}
    return httpx2.AsyncClient(
        base_url=endpoint.base_url, headers=headers, timeout=endpoint.timeout
    )
```

- 全仓库唯一 `new` 出 httpx2 客户端的地方，也是测试注入替身的接缝。
- `Authorization` 头只在 `api_key` 非空时加。
- 超时交给 httpx2（`timeout=` 是总超时，含连接与读取）。

### 重试

只对 `429` / `529` 重试，退避 `_BACKOFF_BASE * 2**n` 秒，最多 `retries` 次（总请求数 = `retries + 1`，与 SDK 语义一致）。退避基数抽成模块级常量：

```python
_BACKOFF_BASE = 0.5  # 秒；测试 patch 成 0 即可免去伪造 asyncio.sleep
```

不解析 `Retry-After`。客户端错误（4xx，除 429）不重试，直接映射成 `DecisionRequestError`。

## 八、错误处理与日志

`errors.py` 的错误树（全部继承 `DecisionError`；原始异常只在超时 / 连接 / 返回体解码失败上挂 `__cause__`，状态码错的坐标收在 `DecisionRequestError` 的四个属性里；**不在 `ServiceError` 树下**——调用失败是运行期错误，不该让进程 fail fast）：

| 场景 | 异常 |
| --- | --- |
| 端点未启用时发起调用 | `DecisionConfigError`（无 cause：根本没发请求） |
| 4xx / 5xx | `DecisionRequestError`（带 `status_code` / `endpoint` / `model` / `request_id`，无 cause） |
| 请求超时 | `DecisionTimeoutError`（cause 是 `httpx2.TimeoutException`） |
| 连不上端点，或其余请求错误 | `DecisionConnectionError`（cause 是 `httpx2.TransportError` / `RequestError`） |
| 返回体不是合法 JSON（含非 UTF-8）、压缩体解不开 | `DecisionResponseError`（cause 是 JSON 解析异常或 `httpx2.DecodingError`） |
| 缺 `answers`、便捷方法要的答案键/字段不在 | `DecisionResponseError`（无 cause） |

映射收在模块级 `_wrap_errors` 上下文管理器里，四个公开方法统一走它。

**`except` 顺序是硬要求**：httpx2 的 `TimeoutException` 是 `TransportError` 的**子类**，必须先捕 `TimeoutException` 再捕 `TransportError`，否则超时会被全部误判成连接错误。这与 openai SDK 的 `APITimeoutError` / `APIConnectionError` 是同一个坑，两层的 `_wrap_errors` 会长得很像。

`request_id` 从响应头取（httpx2 不像 openai SDK 那样替我们解析）：

```python
request_id = response.headers.get("x-request-id")
```

日志（都走 `self.log`，带 `[decision]` 前缀）：

- 成功一条「判断调用完成」：`模型` / `端点` / `问题数` / `耗时毫秒` / `输入token` / `输出token`（拿不到的字段省略）。
- 重试一条「判断调用重试」：`状态码` / `第几次` / `等待毫秒`。
- **不记 `state` 与 `questions` 正文**：与 llm 层「不记正文」一致，正文该不该记由调用方自己决定并自己打。

## 九、改动清单

| 文件 | 动作 |
| --- | --- |
| `pyproject.toml` | `dependencies` 加 `httpx2>=2.12`（**已随 `openai` 装在环境里**，此处只是不再算隐式可用；`httpx` 1.x 反倒要新装，见文末实施补记） |
| `core/config/settings.py` | 新增 `Backend`、`DecisionEndpointSettings`、`DecisionSettings`；`Settings` 加 `decision` 字段 |
| `core/config/__init__.py` | 导出上述三个名字 |
| `core/decision/__init__.py` | 新增，对外出口 |
| `core/decision/client.py` | 新增，`build_client` + `SYSTEMONE_PATH` |
| `core/decision/errors.py` | 新增，`DecisionError` 树 |
| `core/decision/questions.py` | 新增，三原语构造器与 `Question` 类型 |
| `core/decision/service.py` | 新增，`DecisionService` + 三个答案 dataclass |
| `core/service/registry.py` | `build_manager()` 的 `register(...)` 加 `DecisionService` |
| `data/config/cosmos.yaml` | 补 `decision:` 段 |
| `.env.example` | 补 `TYPESAFE_API_KEY` / `LAYA_API_KEY` |
| `tests/test_decision_questions.py` | 新增，三原语构造器 |
| `tests/test_decision_service.py` | 新增，服务层 |
| `README.md` | 配置项表补 `decision.*`；新增「判断层」章节 |

## 十、测试策略

**替身接缝用 `httpx2.MockTransport`**（httpx2 官方的测试通道）：`build_client` 里不接受 transport 参数，因此测试把 handler 造好、构造一个带 MockTransport 的客户端，白盒塞进 `service._clients`：

```python
service._clients = {"laya": _mock_client(handler)}  # pyright: ignore[reportPrivateUsage]
```

不起真实 HTTP 服务、不打桩 socket。handler 直接拿到 `httpx2.Request`，可断言**请求体与请求头**（这是本层最值得断言的东西：协议形状）。

| 用例 | 断言 |
| --- | --- |
| `choice()` 构造 | `type` 是 `choice`；`criteria` 里 `None` 值原样保留 |
| `score()` 构造 | `criteria` 是有序列表，顺序即级序 |
| `noul()` 构造 | 两个描述都给才有 `criteria`；只给一个时没有该键 |
| `predict` 正常 | 请求体含 `state` / `model` / `questions`；返回 `DecisionResult`，`answers` 与 `usage` 正确 |
| `model` 为空 | 请求体**不含** `model` 键（不是 `null`） |
| `api_key` 为空 | 请求头**没有** `Authorization` |
| `api_key` 非空 | 请求头是 `Bearer <key>` |
| 路径拼接 | 请求 URL 是 `{base_url}/systemone` |
| Laya 的 `routing` | 返回体带 `routing` 时 `DecisionResult.routing` 非 `None`；Jev 不带时为 `None` |
| `usage` 缺失 | `input_tokens` / `output_tokens` 为 `None`，不报错 |
| `answers` 里 Laya 的额外字段 | `answer_confidence` / `abstention` 原样出现在 `answers` 里（不被吃掉） |
| `choose` | 从 `answers["answer"]` 取出 `ChoiceAnswer`；`probabilities` 键值正确 |
| `rate` | 取出 `ScoreAnswer`，`legend` 与 `score` 正确 |
| `ask` | 返回 P(true) 浮点值 |
| 便捷方法缺答案键 | `DecisionResponseError`，消息里含实际键 |
| 便捷方法 `type` 对不上 | `DecisionResponseError` |
| 答案字段类型不对 | `DecisionResponseError` |
| 未启用端点调用 | `DecisionConfigError`，无 cause |
| 4xx / 5xx | `DecisionRequestError`，`status_code` / `endpoint` / `model` / `request_id` 正确 |
| 超时 | `DecisionTimeoutError`（handler 抛 `httpx2.ReadTimeout`） |
| 连接错 | `DecisionConnectionError`（handler 抛 `httpx2.ConnectError`） |
| 非法 JSON | `DecisionResponseError` |
| 缺 `answers` | `DecisionResponseError` |
| 429 退避后成功 | handler 依序返回 429 → 200，断言被调用 2 次、日志有「判断调用重试」 |
| 529 同上 | 529 → 200，行为一致 |
| 重试耗尽 | 连续 429，`retries=2` 时 handler 被调用 3 次，最终 `DecisionRequestError` |
| `retries=0` | 一次 429 即报错，handler 只被调用 1 次 |
| 非重试状态码 | 400 不重试（handler 只调用 1 次） |
| `start` 只建已启用端点 | 只 `enabled` 的那一头有客户端，另一头为 `None`；未启用不产生 warning 日志 |
| `stop` 幂等 | 连续两次不抛错，客户端只被 `close()` 一次 |
| `health` 三态 | 有一头客户端 → healthy；跑着但都没启用 → unhealthy + `NO_ENDPOINT`；`stop()` 后 → unhealthy + 未运行措辞 |
| `health().extra` | 四个字段与配置一致，未启用时也照报 |

退避耗时靠 `monkeypatch.setattr(decision_service, "_BACKOFF_BASE", 0.0)` 消掉，不去伪造 `asyncio.sleep`。

## 十一、破坏性变更与风险

**破坏性变更：无。** 全部是新增（新增层、新增配置段、`register` 多一个类型）。`Settings` 是 `extra="ignore"`，旧配置骨架读到 `decision:` 键也不会报错。

**风险与已规避项**：

- **`uv run python -m core.main` 冒烟输出会多一条**：两个端点默认都关，`decision` 健康检查报 unhealthy。这是预期输出，不是回归（与未配密钥时的 `llm` / `embedding` 同形）。
- **`confidence` 跨后端不可比**：已写进 docstring 与 README；层内不做任何阈值决策，从设计上杜绝误用。
- **`base_url` 写过头会 404**：配置注释写明「只写到 `/v1` 为止，路径由代码补上」。
- **Laya 的 `model` 只能是 checkpoint 名**（`english` / `multilingual` / `typed-decisions`）：写别的值由服务端报错；留空即自动路由。
- **零样本判断质量很差**：官方 model card 自述「Laya 是便于特化的基座，不是零样本决策引擎」（零样本准确率接近随机）。README 要写清楚：判断结果必须配阈值或人工兜底，且阈值要按**当前后端**单独校准。
- **新层不在 coverage omit 里**：当前只 omit `core/main.py`，`core/decision/*` 需要真实测试覆盖，否则覆盖率会明显下滑。
- **httpx2 版本**：`openai` 3.x 声明的是 `httpx2>=2.12.0,<3`，实测环境里是 2.13.1；显式声明 `>=2.12` 与它相容。若将来 SDK 改回 `httpx`，以 `uv sync` 的实际解析结果为准。

## 十二、验收标准

1. `uv run pytest` 全绿（既有测试无回归 + 新增两个测试文件）。
2. `uv run ruff check .` 与 `uv run ruff format --check .` 无输出。
3. `uvx basedpyright core tests` 报 `0 errors, 0 warnings, 0 notes`。
4. `pytest --cov=core` = **100%**。
5. `uv run python -m core.main` 能启动并优雅关闭；两端点默认关时多一条 `decision` 的 unhealthy 健康检查，不报错退出。
6. README 配置项表含 `decision.*` 各字段；「判断层」章节含四个调用示例与三条约定（不记正文、confidence 不可跨后端比较、零样本质量差）。
