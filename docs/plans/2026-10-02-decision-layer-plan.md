# 判断层（Laya / Jev）实施计划

> **给执行者：** 本计划按 TDD 逐任务执行。设计依据是 `docs/plans/2026-10-02-decision-layer-design.md`，实施中若与设计冲突，以设计文档为准并在本文末尾「实施补记」里记下偏差。

**目标：** 新增 `core/decision/` 层，把「让模型做一次判断」收成一个服务：`predict` 走 `/systemone` 协议批量提问，另加 `choose` / `rate` / `ask` 三个便捷方法。

**架构：** 两个端点（`jev` 云端、`laya` 本地 `laya-serve`）共用同一套线协议，因此复用 `llm` 层的「双端点 + 惰性建客户端 + 缺配置不 fail fast」形态。传输用 `httpx.AsyncClient`，路径常量 `/systemone`。

**技术栈：** Python 3.12 / uv / pydantic / httpx（随 `openai` 已在环境里）/ pytest + pytest-asyncio / basedpyright / ruff。

**每个任务结束都要跑这三条判据，全绿才提交：**

```bash
cd .worktrees/decision
uv run ruff check . && uv run ruff format --check .
uvx basedpyright core tests
.venv/bin/python -m pytest | tail -3
```

（`.venv/bin/python -m pytest` 而非 `uv run pytest`：`uv run` 在并发时会争锁。）

---

### Task 1: 配置层接入 `DecisionSettings`

**Files:**
- Modify: `core/config/settings.py`（`Settings` 之后、`_describe_source` 之前插入两个类）
- Modify: `core/config/__init__.py`
- Modify: `pyproject.toml`
- Modify: `data/config/cosmos.yaml`
- Modify: `.env.example`
- Test: `tests/test_config_loader.py`（追加两个用例）

**Step 1: 写失败的测试**

追加到 `tests/test_config_loader.py` 末尾：

```python
def test_判断层配置节点缺省值() -> None:
    settings = Settings()

    assert settings.decision.jev.enabled is False
    assert settings.decision.jev.base_url == "https://api.typesafe.ai/v1"
    assert settings.decision.jev.model == "jev-latest"
    assert settings.decision.laya.model == ""
    assert settings.decision.laya.base_url == "http://127.0.0.1:8000/v1"
    assert settings.decision.jev.retries == 2


def test_判断层配置可被yaml覆盖(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "cosmos.yaml"
    _ = config.write_text(
        "decision:\n  laya:\n    enabled: true\n    model: multilingual\n    timeout: 12.5\n",
        encoding="utf-8",
    )

    settings = load_settings(config_file=config, env_file=tmp_path / ".env")

    assert settings.decision.laya.enabled is True
    assert settings.decision.laya.model == "multilingual"
    assert settings.decision.laya.timeout == 12.5


def test_判断层超时必须为正数(tmp_path: Path) -> None:
    config = tmp_path / "cosmos.yaml"
    _ = config.write_text("decision:\n  jev:\n    timeout: 0\n", encoding="utf-8")

    with pytest.raises(ValidationError):
        _ = load_settings(config_file=config, env_file=tmp_path / ".env")
```

（`Path` / `pytest` / `ValidationError` / `load_settings` / `Settings` 在该文件里已 import；没有就补上。）

**Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_config_loader.py -k 判断层 -v`
Expected: FAIL —— `AttributeError: 'Settings' object has no attribute 'decision'`

**Step 3: 实现**

`core/config/settings.py`，在 `LLMSettings` 之后插入：

```python
#: 判断层的两个端点名。laya 是本地 laya-serve，jev 是 TypeSafe 云端。
type Backend = Literal["jev", "laya"]


class DecisionEndpointSettings(BaseModel):
    """一个判断端点的配置。

    `model` 为空表示「请求体里不带 model 字段」：Laya 靠这个走 Router 自动路由，
    Jev 则会 422（它的 model 是必填）。传空串或 null 都不等于「不传」。
    """

    enabled: bool = False
    base_url: str = ""
    api_key: str = ""  # 留空则不发 Authorization 头（laya-serve 默认不要求认证）
    model: str = ""
    timeout: float = Field(default=60.0, gt=0)  # 单次请求超时（秒）
    retries: int = Field(default=2, ge=0)  # 429/529 退避重试次数，0 关闭


class DecisionSettings(BaseModel):
    """判断层配置：jev 云端与 laya 本地是两个端点，字段相同、协议相同。"""

    jev: DecisionEndpointSettings = DecisionEndpointSettings(
        base_url="https://api.typesafe.ai/v1", model="jev-latest"
    )
    laya: DecisionEndpointSettings = DecisionEndpointSettings(
        base_url="http://127.0.0.1:8000/v1", model=""
    )
```

`Settings` 加一行 `decision: DecisionSettings = DecisionSettings()`（放在 `llm` 之后）。

`core/config/__init__.py`：from-import 里加 `Backend, DecisionEndpointSettings, DecisionSettings`（保持原字母序），`__all__` 同样加三个名字。**插入后跑 `uv run ruff check core/config/__init__.py`，若报 RUF022 按提示调整顺序**（该文件的排序是常量 → 类型 → 函数，与纯 ASCII 序不同）。

`pyproject.toml` 的 `dependencies` 加：

```toml
    "httpx>=0.28",
```

`data/config/cosmos.yaml` 末尾追加：

```yaml

# 判断层：jev（云端）与 laya（本地 laya-serve）是两个端点，同一套 /systemone 协议。
# base_url 只写到 /v1 为止，路径 /systemone 由代码补上，多写一段会 404。
# 两个端点默认都关着：进程照常启动（不 fail fast），调用时才报未启用。
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

`.env.example` 追加两行（放在 `DASHSCOPE_API_KEY` 之后）：

```dotenv
# 判断层：Jev 云端密钥（不配则该端点不可用）
TYPESAFE_API_KEY=
# 判断层：本地 laya-serve 的密钥，未设 LAYA_API_KEY 时留空
LAYA_API_KEY=
```

**Step 4: 验证**

