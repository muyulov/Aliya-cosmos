# ASR 层（实时流式语音识别）设计

- 日期：2026-10-03
- 状态：设计已定（brainstorming 七节逐节确认）
- 目标：在现有 `service / logger / config / llm / embedding / decision` 之外，新增 `core/asr/`，
  把「音频流 → 文本流」收成一个服务，业务代码不直接接触 WebSocket 与 torch。

---

## 0. 决策速览

| # | 议题 | 决策 |
| --- | --- | --- |
| 1 | 定位 | **实时流式转写**（非流式语音消息转写不在本次范围） |
| 2 | 后端 | **远程 + 本地两头**，都要 |
| 3 | 双头形态 | **内核抽象 + 两适配器**（不是同协议双端点，也不是进程外网关） |
| 4 | 远程家 | **DashScope Paraformer 实时**（同一套 `/api-ws/v1/inference` 协议） |
| 5 | 本地栈 | **FunASR / SenseVoice（torch 家族）** |
| 6 | 对外接口 | **会话对象** `ASRSession`（`push` / `events` / `finish`），异步上下文管理器 |
| 7 | 事件粒度 | **全字段 + 情感**（`text` / `is_final` / `begin_ms` / `end_ms` / `words[]` / `emo_tag` / `emo_confidence`） |
| 8 | 失败语义 | **不兜底**（对齐 `decision`：显式 `backend`，失败即抛 `ASRError`） |
| 9 | 本地并发 | **单线程串行**（`ThreadPoolExecutor(max_workers=1)`，对标 `MemorySession`） |
| 10 | 本地切句 | **挂 `fsmn-vad` 流式 VAD**，段边界即句定稿并重置 ASR cache |
| 11 | 音频契约 | **`format` + `sample_rate` 可声明**（不写死 16k 常量）；层内一律不转码 |
| 12 | 默认后端 | **`local`**（本地优先，对齐 decision 的 laya） |
| 13 | 远端心跳 | **关**（`heartbeat: false`，与协议默认一致） |
| 14 | 本地加载时机 | **首次会话懒加载**，不在 `start()` 里加载 |

---

## 1. 定位与边界

这个层解决一件事：**调用方持续喂音频块，层持续产出增量文本，并在句子边界给出定稿**。

它**是**：一个薄封装远端能力的服务层，形态对等 `llm` / `embedding` / `decision`；一个内核接口 + 两个适配器
（远程 WS / 本地进程内模型）；一个显式 `backend` 选择、层内不做兜底的组件。

它**不是**：TTS（合成是另一半）；音频解码器；说话人分离；业务编排（「边说边回话」属调用方）。

---

## 2. 目录与依赖方向

```
core/asr/
  errors.py    错误树（ASRError 基类 + 子类）
  models.py    数据模型：Word / TranscriptEvent / AudioFormat
  kernel.py    内核接口（Protocol）+ 按 backend 建内核的工厂
  remote.py    DashScope Paraformer 实时 WS 适配器
  local.py     FunASR 流式适配器（单线程执行器 + VAD 切句）
  session.py   对外会话对象 ASRSession（push / events / finish）
  service.py   ASRService（生命周期、健康检查、会话入口）
```

- 依赖方向 `asr → (config, logger, service)`，与既有三层同级。
- 唯一反向依赖在 `core/service/registry.py`（它要 import 具体服务类型才能注册），因此 `asr` 不挂
  `core/service/__init__.py`（会成环）。
- **新服务继承 `Service`，`__init__` 只收 `ASRSettings`**（必须挂成 `Settings` 的顶层字段），
  内核 / 客户端 / 模型一律在 `start()` 或首次会话里建（契约校验见 `_validate_contract`）。

**新增依赖两项**（净增，属本次要接受的成本）：

| 依赖 | 用途 | 形态 |
| --- | --- | --- |
| `wsproto`（由 `httpx2[ws]` extra 引入） | 远程 WebSocket | 纯 Python，小 |
| `funasr` + `torch` | 本地流式识别与 VAD | 重；`torch` 数百 MB |

**音频一律不做解码**：不引入 `soundfile` / `librosa` / `ffmpeg`。层对外的契约就是「按声明的 `format` 送字节」，
解码与重采样由调用方负责（与 `embedding` 只收 `str`、不管上游怎么来的一致）。

---

## 3. 配置模型

`ASRSettings` 挂 `Settings.asr`；YAML 骨架 `data/config/cosmos.yaml` 新增 `asr` 段。

```yaml
asr:
  backend: local             # 默认后端：remote | local
  remote:                    # DashScope Paraformer 实时
    base_url: wss://dashscope.aliyuncs.com/api-ws/v1/inference
    api_key: ${DASHSCOPE_API_KEY:}        # 留空 = 该端点不可用
    model: paraformer-realtime-v2         # v2 支持任意采样率
    format: pcm                           # 默认音频编码
    sample_rate: 16000                    # 默认采样率；8000 配 8k-v2 才有情感
    language_hints: []                    # 空 = 自动识别语种
    semantic_punctuation_enabled: false   # false=VAD 断句（交互低延迟）/ true=语义断句（会议更准）
    max_sentence_silence: 1300            # VAD 静音阈值(ms)，仅 VAD 模式生效，取值 [200, 6000]
    heartbeat: false                      # true 则持续发静音保活，长静音不被动断开
    timeout: 60                           # 握手与等 task-started 的超时（秒）
  local:                     # FunASR 流式
    enabled: false           # 本地默认关：显式开启
    model: paraformer-zh-streaming
    vad_model: fsmn-vad
    device: cpu              # cpu | cuda
    model_dir: null          # null = ModelScope 默认缓存目录
```

### 3.1 启用判据（对齐既有各层）

- **remote**：以「`api_key` 非空」为准（同 `embedding`）。留空只警告、健康检查 unhealthy，调用时才抛
  `ASRConfigError`，进程照常启动。
- **local**：以显式 `enabled: true` 为准（同 `decision` 的显式布尔）。默认关。
- **默认 `backend: local`** 而 `local.enabled: false`，与 decision（默认 laya 但 laya.enabled=false）同构：
  进程照常起来，真调时才报未启用。

### 3.2 三级优先（音频契约）

同 `embedding.dimensions` 的口径：**会话覆盖 > 配置默认 > 内置默认**。内置默认即
`format="pcm"` / `sample_rate=16000`。

### 3.3 不开放的旋钮

- FunASR 的 `chunk_size` **固定 600 ms**（`[0, 10, 5]`，`encoder_chunk_look_back=4`、
  `decoder_chunk_look_back=1`）：它必须与模型训练配置匹配，写错会让输出崩坏，因此不做成配置项。
- DashScope 的 `punctuation_prediction_enabled`（默认 true）、`inverse_text_normalization_enabled`
  （默认 true）、`disfluency_removal_enabled`（默认 false）本次不开放，按协议默认值走（YAGNI）。

### 3.4 本地模型不在 `start()` 里加载

几百 MB、几十秒的加载会把启动拖到 `start_timeout` 超时并触发**整进程回滚**；因此本地模型走
**首次会话懒加载**（加载与推理都在单线程执行器里）。`health()` 只反映配置与是否已加载。

---

## 4. 音频契约

- 会话参数带 **`format`**（`pcm` / `wav` / `mp3` / `opus` / `speex` / `aac` / `amr`）+ **`sample_rate`**；
  PCM 隐式为 **16-bit、单声道**。
- **层内不转码**：给的是「声明 + 校验」，不是「转换」。
- **remote** 把这组值原样投进 `run-task.parameters`（该端支持上述全部编码）。
- **local** 只接受 **`pcm`/`16000`**，其它组合在开会话时直接抛 `ASRConfigError`。
- ⚠️ **一处必须写进文档的耦合**：DashScope 只有 **`paraformer-realtime-8k-v2`** 支持情感，而它**只吃
  8 kHz**——所以「情感字段」与默认 16k 配置**互斥**。`sample_rate` 开放配置正是为了让想走情感的人把整条
  线降到 8k（此时拿不到 16k 精度）。