先确认 `load_settings()` 真能读回 `decision` 段（`extra="ignore"` 会让写错的段静默失效）：

```bash
uv run python -c "from core.config import load_settings; s=load_settings(); print(s.decision.model_dump_json())"
```

Expected: 打印出 `{"jev": {...}, "laya": {...}}`，不是空的对象。

Run: `.venv/bin/python -m pytest tests/test_config_loader.py -v`
Expected: PASS（含新增 3 条）

**Step 5: 提交**

```bash
git add pyproject.toml core/config/settings.py core/config/__init__.py data/config/cosmos.yaml .env.example tests/test_config_loader.py
git commit -m "feat(config): 新增判断层双端点配置"
```

---

### Task 2: 三原语构造器 `questions.py`

**Files:**
- Create: `core/decision/__init__.py`（本任务先只导出构造器与类型，后续任务逐步补齐）
- Create: `core/decision/questions.py`
- Test: `tests/test_decision_questions.py`

**Step 1: 写失败的测试**

```python
"""三原语构造器测试。"""

from __future__ import annotations

from core.decision import choice, noul, score


def test_choice_带选项描述() -> None:
    question = choice("归哪个部门？", {"billing": "账单", "other": None})

    assert question == {
        "type": "choice",
        "instructions": "归哪个部门？",
        "criteria": {"billing": "账单", "other": None},
    }


def test_score_保持级序() -> None:
    question = score("愤怒程度？", ["平静", "不满", "愤怒"])

    assert question["type"] == "score"
    assert question["criteria"] == ["平静", "不满", "愤怒"]


def test_noul_两个描述都给才带标准() -> None:
    question = noul("是否要求退款？", true="明确要求", false="没提")

    assert question["type"] == "noul"
    assert question["criteria"] == {"true": "明确要求", "false": "没提"}


def test_noul_只给一个描述时不带标准() -> None:
    question = noul("是否要求退款？", true="明确要求")

    assert "criteria" not in question


def test_构造器不校验上限() -> None:
    # 选项数上限是端点的事（Jev 255 / Laya 100），本地按任一家的规则拦都会误伤另一家
    question = choice("选一个", {f"opt{index}": None for index in range(300)})

    assert len(question["criteria"]) == 300
```

**Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_decision_questions.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'core.decision'`

**Step 3: 实现**

`core/decision/questions.py`：

```python
"""三原语构造器。

约定：
- 调用方只碰这里的构造器，不手拼协议字典。
- 每个构造器返回**具体的 TypedDict**（不是 `Question` 联合体）：
  联合体会放过拼错的键，具体类型才能在构造处报错。
- 构造器只做「填好字面量」，不做上限校验：选项数（Jev 255 / Laya 100）、
  score 级数（2–10）由端点裁决，本地按任一家的规则拦都会误伤另一家。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal, Required, TypedDict


class ChoiceQuestion(TypedDict):
    """从离散选项里挑一个。criteria 的值是选项的评分说明，`None` 表示不需要说明。"""

    type: Literal["choice"]
    instructions: str
    criteria: dict[str, str | None]


class ScoreQuestion(TypedDict):
    """在有序档位上打分：criteria 的顺序就是级序。"""

    type: Literal["score"]
    instructions: str
    criteria: list[str]


class NoulCriteria(TypedDict):
    """是非题的两个语义槽：只能用 true / false 这两个键。"""

    true: str
    false: str


class NoulQuestion(TypedDict, total=False):
    """是非题：返回 P(true)。`criteria` 可选，给了就必须两个槽都填。"""

    type: Required[Literal["noul"]]
    instructions: Required[str]
    criteria: NoulCriteria


#: 一次请求里 questions 的值：三原语可以混在同一趟里问完
type Question = ChoiceQuestion | ScoreQuestion | NoulQuestion


def choice(instructions: str, criteria: Mapping[str, str | None]) -> ChoiceQuestion:
    """选一个：criteria 的键就是候选选项。"""
    return {
        "type": "choice",
        "instructions": instructions,
        "criteria": dict(criteria),
    }


def score(instructions: str, criteria: Sequence[str]) -> ScoreQuestion:
    """打分：criteria 的顺序即级序，最低档在前。"""
    return {"type": "score", "instructions": instructions, "criteria": list(criteria)}


def noul(instructions: str, *, true: str | None = None, false: str | None = None) -> NoulQuestion:
    """是非题：两个语义槽都给才带 criteria，只给一个等于给一半。"""
    question = NoulQuestion(type="noul", instructions=instructions)
    if true is not None and false is not None:
        question["criteria"] = NoulCriteria(true=true, false=false)
    return question
```

`core/decision/__init__.py`：

```python
"""判断层对外出口。

用法：
    from core.decision import DecisionService, choice, noul, score

    result = await svc.predict("这条消息是否要求退款？", {"退款": noul("是否要求退款？")})