- 远端推荐分片 3200 字节/次（≈100 ms @16k/16bit/单声道）；层不强制分片大小，但会按需透传。

---

## 5. 数据模型（`models.py`）

```python
AudioFormat = Literal["pcm", "wav", "mp3", "opus", "speex", "aac", "amr"]


class Word(BaseModel):
    """字级时间戳。远端给，本地恒为空。"""

    text: str
    begin_ms: int
    end_ms: int
    punctuation: str = ""


class TranscriptEvent(BaseModel):
    """一次转写产出。

    - text：当前句**至今**的完整文本（累积，不是本块增量）；远端天然如此，本地由适配器拼接。
    - is_final：True = 该句已定稿，不会再变。
    - begin_ms / end_ms：句起止（毫秒）；拿不到是 None。
    - words：字级时间戳；拿不到是 []。
    - emo_tag / emo_confidence：情感；仅远端 8k-v2 可能给，其余恒为 None。
    """

    text: str
    is_final: bool
    begin_ms: int | None = None
    end_ms: int | None = None
    words: list[Word] = []
    emo_tag: str | None = None
    emo_confidence: float | None = None
```

- 模型一律 `extra="ignore"`，不做「原样透传后端 JSON」的兜底字段（与 `decision` 的 `answers` 透传不同：
  那里是**不建模**，这里是**建模并归一化**，因为两头字段名根本不同）。
- 情感字段长期可能为空，是**预期**而非缺陷（见 §4 的 8k 耦合与本地能力差）。

---

## 6. 内核接口与会话对象

### 6.1 内核接口（`kernel.py`）

**一个内核实例 = 一路识别会话**，两适配器共同实现：

```python
class Kernel(Protocol):
    async def start(self) -> None: ...                 # 建立会话（远程含 run-task → task-started）
    async def push(self, data: bytes) -> None: ...     # 喂音频（编码由 format 决定）
    def events(self) -> AsyncIterator[TranscriptEvent]: ...
    async def finish(self) -> None: ...                # 通知输入结束
    async def aclose(self) -> None: ...                # 关连接 / 释放 cache


def build_kernel(
    backend: str, config: ASRSettings, *, audio_format: AudioFormat, sample_rate: int
) -> Kernel: ...
```

`push(b"")` **静默忽略**（空块是无害噪声，不是错误）。

### 6.2 对外会话（`session.py`）

```python
async with svc.session(backend="local", sample_rate=16000) as s:
    await s.push(pcm_chunk)
    async for ev in s.events():     # 中间结果 + 句级定稿
        ...
    await s.finish()                # 通知输入结束
```

- `svc.session(...)` 返回**未打开**的 `ASRSession`；`__aenter__` 负责建内核 + `start()`（本地含首次懒加载）。
- `__aexit__` **兜底** `finish()`（尽力）与 `aclose()`，所以调用方可以省掉显式 `finish()`。
- 状态机：`CREATED → OPEN → FINISHING → CLOSED`；非法迁移抛 `ASRSessionError`。

### 6.3 双方向错误传播（会话对象存在的理由）

内核一旦终结（远程 `task-failed` / WS 断开 / 本地推理异常），异常**同时涌向两个方向**：

- `events()` 在下一次迭代抛出；
- 此后所有 `push()` 抛**同一个**异常；
- `finish()` 也复现它。

实现上会话缓存**终结异常一次**，不重复包装、不丢失首次原因。

---

## 7. 远程适配器（`remote.py`）

### 7.1 连接与泵任务

- 握手用 `httpx2.AsyncClient` + `aconnect_ws(url, headers={"Authorization": f"bearer {key}"})`；
  鉴权在**握手期**，失败即 upgrade 失败。
- **泵任务**：一个 asyncio 任务独占 `async with` 作用域并**串行收帧**；`push()` 把字节投进出站队列、
  `events()` 读入站队列、`finish()` 投哨兵、`aclose()` 取消任务并关客户端。
- 为什么必须这样：`httpx2.websockets.AsyncWebSocketSession` 内部是 **anyio task group**，
  必须在 `async with` 作用域内使用（作用域一退，后台收帧任务被取消、连接即断）；并且**逃出作用域的异常会被
  包成 `ExceptionGroup`**。泵任务把这两件事一起封在里面，公开的 `push` / `events` 只见普通异常。

### 7.2 帧协议映射（已查证）

`start()` 发文本帧：

```json
{
  "header": {"action": "run-task", "task_id": "<uuid4>", "streaming": "duplex"},
  "payload": {
    "task_group": "audio",
    "task": "asr",
    "function": "recognition",
    "model": "<配置>",
    "input": {},
    "parameters": {
      "format": "<会话声明>",
      "sample_rate": 16000,
      "language_hints": [],
      "semantic_punctuation_enabled": false,
      "max_sentence_silence": 1300,
      "heartbeat": false
    }
  }
}
```

随后等 `task-started`（官方示例等 10s 即判失败，本层用 `timeout` 控）。`push()` 发**二进制音频帧**；
`finish()` 发 `finish-task`（`header.task_id` 必须与 `run-task` 一致）。

### 7.3 事件映射

| 服务端事件 | 映射 |
| --- | --- |
| `task-started` | `start()` 返回 |
| `result-generated` | 取 `payload.output.sentence`；`heartbeat=true` 的**跳过**；`sentence_end` → `is_final`；`begin_time` / `end_time` / `words[]`（每项含 `text` / `begin_time` / `end_time` / `punctuation`）直取；`emo_tag` / `emo_confidence` 缺则 `None` |
| `task-finished` | `events()` 自然结束 |
| `task-failed` | → `ASRRequestError(error_code, error_message)`，连接不可复用，终结整条会话 |

`payload.usage.duration` 只在 `sentence_end = true` 时下发（秒），本层**只记日志不计费**。

---

## 8. 本地适配器（`local.py`）

### 8.1 执行模型

- **单线程执行器** `ThreadPoolExecutor(max_workers=1)`（线程亲和 + 全局串行，对标 `MemorySession`）；
  模型加载与推理都在这个线程里，天然互斥、无 torch 线程安全之忧。
- **懒加载**：首次会话时 `AutoModel(model=..., device=..., cache_dir=model_dir)` +
  `AutoModel(model="fsmn-vad")`；两个模型常驻执行器线程，`stop()` 释放。
- **并发**：所有本地会话排同一个执行器，后到的排队（不做拒绝、不做限流、不做多实例）。

### 8.2 推流与切句

- 字节 → `np.frombuffer(pcm, "<i2").astype(np.float32) / 32768`，按 **600 ms（9600 样本 @16k）** 切块。
- **ASR**：`asr.generate(input=chunk, cache=asr_cache, is_final=False, chunk_size=[0,10,5],
  encoder_chunk_look_back=4, decoder_chunk_look_back=1)` 拿**本块增量**，拼进「当前句」。
- **VAD**：同块音频喂 `vad.generate(input=chunk, cache=vad_cache, is_final=False, chunk_size=...)`，
  返回 `res[0]["value"] = [[beg_ms, end_ms], ...]`。
  **只有 `end != -1`（闭合）才算一段语音结束** → 把「当前句」以 `is_final=True` 吐出，
  并用 `cache = {}` 重开一段；`-1` 是**未闭合占位，不能当时间戳**。
- **`finish()`**：对 ASR 与 VAD 各传一次 `is_final=True` 冲尾，吐出最后一段定稿。
- 段级 `begin_ms` / `end_ms` 用 VAD 的段边界填。

### 8.3 能力差（明确记录）