两家的协议都是同一套 /systemone，laya 与 jev 只是两个端点，用 backend= 选。
"""

from core.decision.questions import (
    ChoiceQuestion,
    NoulCriteria,
    NoulQuestion,
    Question,
    ScoreQuestion,
    choice,
    noul,
    score,
)

__all__ = [
    "ChoiceQuestion",
    "NoulCriteria",
    "NoulQuestion",
    "Question",
    "ScoreQuestion",
    "choice",
    "noul",
    "score",
]
```

**Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_decision_questions.py -v`
Expected: PASS（5 条）

**Step 5: 提交**

```bash
git add core/decision/__init__.py core/decision/questions.py tests/test_decision_questions.py
git commit -m "feat(decision): 新增三原语问题构造器"
```

---

### Task 3: 错误树与客户端工厂

**Files:**
- Create: `core/decision/errors.py`
- Create: `core/decision/client.py`
- Modify: `core/decision/__init__.py`（补导出）
- Test: `tests/test_decision_client.py`

**Step 1: 写失败的测试**

```python
"""判断层客户端工厂测试。"""

from __future__ import annotations

import httpx
import pytest

from core.config import DecisionEndpointSettings
from core.decision import (
    DecisionConfigError,
    DecisionConnectionError,
    DecisionError,
    DecisionRequestError,
    DecisionResponseError,
    DecisionTimeoutError,
    build_client,
)


def test_有密钥时带鉴权头() -> None:
    endpoint = DecisionEndpointSettings(base_url="https://api.typesafe.ai/v1", api_key="sk-x")

    client = build_client(endpoint)

    assert client.headers["authorization"] == "Bearer sk-x"
    assert str(client.base_url) == "https://api.typesafe.ai/v1"


def test_没密钥时不带鉴权头() -> None:
    endpoint = DecisionEndpointSettings(base_url="http://127.0.0.1:8000/v1")

    client = build_client(endpoint)

    assert "authorization" not in client.headers


def test_超时按配置传给客户端() -> None:
    endpoint = DecisionEndpointSettings(base_url="http://127.0.0.1:8000/v1", timeout=12.5)

    client = build_client(endpoint)

    assert isinstance(client.timeout, httpx.Timeout)


def test_错误都继承自基类() -> None:
    assert issubclass(DecisionConfigError, DecisionError)
    assert issubclass(DecisionRequestError, DecisionError)
    assert issubclass(DecisionTimeoutError, DecisionError)
    assert issubclass(DecisionConnectionError, DecisionError)
    assert issubclass(DecisionResponseError, DecisionError)


def test_请求错误带对账坐标() -> None:
    error = DecisionRequestError(
        "端点返回 422", status_code=422, endpoint="http://x/v1", model="m", request_id="r"
    )

    assert (error.status_code, error.endpoint, error.model, error.request_id) == (
        422,
        "http://x/v1",
        "m",
        "r",
    )
```

**Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_decision_client.py -v`
Expected: FAIL —— `ImportError: cannot import name 'build_client'`

**Step 3: 实现**

`core/decision/errors.py`：

```python
"""判断层错误树。

约定：
- 全部继承 DecisionError，调用方可以一次捕获整个判断层。
- 原始 httpx 异常挂在 __cause__ 上，需要排查端点返回体时顺着 cause 找。
- 不在 ServiceError 树下：调用失败是业务运行期错误，不该让进程 fail fast。
"""

from __future__ import annotations


class DecisionError(Exception):
    """判断层错误基类。"""


class DecisionConfigError(DecisionError):
    """配置导致的能力不可用：端点没启用。

    无 cause：这不是调用失败，是根本没有发起调用。
    """


class DecisionRequestError(DecisionError):
    """端点返回 4xx / 5xx。

    带上对账需要的三个坐标（状态码、端点、模型）与端点给的 request_id。
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        endpoint: str,
        model: str,
        request_id: str | None,
    ) -> None:
        self.status_code: int = status_code
        self.endpoint: str = endpoint
        self.model: str = model
        self.request_id: str | None = request_id
        super().__init__(message)


class DecisionTimeoutError(DecisionError):
    """请求超时。"""


class DecisionConnectionError(DecisionError):
    """连不上端点（DNS、TLS、连接被拒等），超时除外。"""


class DecisionResponseError(DecisionError):
    """返回体不可用：不是合法 JSON、顶层不是对象、缺 answers、便捷方法要的字段不在。"""
```

`core/decision/client.py`：

```python
"""判断层客户端工厂。

约定：这是全仓库唯一 new 出 httpx 客户端的地方，也是测试注入替身的接缝。
base_url 只写到 /v1 为止（两家的完整地址都是 …/v1/systemone），路径由 SYSTEMONE_PATH 补。
"""

from __future__ import annotations

import httpx

from core.config import DecisionEndpointSettings

#: /systemone 是 jev 与 laya-serve 共用的线协议路径
SYSTEMONE_PATH = "/systemone"


def build_client(endpoint: DecisionEndpointSettings) -> httpx.AsyncClient:
    """按端点配置构造异步客户端。

    api_key 为空时不发 Authorization 头：laya-serve 不设 LAYA_API_KEY 时本来就不要求认证。
    """
    headers = {"Authorization": f"Bearer {endpoint.api_key}"} if endpoint.api_key else {}
    return httpx.AsyncClient(
        base_url=endpoint.base_url,
        headers=headers,
        timeout=endpoint.timeout,
    )
```

`core/decision/__init__.py` 在 `__all__` 与 import 里补上：`DecisionConfigError`、`DecisionConnectionError`、`DecisionError`、`DecisionRequestError`、`DecisionResponseError`、`DecisionTimeoutError`、`SYSTEMONE_PATH`、`build_client`。

**Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_decision_client.py -v`
Expected: PASS（5 条）

**Step 5: 提交**

```bash
git add core/decision/errors.py core/decision/client.py core/decision/__init__.py tests/test_decision_client.py
git commit -m "feat(decision): 新增错误树与客户端工厂"
```

---

### Task 4: `DecisionService.predict` 主路径

**Files:**
- Create: `core/decision/service.py`
- Modify: `core/decision/__init__.py`（补导出）
- Test: `tests/test_decision_service.py`

**Step 1: 写失败的测试**