| 字段 | 远程 | 本地 |
| --- | --- | --- |
| `text` / `is_final` | ✅ 句级 | ✅（由 VAD 切句模拟） |
| `begin_ms` / `end_ms` | ✅ | ✅（VAD 段边界） |
| `words[]` | ✅ | ❌ 恒 `[]` |
| `emo_tag` / `emo_confidence` | 仅 8k-v2 | ❌ 恒 `None` |

---

## 9. 错误树与失败语义

| 类型 | 触发条件 |
| --- | --- |
| `ASRConfigError` | 后端未启用（远端无 `api_key` / 本地 `enabled: false`）；音频组合该后端不支持（如本地收 `mp3`） |
| `ASRSessionError` | 会话状态非法：`finish()` 后 `push()`、重复 `finish()`、未打开就 `push` |
| `ASRConnectionError` | 握手失败 / WS 中途断开 / 连不上端点 |
| `ASRTimeoutError` | 握手超时、等 `task-started` 超时 |
| `ASRRequestError` | 远端 `task-failed`（带 `error_code` / `error_message` 两个属性） |
| `ASRResponseError` | 帧不合法：非 JSON、结构缺 `sentence`、字段类型不符 |
| `ASRLocalError` | 本地模型加载失败（下载 / 名字 / 显存）或推理抛异常 |

- 全部继承 `ASRError`，调用方一次 `except ASRError` 兜住整层。
- 原始异常挂在 `__cause__`（远程为 `httpx2` / `httpx2.websockets` 异常，本地为 torch/FunASR 异常）。
- **不兜底**（对齐 decision）：一头失败就报错，层内不换头、不自动重连；重试与换头由调用方决定。
- `push(b"")` 静默忽略，因此**不设** `ASRInputError`。

---

## 10. 服务装配与健康检查（`service.py`）

- `ASRService(Service)`，`name = "asr"`，`__init__(config: ASRSettings)` 只赋值。
- `start()`：
  - 远端可用（`api_key` 非空）时建**一个共享的 `httpx2.AsyncClient`**，会话复用它（省连接池）；
    远端不可用只警告。
  - **本地不在 `start()` 里加载模型**（懒加载），也不因本地未启用而报错。
- `stop()`：关共享客户端；本地执行器若有则 `shutdown`、模型引用置空。**幂等**。
- `session(backend=None, format=None, sample_rate=None)` → 未打开的 `ASRSession`；
  `backend=None` 取配置默认（`local`）。
- `health()`：
  - `healthy` = 「远端有 `api_key`」或「本地已启用」二者之一（同 `llm` 的「两头全没才 unhealthy」口径）。
  - `extra`：`默认后端` / `远端模型` / `远端端点` / `本地模型` / `本地设备` / `本地已加载`。
  - detail：跑着但两头都不可用 → 与调用时报错同一句话；未启动 / 已停止 → `not_running_detail(state)`。
  - **不发请求**（健康检查不该有副作用、花钱、受网络抖动影响）。
- 注册：`core/service/registry.py` 的 `build_manager()` 加 `ASRService`。
- `README.md`：补「语音识别层」章节、配置表、目录结构、依赖方向。

---

## 11. 归一化缺口（本次设计的核心难点）

两头的原生口径**并不对齐**，这是本层存在的真正理由：

| 维度 | DashScope 远程 | FunASR 本地 |
| --- | --- | --- |
| 文本口径 | **句内累积**（同一句多次刷新，`text` 越来越长） | **块级增量**（每块只给新增） |
| 句边界 | **原生**（`sentence_end`，VAD 或语义断句） | **无**（流式模型不给句边界） |
| 时间戳 | 句级 + 字级 | 需另挂 VAD（段级）；字级拿不到 |
| 情感 | 仅 8k-v2 | 无 |

本层的统一口径：

1. **`text` 一律是「当前句至今的完整文本」**（累积）——远端天然如此；本地由适配器把增量拼起来。
2. **`is_final` 一律表示「该句已定稿」**——远端来自 `sentence_end`；本地来自 `fsmn-vad` 的段闭合，
   并在 `finish()` 时冲尾定稿最后一段。
3. 拿不到的字段留空（`[]` / `None`），**不伪造**。

---

## 12. 测试策略

判据与既有各层一致：`uvx basedpyright core tests` = `0 errors, 0 warnings, 0 notes`；
`pytest --cov=core` = **100%**（`core/main.py` omit）；`ruff check` / `format --check` 全绿。

- **远程头不打桩库内部**：用 `httpx2` 自带的 `ASGIWebSocketTransport` 挂一个**手写的 ASGI 假 DashScope**
  （纯 ASGI 三元组，不引 `starlette` / `fastapi`），断言 `run-task` 字段、音频帧顺序、`finish-task`，
  并驱动 `task-started` / `result-generated` / `task-failed` / 中途断连四类路径。协议层是**真跑**的。
- **本地头白盒注入**假 `AutoModel`（ASR 返增量、VAD 返含 `-1` 的 `[[beg, end]]`）与假执行器；
  真实模型只在手动验收时跑，不进 CI（几百 MB + 首次下载）。
- **会话对象单测**：`finish` 后 `push` 报错、双方向错误传播（同一异常）、`aclose` 幂等、
  `async with` 兜底 finish、空块静默忽略。
- **配置守护**：YAML 骨架键名与 `ASRSettings` 字段一致（防「多写段/缩进错静默失效」）。

---

## 13. 非目标（本次明确不做）

- TTS（语音合成）；
- 音频解码 / 重采样（调用方负责，层内一律不转码）；
- 说话人分离（diarization）；
- 热词 / 词表管理（`vocabulary_id` 留待以后）；
- 「边说边回话」的 TTS 回路编排（属业务层）；
- 音频落盘 / 存档；
- 自建远端 WS 服务端；
- OpenAI Realtime 等其它家（只做 DashScope 远端 + FunASR 本地）。

---

## 14. 阶段划分

1. **期一**：`wsproto` 依赖 + `ASRSettings`/YAML + 错误树 + `models` + `Kernel`/`build_kernel` +
   `ASRSession` + **远程适配器** + `ASRService` 装配与健康检查 + README。跑通云端一条全链路。
2. **期二**：`funasr` + `torch` 依赖 + **本地适配器**（懒加载、单线程执行器、VAD 切句）+ 本地测试。
3. **期三**：端到端验收（真实 DashScope key 跑一段 wav；本地跑一段 16k pcm）+ 文档收尾。

---

## 15. 风险与动工前必须实测的点

**风险**：

| 风险 | 说明 |
| --- | --- |
| `torch` / `funasr` 体积 | 数百 MB，且首次使用会从 ModelScope 下载模型；CI 不跑真模型 |
| 情感与 8k 互斥 | 想要情感就必须整条线降到 8k，与 16k 精度不可兼得 |
| 长静音断连 | `heartbeat: false` 时远端会因超时 `task-failed`（由调用方处理或改配置开启心跳） |
| 地域限制 | Paraformer 仅华北 2（北京）；官方推荐迁到 `{WorkspaceId}.cn-beijing.maas.aliyuncs.com` 专属域名 |
| 两适配器差异 | `words` / `emo` 在本地长期为空，调用方必须容忍 |

**动工前必须实测的点**（写进文档不装懂，实施时逐条验）：

1. 装上 `wsproto` 后 `aconnect_ws` / `AsyncClient.websocket` 的**真实取用方式**，
   以及逃出 `async with` 的 `ExceptionGroup` 具体形态与 `except*` 处理；
2. `fsmn-vad` 流式 `chunk_size` 的取值，以及它与 ASR 600 ms 粒度的**缓冲对齐**方式；
3. `paraformer-realtime-v2` 是否**真接受任意采样率**（文档如此声称，需实测）；
4. `task-started` 的**实际等待上限**与超时表现；
5. `AsyncWebSocketSession` 在 `push` 与 `events` 并发使用时的安全性
   （内部有 `_write_lock`，但需实测确认 send 与 receive 并发无碍）。

---

## 附：协议与事实查证记录（2026-10-03）

**DashScope Paraformer 实时**