```python
"""判断服务测试。

约定：
- 客户端在 start() 里建，容器又不认它，所以除「未启用」那条外一律白盒塞 service._clients。
- 用 httpx.MockTransport 注入 handler：不起 mock server、不打桩 socket，
  handler 直接拿到 httpx.Request，可断言请求体与请求头（本层最值得断言的东西）。
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from core.config import DecisionSettings
from core.decision import DecisionService, DecisionResult, noul
from core.decision.service import _QID


def _handler(body: dict[str, object] | None = None, status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    """造一个固定返回的 handler。"""
    payload = body if body is not None else {
        "model": "laya-multilingual",
        "answers": {_QID: {"type": "noul", "noul": 0.93}},
        "usage": {"input_tokens": 11, "output_tokens": 0},
    }
    return lambda request: httpx.Response(status, json=payload)


def _service(handler: Callable[[httpx.Request], httpx.Response], **overrides: object) -> DecisionService:
    """构造一个已「启动」的服务：客户端直接白盒塞进去。"""
    settings = DecisionSettings()
    service = DecisionService(settings)
    service._clients = {  # pyright: ignore[reportPrivateUsage]
        "laya": httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=settings.laya.base_url
        )
    }
    return service


async def test_predict_请求体与返回体() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "model": "laya-multilingual",
                "answers": {"退款": {"type": "noul", "noul": 0.93}},
                "routing": {"model": "multilingual"},
                "usage": {"input_tokens": 11, "output_tokens": 0},
            },
        )

    service = _service(handler)

    result = await service.predict("我要退款", {"退款": noul("是否要求退款？")})

    assert isinstance(result, DecisionResult)
    assert result.model == "laya-multilingual"
    assert result.answers["退款"] == {"type": "noul", "noul": 0.93}
    assert (result.input_tokens, result.output_tokens) == (11, 0)
    assert result.routing == {"model": "multilingual"}
    assert seen[0].url.path == "/v1/systemone"
    assert seen[0].method == "POST"


async def test_laya默认端点且model为空时不带该字段() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"model": "m", "answers": {}, "usage": {}})

    service = _service(handler)

    _ = await service.predict("state", {})

    assert "model" not in json.loads(seen[0].content)


async def test_jev端点带上配置的model() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"model": "jev-1.13.0", "answers": {}, "usage": {}})

    service = _service(handler)
    service._clients["jev"] = httpx.AsyncClient(  # pyright: ignore[reportPrivateUsage]
        transport=httpx.MockTransport(handler), base_url="https://api.typesafe.ai/v1"
    )

    _ = await service.predict("state", {}, backend="jev")

    assert json.loads(seen[0].content)["model"] == "jev-latest"


async def test_usage缺失时不报错() -> None:
    service = _service(lambda request: httpx.Response(200, json={"model": "m", "answers": {}}))

    result = await service.predict("state", {})

    assert (result.input_tokens, result.output_tokens) == (None, None)


async def test_未启用端点调用报配置错误() -> None:
    service = DecisionService(DecisionSettings())

    with pytest.raises(DecisionConfigError) as excinfo:
        _ = await service.predict("state", {})

    assert excinfo.value.__cause__ is None
```

（文件头补 `import json`、`from core.decision import DecisionConfigError`。）

**Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_decision_service.py -v`
Expected: FAIL —— `ImportError: cannot import name 'DecisionService'`

**Step 3: 实现**

`core/decision/service.py`（完整）：

```python
"""判断服务：把「让模型做一次判断」收成一个服务，业务代码不直接接触 HTTP。

约定：
- 两个端点（jev 云端 / laya 本地）走同一套 /systemone 协议，字段完全相同。
- 调用方显式传 backend 选端点，默认 laya。层内不做兜底——两个后端的 confidence
  语义不同（Jev 是 (n·p_max-1)/(n-1)，Laya 是 1-归一化熵），跨端点比较必然出错。
- predict 的 answers 原样透传：两家的 answer 并不同构（Laya 多 answer_confidence /
  abstention），建模会把这些字段静默吃掉。
- 日志不记 state 与 questions 正文。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import ClassVar, cast, override

import httpx

from core.config import Backend, DecisionEndpointSettings, DecisionSettings
from core.decision.client import SYSTEMONE_PATH, build_client
from core.decision.errors import (
    DecisionConfigError,
    DecisionConnectionError,
    DecisionRequestError,
    DecisionResponseError,
    DecisionTimeoutError,
)
from core.decision.questions import Question, choice, noul, score
from core.service.base import HealthStatus, Service, not_running_detail

#: 没启用任何端点时的统一措辞：调用报错与健康检查详情共用
NO_ENDPOINT = "未启用任何判断端点，判断功能不可用"

#: 需要退避重试的状态码：429 限流、529 过载（Jev 文档里的码）
RETRY_STATUS: frozenset[int] = frozenset({429, 529})

#: 退避基数（秒）：delay = _BACKOFF_BASE * 2 ** 已重试次数。测试 patch 成 0 用。
_BACKOFF_BASE = 0.5

#: 便捷方法内部用的问题 id
_QID = "answer"

#: state 就是任意 JSON：纯文本、业务记录、聊天记录、数组都可以
type State = str | dict[str, object] | list[object]


@dataclass(frozen=True, slots=True)
class DecisionResult:
    """一次 predict 的结果。

    answers 原样透传协议结构（键与 questions 一一对应）：两家 answer 并不同构，
    这一层不做承诺，调用方按键取。routing 只有 Laya 返回。
    """

    model: str
    answers: dict[str, dict[str, object]]
    input_tokens: int | None
    output_tokens: int | None
    routing: dict[str, object] | None


@dataclass(frozen=True, slots=True)
class ChoiceAnswer:
    """choice 原语的答案：选中的选项、完整概率分布与置信度。"""

    choice: str
    probabilities: dict[str, float]
    confidence: float


@dataclass(frozen=True, slots=True)
class ScoreAnswer:
    """score 原语的答案：概率加权得分、档位说明、分布与置信度。"""

    score: float
    legend: dict[str, str]
    probabilities: dict[str, float]
    confidence: float


def _elapsed_ms(begin: float) -> float:
    """从 begin（`time.perf_counter()` 的取值）到现在经过的毫秒数。"""
    return round((time.perf_counter() - begin) * 1000, 1)


def _int_or_none(value: object) -> int | None:
    """usage 字段可能缺失，非整数一律当「没给」。"""
    return value if isinstance(value, int) else None


@asynccontextmanager
async def _wrap_errors(endpoint: str, model: str) -> AsyncGenerator[None, None]:
    """把 httpx 异常换成判断层错误。

    except 顺序是硬要求：httpx 的 TimeoutException 是 TransportError 的子类，
    先捕子类才不会把超时一律误判成连接错误。
    """
    try:
        yield
    except httpx.TimeoutException as exc:
        raise DecisionTimeoutError(f"请求 {endpoint} 超时：{exc}") from exc
    except httpx.TransportError as exc:
        raise DecisionConnectionError(f"连接 {endpoint} 失败：{exc}") from exc


class DecisionService(Service):
    """判断服务：持有两个端点的客户端，暴露批量提问与三个便捷方法。"""

    name: ClassVar[str] = "decision"

    def __init__(self, config: DecisionSettings) -> None:
        super().__init__()
        self._config: DecisionSettings = config
        self._clients: dict[Backend, httpx.AsyncClient] = {}

    # ---------- 生命周期 ----------

    @override
    async def start(self) -> None:
        """只为启用的端点建客户端。未启用是用户的显式选择，不警告。"""
        if self.running:
            return
        self._clients = {
            name: build_client(settings)
            for name, settings in self._endpoints()
            if settings.enabled
        }

    @override
    async def stop(self) -> None:
        """关掉两个客户端。必须幂等：回滚时它也会作用在没建过客户端的实例上。"""
        clients, self._clients = self._clients, {}
        for client in clients.values():
            await client.close()

    @override
    async def health(self) -> HealthStatus:
        """只看有没有客户端，不发请求。

        两个端点都没启用才算不健康：只启用一头也是能用的。extra 里把两端的
        模型与地址都带出来——「未启用」时更要靠它看出是哪一头没开。
        """
        extra: dict[str, object] = {}
        for name, settings in self._endpoints():
            extra[f"{name}模型"] = settings.model
            extra[f"{name}端点"] = settings.base_url
        if self._clients:
            return HealthStatus(
                name=self.label, healthy=True, state=self.state, detail="", extra=extra
            )
        # 没客户端分两种：跑着但一个端点都没启用（问题在配置），以及还没启动 / 已停止
        # （问题在状态）。后者套用 NO_ENDPOINT 会误导。
        detail = NO_ENDPOINT if self.running else not_running_detail(self.state)
        return HealthStatus(
            name=self.label, healthy=False, state=self.state, detail=detail, extra=extra
        )

    # ---------- 调用能力 ----------

    async def predict(
        self, state: State, questions: Mapping[str, Question], *, backend: Backend | None = None
    ) -> DecisionResult:
        """一次前向把 questions 里所有问题问完，返回带概率的答案。"""
        name, settings, client = self._endpoint(backend)
        payload: dict[str, object] = {"state": state, "questions": dict(questions)}
        if settings.model:
            payload["model"] = settings.model
        begin = time.perf_counter()
        result = self._parse(await self._request(name, client, settings, payload))
        self._log_done(begin, settings, result, len(questions))
        return result

    async def choose(
        self,
        state: State,
        instructions: str,
        criteria: Mapping[str, str | None],
        *,
        backend: Backend | None = None,
    ) -> ChoiceAnswer:
        """从离散选项里挑一个。"""
        result = await self.predict(state, {_QID: choice(instructions, criteria)}, backend=backend)
        answer = self._answer(result)
        picked = answer.get("choice")
        probabilities = answer.get("probabilities")
        confidence = answer.get("confidence")
        if (
            not isinstance(picked, str)
            or not isinstance(probabilities, dict)
            or not isinstance(confidence, (int, float))
        ):
            raise DecisionResponseError(f"choice 答案字段不完整：{sorted(answer)}")
        return ChoiceAnswer(
            choice=picked,
            probabilities=cast("dict[str, float]", probabilities),
            confidence=float(confidence),
        )

    async def rate(
        self,
        state: State,
        instructions: str,
        criteria: Sequence[str],
        *,
        backend: Backend | None = None,
    ) -> ScoreAnswer:
        """在有序档位上打分。"""
        result = await self.predict(state, {_QID: score(instructions, criteria)}, backend=backend)
        answer = self._answer(result)
        value = answer.get("score")
        legend = answer.get("legend")
        probabilities = answer.get("probabilities")
        confidence = answer.get("confidence")
        if (
            not isinstance(value, (int, float))
            or not isinstance(legend, dict)
            or not isinstance(probabilities, dict)
            or not isinstance(confidence, (int, float))
        ):
            raise DecisionResponseError(f"score 答案字段不完整：{sorted(answer)}")
        return ScoreAnswer(
            score=float(value),
            legend=cast("dict[str, str]", legend),
            probabilities=cast("dict[str, float]", probabilities),
            confidence=float(confidence),
        )

    async def ask(
        self,
        state: State,
        instructions: str,
        *,
        true: str | None = None,
        false: str | None = None,
        backend: Backend | None = None,
    ) -> float:
        """是非题，返回 P(true)。

        返回裸浮点而不是 dataclass：Jev 的 noul 答案只有 {type, noul}，连
        confidence 都没有，包一层没东西可装。
        """
        result = await self.predict(
            state, {_QID: noul(instructions, true=true, false=false)}, backend=backend
        )
        answer = self._answer(result)
        value = answer.get("noul")
        if not isinstance(value, (int, float)):
            raise DecisionResponseError(f"noul 答案字段不合法：{value!r}")
        return float(value)

    # ---------- 内部 ----------

    def _endpoints(self) -> tuple[tuple[Backend, DecisionEndpointSettings], ...]:
        """两个端点，顺序固定。"""
        return (("jev", self._config.jev), ("laya", self._config.laya))

    @staticmethod
    def _settings_of(config: DecisionSettings, name: Backend) -> DecisionEndpointSettings:
        """按端点名取配置：显式分支，不用 getattr（会退化成 Any）。"""
        return config.jev if name == "jev" else config.laya

    def _endpoint(
        self, backend: Backend | None
    ) -> tuple[Backend, DecisionEndpointSettings, httpx.AsyncClient]:
        """挑端点：默认 laya（本地优先：免费、数据不出域）。"""
        name: Backend = "laya" if backend is None else backend
        client = self._clients.get(name)
        if client is None:
            raise DecisionConfigError(f"{name} 端点{NO_ENDPOINT}")
        return name, self._settings_of(self._config, name), client

    async def _request(
        self,
        name: Backend,
        client: httpx.AsyncClient,
        settings: DecisionEndpointSettings,
        payload: dict[str, object],
    ) -> dict[str, object]:
        """发一次请求，429/529 按指数退避重试。"""
        attempt = 0
        while True:
            async with _wrap_errors(settings.base_url, settings.model):
                response = await client.post(SYSTEMONE_PATH, json=payload)
            if response.status_code not in RETRY_STATUS or attempt >= settings.retries:
                return self._body(response, settings)
            delay = _BACKOFF_BASE * 2**attempt
            self.log.warning(
                "判断调用重试",
                端点=name,
                状态码=response.status_code,
                第几次=attempt + 1,
                等待毫秒=round(delay * 1000, 1),
            )
            await asyncio.sleep(delay)
            attempt += 1

    @staticmethod
    def _body(
        response: httpx.Response, settings: DecisionEndpointSettings
    ) -> dict[str, object]:
        """状态码与 JSON 解析的收口：错误码换成 DecisionRequestError，坏返回体换 DecisionResponseError。"""
        if response.status_code >= 400:
            raise DecisionRequestError(
                f"判断端点返回 {response.status_code}",
                status_code=response.status_code,
                endpoint=settings.base_url,
                model=settings.model,
                request_id=response.headers.get("x-request-id"),
            )
        try:
            body = cast("object", response.json())
        except json.JSONDecodeError as exc:
            raise DecisionResponseError(f"返回体不是合法 JSON：{exc}") from exc
        if not isinstance(body, dict):
            raise DecisionResponseError("返回体顶层不是对象")
        return cast("dict[str, object]", body)

    @staticmethod
    def _parse(body: dict[str, object]) -> DecisionResult:
        """把返回体翻译成 DecisionResult。answers 内部不做任何加工。"""
        answers = body.get("answers")
        if not isinstance(answers, dict):
            raise DecisionResponseError("返回体缺少 answers")
        usage = body.get("usage")
        tokens = cast("dict[str, object]", usage) if isinstance(usage, dict) else {}
        routing = body.get("routing")
        return DecisionResult(
            model=str(body.get("model", "")),
            answers=cast("dict[str, dict[str, object]]", answers),
            input_tokens=_int_or_none(tokens.get("input_tokens")),
            output_tokens=_int_or_none(tokens.get("output_tokens")),
            routing=cast("dict[str, object]", routing) if isinstance(routing, dict) else None,
        )

    @staticmethod
    def _answer(result: DecisionResult) -> dict[str, object]:
        """取便捷方法那一题的答案。"""
        answer = result.answers.get(_QID)
        if answer is None:
            raise DecisionResponseError(
                f"返回体里没有 {_QID} 的答案（实到 {sorted(result.answers)}）"
            )
        return answer

    def _log_done(
        self,
        begin: float,
        settings: DecisionEndpointSettings,
        result: DecisionResult,
        question_count: int,
    ) -> None:
        """调用成功日志：只记模型 / 端点 / 问题数 / 耗时 / token，不记正文。"""
        fields: dict[str, object] = {
            "模型": result.model,
            "端点": settings.base_url,
            "问题数": question_count,
            "耗时毫秒": _elapsed_ms(begin),
        }
        if result.input_tokens is not None:
            fields["输入token"] = result.input_tokens
        if result.output_tokens is not None:
            fields["输出token"] = result.output_tokens
        # 显式传 face：fields 的值是 object，**fields 展开时会被逐个形参对账
        self.log.info("判断调用完成", face=None, **fields)
```

`core/decision/__init__.py` 补导出 `ChoiceAnswer`、`DecisionResult`、`DecisionService`、`ScoreAnswer`、`State`。

**Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_decision_service.py -v`
Expected: PASS（5 条）

**Step 5: 提交**

```bash
git add core/decision/service.py core/decision/__init__.py tests/test_decision_service.py
git commit -m "feat(decision): 新增判断服务与 predict 主路径"
```

---

### Task 5: 错误映射与退避重试

**Files:**
- Modify: `tests/test_decision_service.py`（追加用例；实现已在 Task 4 落地，本任务补齐覆盖）

**Step 1: 写测试**

```python
async def test_状态码错误带上对账坐标() -> None:
    service = _service(
        lambda request: httpx.Response(
            422, json={"error": "bad"}, headers={"x-request-id": "req-7"}
        )
    )

    with pytest.raises(DecisionRequestError) as excinfo:
        _ = await service.predict("state", {})

    assert excinfo.value.status_code == 422
    assert excinfo.value.request_id == "req-7"
    assert excinfo.value.endpoint == DecisionSettings().laya.base_url


async def test_超时映射成超时错误() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    with pytest.raises(DecisionTimeoutError):
        _ = await _service(handler).predict("state", {})


async def test_连不上映射成连接错误() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    with pytest.raises(DecisionConnectionError):
        _ = await _service(handler).predict("state", {})


async def test_非法JSON报返回体错误() -> None:
    service = _service(lambda request: httpx.Response(200, content=b"not json"))

    with pytest.raises(DecisionResponseError):
        _ = await service.predict("state", {})


async def test_顶层不是对象报返回体错误() -> None:
    service = _service(lambda request: httpx.Response(200, json=[1, 2]))

    with pytest.raises(DecisionResponseError):
        _ = await service.predict("state", {})


async def test_缺answers报返回体错误() -> None:
    service = _service(lambda request: httpx.Response(200, json={"model": "m"}))

    with pytest.raises(DecisionResponseError):
        _ = await service.predict("state", {})


async def test_429退避后成功() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={})
        return httpx.Response(200, json={"model": "m", "answers": {}, "usage": {}})

    service = _service(handler)

    _ = await service.predict("state", {})

    assert len(calls) == 2


async def test_529退避后成功(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(decision_service, "_BACKOFF_BASE", 0.0)
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(529, json={})
        return httpx.Response(200, json={"model": "m", "answers": {}, "usage": {}})

    _ = await _service(handler).predict("state", {})

    assert len(calls) == 2


async def test_重试耗尽后报错(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(decision_service, "_BACKOFF_BASE", 0.0)
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429, json={})

    with pytest.raises(DecisionRequestError):
        _ = await _service(handler).predict("state", {})

    assert len(calls) == 3  # retries=2 → 总请求数 3


async def test_retries为零时不重试(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(decision_service, "_BACKOFF_BASE", 0.0)
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429, json={})

    service = DecisionService(DecisionSettings(laya=DecisionEndpointSettings(
        enabled=True, base_url="http://laya.test/v1", retries=0
    )))
    service._clients = {"laya": httpx.AsyncClient(  # pyright: ignore[reportPrivateUsage]
        transport=httpx.MockTransport(handler), base_url="http://laya.test/v1"
    )}

    with pytest.raises(DecisionRequestError):
        _ = await service.predict("state", {})

    assert len(calls) == 1


async def test_其他4xx不重试() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, json={})

    with pytest.raises(DecisionRequestError):
        _ = await _service(handler).predict("state", {})

    assert len(calls) == 1


async def test_重试会记日志(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(decision_service, "_BACKOFF_BASE", 0.0)
    files = setup_logging(LogSettings(dir=str(tmp_path), level="TRACE"))
    monkeypatch.setattr(decision_service, "setup_logging", lambda _settings: files)

    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={})
        return httpx.Response(200, json={"model": "m", "answers": {}, "usage": {}})

    _ = await _service(handler).predict("state", {})

    assert "判断调用重试" in files.app.read_text(encoding="utf-8")
```

（文件头补 `import os` 不需要；补 `from pathlib import Path`、`from core.logger import setup_logging`、`from core.config import DecisionEndpointSettings, LogSettings`、`import core.decision.service as decision_service`。**注意用例名里不能出现断言关键字**，否则会被日志文件的 `tmp_path` 命中而假红。）

**Step 2: 跑测试**

Run: `.venv/bin/python -m pytest tests/test_decision_service.py -v`
Expected: 若 Task 4 的实现无误则应全 PASS；有 FAIL 就修实现（重试计数、except 顺序、JSON 解析收口是三个高危点）。

**Step 3: 提交**

```bash
git add tests/test_decision_service.py
git commit -m "test(decision): 覆盖错误映射与退避重试"
```

---

### Task 6: 三个便捷方法

**Files:**
- Modify: `tests/test_decision_service.py`（追加用例；实现已在 Task 4 落地）

**Step 1: 写测试**

```python
_CHOICE_BODY = {
    "model": "m",
    "answers": {
        _QID: {
            "type": "choice",
            "choice": "billing",
            "probabilities": {"billing": 0.88, "other": 0.12},
            "confidence": 0.81,
        }
    },
    "usage": {"input_tokens": 5, "output_tokens": 0},
}
_SCORE_BODY = {
    "model": "m",
    "answers": {
        _QID: {
            "type": "score",
            "score": 1.05,
            "legend": {"0": "平静", "1": "不满", "2": "愤怒"},
            "probabilities": {"0": 0.0, "1": 0.95, "2": 0.05},
            "confidence": 0.92,
        }
    },
    "usage": {"input_tokens": 5, "output_tokens": 0},
}
_NOUL_BODY = {
    "model": "m",
    "answers": {_QID: {"type": "noul", "noul": 0.93}},
    "usage": {"input_tokens": 5, "output_tokens": 0},
}


async def test_choose返回选中选项与分布() -> None:
    service = _service(lambda request: httpx.Response(200, json=_CHOICE_BODY))

    answer = await service.choose("要退款", "归哪个部门？", {"billing": "账单", "other": None})

    assert answer.choice == "billing"
    assert answer.probabilities["billing"] == 0.88
    assert answer.confidence == 0.81


async def test_rate返回得分与档位说明() -> None:
    service = _service(lambda request: httpx.Response(200, json=_SCORE_BODY))

    answer = await service.rate("钱扣了两次", "愤怒程度？", ["平静", "不满", "愤怒"])

    assert answer.score == 1.05
    assert answer.legend["1"] == "不满"


async def test_ask返回是概率() -> None:
    service = _service(lambda request: httpx.Response(200, json=_NOUL_BODY))

    assert await service.ask("我要退款", "是否要求退款？") == 0.93


async def test_便捷方法缺答案键时报错() -> None:
    service = _service(lambda request: httpx.Response(200, json={"model": "m", "answers": {}}))

    with pytest.raises(DecisionResponseError) as excinfo:
        _ = await service.ask("state", "是否？")

    assert "answer" in str(excinfo.value)


async def test_便捷方法字段不完整时报错() -> None:
    service = _service(
        lambda request: httpx.Response(
            200,
            json={
                "model": "m",
                "answers": {_QID: {"type": "choice", "choice": "billing"}},
                "usage": {},
            },
        )
    )

    with pytest.raises(DecisionResponseError):
        _ = await service.choose("state", "选一个", {"billing": None})


async def test_noul字段类型不对时报错() -> None:
    service = _service(
        lambda request: httpx.Response(
            200,
            json={"model": "m", "answers": {_QID: {"type": "noul", "noul": "yes"}}, "usage": {}},
        )
    )

    with pytest.raises(DecisionResponseError):
        _ = await service.ask("state", "是否？")


async def test_便捷方法把三原语混进同一趟() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_NOUL_BODY)

    service = _service(handler)

    _ = await service.predict(
        "state", {"a": noul("是否？"), "b": choice("选一个", {"x": None}), "c": score("打分", ["低", "高"])}
    )

    assert sorted(json.loads(seen[0].content)["questions"]) == ["a", "b", "c"]
    assert len(seen) == 1  # 三题只发一次请求
```

**Step 2: 跑测试**

Run: `.venv/bin/python -m pytest tests/test_decision_service.py -v`
Expected: 全 PASS。

**Step 3: 覆盖率的兜底检查**

```bash
.venv/bin/python -m pytest --cov=core --cov-report=term-missing | grep decision
```

Expected: `core/decision/*` 无未覆盖行。若有，按报告补用例（不要用 `# pragma: no cover`）。

**Step 4: 提交**

```bash
git add tests/test_decision_service.py
git commit -m "test(decision): 覆盖三个便捷方法"
```

---

### Task 7: 生命周期与健康检查

**Files:**
- Modify: `tests/test_decision_service.py`（追加用例）

**Step 1: 写测试**

```python
def _enabled(**overrides: object) -> DecisionSettings:
    return DecisionSettings(
        laya=DecisionEndpointSettings(
            enabled=True, base_url="http://laya.test/v1", model="multilingual"
        ),
        **overrides,
    )


async def test_start只为启用的端点建客户端() -> None:
    service = DecisionService(_enabled())

    await service.start()

    assert set(service._clients) == {"laya"}  # pyright: ignore[reportPrivateUsage]
    await service.stop()


async def test_未启用端点不产生警告(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = setup_logging(LogSettings(dir=str(tmp_path), level="TRACE"))
    monkeypatch.setattr(decision_service, "setup_logging", lambda _settings: files)

    await DecisionService(DecisionSettings()).start()

    assert "warning" not in files.app.read_text(encoding="utf-8").lower()


async def test_stop幂等() -> None:
    service = DecisionService(_enabled())
    await service.start()
    client = service._clients["laya"]  # pyright: ignore[reportPrivateUsage]

    await service.stop()
    await service.stop()

    assert client.is_closed


async def test_未启动时stop不报错() -> None:
    await DecisionService(DecisionSettings()).stop()


async def test_健康检查三态() -> None:
    service = DecisionService(_enabled())
    assert (await service.health()).healthy is False

    await service.start()
    healthy = await service.health()
    assert healthy.healthy is True
    assert healthy.detail == ""
    assert healthy.extra["laya模型"] == "multilingual"

    await service.stop()
    stopped = await service.health()
    assert stopped.healthy is False
    assert "未运行" in stopped.detail or "未启动" in stopped.detail


async def test_一个端点都没启用时的详情() -> None:
    service = DecisionService(DecisionSettings())
    await service.start()  # 状态进 RUNNING，但一个客户端都没有

    status = await service.health()

    assert status.healthy is False
    assert status.detail == NO_ENDPOINT
    assert status.extra["jev端点"] == "https://api.typesafe.ai/v1"


async def test_start幂等早退() -> None:
    service = DecisionService(_enabled())
    await service.start()
    first = service._clients["laya"]  # pyright: ignore[reportPrivateUsage]

    await service.start()  # 已是 RUNNING，应早退、不重建

    assert service._clients["laya"] is first  # pyright: ignore[reportPrivateUsage]
    await service.stop()
```

（补 `from core.decision.service import NO_ENDPOINT`。）

**Step 2: 跑测试**

Run: `.venv/bin/python -m pytest tests/test_decision_service.py -v`
Expected: 全 PASS。若 `test_健康检查三态` 的停止措辞断言不成立，先跑一次看 `not_running_detail()` 的实际文案再改断言（别改实现）。

**Step 3: 提交**

```bash
git add tests/test_decision_service.py
git commit -m "test(decision): 覆盖生命周期与健康检查"
```

---

### Task 8: 注册进容器、文档与冒烟

**Files:**
- Modify: `core/service/registry.py`
- Modify: `README.md`
- Modify: `tests/test_service_injection.py`（若它断言了注册顺序）

**Step 1: 写失败的测试**

`tests/test_service_injection.py` 里若有「注册表顺序」断言（形如 `["clock", "embedding", "llm"]`），改成加 `"decision"` 的版本；没有就新增一条：

```python
def test_注册表包含判断服务() -> None:
    manager = build_manager()

    assert "decision" in [service_type.name for service_type in manager._types]  # pyright: ignore[reportPrivateUsage]
```

**Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_service_injection.py -v`
Expected: FAIL

**Step 3: 实现**

`core/service/registry.py`：

```python
from core.decision.service import DecisionService
```

```python
    manager.register(ClockService, EmbeddingService, LLMService, DecisionService)
```

**Step 4: 跑全部测试 + 三条判据**

```bash
.venv/bin/python -m pytest | tail -3
uv run ruff check . && uv run ruff format --check .
uvx basedpyright core tests
.venv/bin/python -m pytest --cov=core --cov-report=term-missing | tail -20
```

Expected: 全绿、覆盖率 100%。

**Step 5: 冒烟**

```bash
timeout -s TERM 6 uv run python -m core.main
```

Expected: 正常情况下能看到 `服务健康 | 服务=decision | 健康=false | 详情=未启用任何判断端点，判断功能不可用 | jev端点=... | laya端点=...`，并且进程是被 SIGTERM 优雅关掉的、退出码 124。

**Step 6: 文档**

`README.md`：

1. 目录结构那段加一行 `decision/     判断层：批量提问与 choice / score / noul 三原语`（排在 `llm/` 之后）。
2. 依赖方向那句改成 `service → (config, logger)`、`embedding / llm / decision → (config, logger, service)`。
3. 配置项表末尾加 `decision.jev.*` / `decision.laya.*` 各字段（`enabled` / `base_url` / `api_key` / `model` / `timeout` / `retries`）。
4. 新增「判断层」章节，含四个调用示例与三条约定：

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

三条约定：**不记正文**（日志只记模型 / 端点 / 问题数 / 耗时 / token）；**`confidence` 不能跨后端比较**（Jev 是 `(n·p_max−1)/(n−1)`，Laya 是 `1−归一化熵`，各自的阈值要分别校准）；**零样本质量很差**（官方 model card 自述「Laya 是便于特化的基座，不是零样本决策引擎」，判断结果必须配阈值或人工兜底）。

**Step 7: 提交**

```bash
git add core/service/registry.py README.md tests/test_service_injection.py
git commit -m "feat(decision): 注册判断服务并补充文档"
```

---

## 收尾

跑一次完整判据并把结果贴进 PR / 汇报：

```bash
.venv/bin/python -m pytest | tail -3
uv run ruff check . && uv run ruff format --check .
uvx basedpyright core tests
.venv/bin/python -m pytest --cov=core | tail -3
```

然后按 `superpowers:finishing-a-development-branch` 的流程决定合并方式（本仓库惯例：`--no-ff` 合并后删 worktree 与分支）。