- URL：`wss://dashscope.aliyuncs.com/api-ws/v1/inference`（官方推荐迁往
  `wss://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference`）。
- 鉴权：握手期 `Authorization: bearer <API_KEY>`；无效/缺失 → HTTP 401/403。
- 客户端事件：`run-task`（`header{action, task_id(uuid), streaming:"duplex"}`；
  `payload{task_group:"audio", task:"asr", function:"recognition", model, input:{}, parameters}`）、
  `finish-task`（同名 `task_id`）。
- `parameters` 可选：`vocabulary_id`、`disfluency_removal_enabled`、`language_hints`、
  `semantic_punctuation_enabled`(v2)、`max_sentence_silence`(v2, 默认 1300, [200,6000])、
  `multi_threshold_mode_enabled`(v2)、`punctuation_prediction_enabled`(v2, 默认 true)、
  `heartbeat`(v2, 默认 false)、`inverse_text_normalization_enabled`(v2, 默认 true)。
- 采样率：`paraformer-realtime-v2` 任意；`v1` 仅 16000；`8k-v2` / `8k-v1` 仅 8000。
- 音频：`pcm`/`wav`/`mp3`/`opus`/`speex`/`aac`/`amr`；`opus`/`speex` 须 Ogg 封装；`wav` 须 PCM 编码；
  `amr` 仅 AMR-NB；单声道 16-bit；推荐 3200 字节 ≈ 100 ms。
- 服务端事件：`task-started` / `result-generated` / `task-finished` / `task-failed`
  （失败连接关闭且不可复用；含 `error_code` / `error_message`）。
- `result-generated.payload.output.sentence`：`begin_time`、`end_time`、`text`、`heartbeat`、
  `sentence_end`、`emo_tag`、`emo_confidence`、`words[{begin_time, end_time, text, punctuation}]`；
  `payload.usage.duration` 仅在 `sentence_end=true` 时给出（秒）。
- 情感：仅 `paraformer-realtime-8k-v2`，需 `semantic_punctuation_enabled=false`，且仅 `sentence_end=true` 时给。

**FunASR 流式**

- `res[0]["text"]` 是**本块增量**；`cache` 必须跨块复用（首次 `{}`）且不得读写内部字段；
  最后一块 `is_final=True` 负责冲尾（否则丢尾字）。
- `chunk_size=[0,10,5]` 单位是 60ms 帧：当前 600ms + 右看 300ms ≈ 900ms 延迟；
  `chunk_stride = chunk_size[1] * 960 = 9600` 样本 @16k = 600 ms。
- `encoder_chunk_look_back=4`、`decoder_chunk_look_back=1`。
- 采样率固定 16k（`960` 是按 16k 算的）。
- 流式模型**不产出句边界/标点**；标点需另挂 `ct-punc`（本次不挂）。

**FunASR `fsmn-vad` 流式**

- `res[0]["value"] = [[beg_ms, end_ms], ...]`，时间相对**整条流**起点；
  **未闭合的一侧为 `-1` 占位**，不能当时间戳使用。

**httpx2 WebSocket**

- 由可选 extra `httpx2[ws]` 提供，唯一依赖 `wsproto>=1.2`（**当前环境未装**）。
- `AsyncClient.websocket(...)` → `AsyncGenerator[AsyncWebSocketSession]`；
  `httpx2.websockets.aconnect_ws(...)` 是 `@asynccontextmanager`。
- `AsyncWebSocketSession` 是 `anyio.AsyncContextManagerMixin`，内部 `anyio` task group +
  memory object stream；API 有 `send_text` / `send_bytes` / `send_json` / `receive_text` /
  `receive_bytes` / `receive_json` / `ping` / `close`；异常 `WebSocketDisconnect` /
  `WebSocketNetworkError` / `WebSocketUpgradeError` / `WebSocketInvalidTypeReceived` / `HTTPXWSException`。
- **逃出 `async with` 作用域的异常会被包成 `ExceptionGroup`**（需 `except*`）。
