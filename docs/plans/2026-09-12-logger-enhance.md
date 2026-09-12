# 日志层完善实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 把 `core/logger/` 从"能用"提升到"好用"：请求上下文自动携带 request_id、异常堆栈独立渲染、主日志与错误日志分离并按天轮转。

**Architecture:** 新增 `context.py`（contextvars 存 `dict[str, str]` 快照，不依赖 loguru）与 `sinks.py`（三个 sink 的装配与 filter 工厂）。`formatters.py` 自行渲染堆栈并清空 `record["exception"]` 阻止 loguru 重复追加。`setup.py` 瘦身为门面 + 装配编排，删除会全局串号的 `bind_request`。`api` 层的手写 `request_id` 字段全部删除。

**Tech Stack:** Python 3.12、loguru、contextvars、pytest（asyncio_mode=auto）、uv、ruff

**设计文档：** `docs/plans/2026-09-12-logger-design.md`

**工作目录：** `/home/cosmos/项目/Aliya-cosmos/.worktrees/logger-enhance`（所有命令都在此目录下执行）

---

## 前置说明

- 命令统一走 `uv run`。
- 测试用例名用中文，与现有 `tests/test_logger.py` 风格一致。
- 每个任务末尾提交一次。
- `tests/test_logger.py` 里的 `make_record` 是目前唯一的 record 构造器，本计划会扩展它以支持 `exception` 字段。

---

### Task 1: 新增 `context.py`（上下文存储）

**Files:**
- Create: `core/logger/context.py`
- Test: `tests/test_logger_context.py`

**Step 1: 写失败的测试**

创建 `tests/test_logger_context.py`：

```python
"""日志上下文测试。"""

from __future__ import annotations

import asyncio

from core.logger import context


def test_未绑定时_request_id_返回占位符() -> None:
    assert context.request_id() == "-"


def test_current_未绑定时返回空字典() -> None:
    assert context.current() == {}


def test_bind_后可读出() -> None:
    token = context.bind(request_id="abc", 会话="s1")
    try:
        assert context.request_id() == "abc"
        assert context.current()["会话"] == "s1"
    finally:
        context.reset(token)
    assert context.request_id() == "-"


def test_嵌套_bind_按逆序还原() -> None:
    outer = context.bind(request_id="outer")
    inner = context.bind(request_id="inner")
    assert context.request_id() == "inner"
    context.reset(inner)
    assert context.request_id() == "outer"
    context.reset(outer)
    assert context.request_id() == "-"


def test_new_request_id_为_16_位十六进制() -> None:
    rid = context.new_request_id()
    assert len(rid) == 16
    assert all(c in "0123456789abcdef" for c in rid)
    assert context.new_request_id() != rid


def test_request_scope_自动生成并还原() -> None:
    with context.request_scope() as rid:
        assert rid
        assert context.request_id() == rid
    assert context.request_id() == "-"


def test_request_scope_沿用传入的_id() -> None:
    with context.request_scope("fixed-id") as rid:
        assert rid == "fixed-id"
        assert context.request_id() == "fixed-id"


async def test_并发任务互不污染() -> None:
    """两个协程各自绑定，互不串号。"""

    async def worker(tag: str, gate: asyncio.Event) -> str:
        with context.request_scope(tag):
            await gate.wait()
            return context.request_id()

    gate = asyncio.Event()
    task_a = asyncio.create_task(worker("aaa", gate))
    task_b = asyncio.create_task(worker("bbb", gate))
    await asyncio.sleep(0)
    gate.set()
    assert await task_a == "aaa"
    assert await task_b == "bbb"
```

**Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_logger_context.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'core.logger.context'`

**Step 3: 实现**

创建 `core/logger/context.py`：

```python
"""日志上下文。

为什么用 contextvars：request_id 这类维度属于"横切"信息，希望调用点零手写、
全链路自动携带。loguru 的 logger.configure(extra=...) 是全局的，多请求并发时
会互相覆盖；contextvars 按协程隔离，配合 reset 保证嵌套场景正确回退。

本模块不依赖 loguru，只依赖标准库，因此可被 api 层直接使用而不引入框架耦合。
值类型限制为 str：上下文会跨 await 传播并写入日志，限制成字符串可避免往上下文
塞大对象导致引用滞留。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

#: 上下文快照。不可变用法：每次 bind 生成新字典。
_context: ContextVar[dict[str, str]] = ContextVar("logger_context", default={})

#: 上下文里的保留键
REQUEST_ID = "request_id"

#: request_id 缺失时的占位符
MISSING = "-"

ContextToken = Token[dict[str, str]]


def current() -> dict[str, str]:
    """读出当前上下文快照（副本，外部改动不影响内部）。"""
    return dict(_context.get())


def bind(**kv: object) -> ContextToken:
    """把键值并入当前上下文，返回用于还原的 token。

    值统一转成字符串，保证上下文里只有 str。
    """
    merged = {**_context.get(), **{key: str(value) for key, value in kv.items()}}
    return _context.set(merged)


def reset(token: ContextToken) -> None:
    """还原到 bind 之前的状态。"""
    _context.reset(token)


def request_id() -> str:
    """取当前 request_id，缺失时返回占位符。"""
    return _context.get().get(REQUEST_ID, MISSING)


def new_request_id() -> str:
    """生成新的 request_id（16 位十六进制）。"""
    return uuid.uuid4().hex[:16]


@contextmanager
def request_scope(request_id_value: str | None = None) -> Iterator[str]:
    """进入请求上下文，退出时自动还原。

    传入为空时自动生成 id，yield 出最终生效的 id。
    """
    rid = request_id_value or new_request_id()
    token = bind(**{REQUEST_ID: rid})
    try:
        yield rid
    finally:
        reset(token)


__all__ = [
    "MISSING",
    "REQUEST_ID",
    "ContextToken",
    "bind",
    "current",
    "new_request_id",
    "request_id",
    "request_scope",
    "reset",
]
```

注意：参数名用 `request_id_value` 而非 `request_id`，避免与同名函数遮蔽。

**Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_logger_context.py -v`
Expected: PASS，9 passed

**Step 5: 提交**

```bash
git add core/logger/context.py tests/test_logger_context.py
git commit -m "feat(logger): 新增基于 contextvars 的日志上下文"
```

---

### Task 2: `formatters.py` 支持堆栈独立渲染

**Files:**
- Modify: `core/logger/formatters.py`
- Test: `tests/test_logger.py`

**Step 1: 写失败的测试**

在 `tests/test_logger.py` 的 `make_record` 增加 `exception` 参数。把该函数整体替换为：

```python
def make_record(
    message: str = "消息",
    *,
    level: str = "INFO",
    face: str = "",
    exception: object = None,
    **fields: object,
) -> Record:
    """构造一个字段形状与 loguru Record 一致的最小记录。

    loguru 的 Record 是 TypedDict，渲染函数按 record["key"] 取值，
    因此这里直接造 dict。Record 的必填字段远多于渲染所需，故用 cast
    声明这是有意为之的测试替身。
    """
    raw: dict[str, object] = {
        "message": message,
        "level": FakeLevel(level),
        "time": datetime(2026, 8, 27, 23, 9, 52),
        "exception": exception,
        "extra": {"face": face, "fields": fields},
    }
    return cast(Record, cast(object, raw))
```

在文件末尾追加测试：

```python
class FakeException:
    """模拟 loguru 的 RecordException（含 type/value/traceback 三元组）。"""

    def __init__(self, exc: BaseException) -> None:
        self.type = type(exc)
        self.value = exc
        self.traceback = exc.__traceback__


def _make_exception() -> FakeException:
    try:
        raise KeyError("item_id")
    except KeyError as exc:
        return FakeException(exc)


def test_无堆栈时不渲染堆栈块() -> None:
    line = format_tree(make_record("正常"))
    assert "堆栈" not in line


def test_带堆栈时字段块完整且堆栈独立成块() -> None:
    line = format_tree(make_record("未捕获异常", level="ERROR", 路径="/x", exception=_make_exception()))
    lines = line.split("\n")
    assert lines[0].startswith("2026-08-27 23:09:52 [E]")
    assert lines[1] == "    └─ 路径: /x"
    assert lines[2] == "    └─ 堆栈"
    body = "\n".join(lines[3:])
    assert "KeyError" in body
    assert "\n" in body


def test_堆栈渲染不会把_exception_塞进字段() -> None:
    line = format_tree(make_record("异常", exception=_make_exception()))
    assert "exception:" not in line


def test_json_模式下堆栈作为独立键且保持单行() -> None:
    import json

    raw = format_json(make_record("未捕获异常", exception=_make_exception()))
    assert "\n" not in raw
    payload = cast("dict[str, object]", json.loads(raw))
    assert isinstance(payload["exception"], str)
    assert "KeyError" in cast("str", payload["exception"])
```

**Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_logger.py -v -k "堆栈 or 独立键"`
Expected: FAIL，断言不通过（当前实现不渲染堆栈）

**Step 3: 实现**

修改 `core/logger/formatters.py`。

首先在文件头部 import 区加入：

```python
import traceback
```

在 `format_tree` 里，把 `if not fields: return line` 之后的逻辑改为先收集行、再追加堆栈块。将 `format_tree` 整体替换为：

```python
def format_tree(record: Record) -> str:
    """树形格式化，供控制台与文件 sink 使用。

    返回不带结尾换行的字符串，换行由 loguru 负责，避免多行内容被拼接。
    """
    extra = _extra_of(record)
    level_name = _level_name_of(record)
    face = _pick_face(extra.get("face"), level_name)
    fields = _fields_of(extra)
    timestamp = _time_of(record).strftime("%Y-%m-%d %H:%M:%S")

    head = f"{timestamp} [{LEVEL_TAGS.get(level_name, 'I')}]"
    if face:
        head = f"{head} {face}"
    lines = [f"{head} {_message_of(record)}"]

    items = list(fields.items())
    for index, (key, value) in enumerate(items):
        branch = "└─" if index == len(items) - 1 else "├─"
        lines.append(f"{FIELD_INDENT}{branch} {key}: {render_value(value)}")

    lines.extend(_stack_lines(record))
    return "\n".join(lines)
```

在 `render_value` 之前新增两个函数：

```python
def format_exception(record: Record) -> str | None:
    """把 record 里的异常渲染成堆栈文本，无异常时返回 None。"""
    exception = record.get("exception")
    if not exception:
        return None
    stack = traceback.format_exception(exception.type, exception.value, exception.traceback)
    return "".join(stack).rstrip("\n")


def _stack_lines(record: Record) -> list[str]:
    """生成堆栈块的行，供树形渲染使用。"""
    text = format_exception(record)
    if not text:
        return []
    lines = [f"{FIELD_INDENT}└─ 堆栈"]
    lines.extend(f"{FIELD_INDENT}   {raw}" for raw in text.split("\n"))
    return lines
```

把 `format_json` 替换为：

```python
def format_json(record: Record) -> str:
    """JSON 格式化，字段平铺到顶层；堆栈作为独立键存放。"""
    extra = _extra_of(record)
    level_name = _level_name_of(record)

    payload: dict[str, object] = {
        "time": _time_of(record).strftime("%Y-%m-%d %H:%M:%S"),
        "level": level_name,
        "message": _message_of(record),
        "face": _pick_face(extra.get("face"), level_name),
    }
    payload.update(_fields_of(extra))

    stack = format_exception(record)
    if stack:
        payload["exception"] = stack

    return json.dumps(payload, ensure_ascii=False, default=str)
```

把 `format_exception` 加进 `__all__`（放在 `colorize` 之前，保持字母序：`colorize`、`format_exception`、`format_json`、`format_tree`、`render_value`）。

**Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_logger.py -v`
Expected: PASS，全部通过（原有 11 项 + 新增 4 项）

**Step 5: 提交**

```bash
git add core/logger/formatters.py tests/test_logger.py
git commit -m "feat(logger): 异常堆栈独立渲染，JSON 模式下作为独立键"
```

---

### Task 3: 新增 `sinks.py`（三 sink 装配）

**Files:**
- Create: `core/logger/sinks.py`
- Modify: `core/config/settings.py`（新增 `error_file_name`，`rotation` 默认值改 `00:00`）
- Test: `tests/test_logger_sinks.py`

**Step 1: 改配置**

修改 `core/config/settings.py` 的 `LogSettings`，把这两个字段：

```python
    rotation: str = "10 MB"
```

改为：

```python
    rotation: str = "00:00"
```

并在 `file_name` 之后新增：

```python
    error_file_name: str = "error.log"
```

**Step 2: 写失败的测试**

创建 `tests/test_logger_sinks.py`：

```python
"""sink 装配测试。"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from loguru import logger

from core.config import LogSettings
from core.logger import setup_logging


@pytest.fixture(autouse=True)
def _restore_logger():
    """每个用例前后重置 loguru，避免 sink 泄漏到其他测试。"""
    yield
    _ = logger.remove()
    logging.getLogger().handlers = []


def _cfg(tmp_path: Path, **overrides: object) -> LogSettings:
    base: dict[str, object] = {
        "level": "INFO",
        "dir": str(tmp_path / "logs"),
        "retention": "1 day",
    }
    base.update(overrides)
    return LogSettings(**base)  # type: ignore[arg-type]


def test_装配三个_sink(tmp_path: Path) -> None:
    setup_logging(_cfg(tmp_path))
    assert len(logger._core.handlers) == 3  # noqa: SLF001


def test_敏感度_信息只进主日志(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    setup_logging(cfg)
    logger.info("普通信息")
    logger.remove()

    app_log = Path(cfg.dir) / cfg.file_name
    error_log = Path(cfg.dir) / cfg.error_file_name
    assert "普通信息" in app_log.read_text(encoding="utf-8")
    assert not error_log.exists() or "普通信息" not in error_log.read_text(encoding="utf-8")


def test_错误同时进主日志与错误日志(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    setup_logging(cfg)
    logger.error("出错了")
    logger.remove()

    app_text = (Path(cfg.dir) / cfg.file_name).read_text(encoding="utf-8")
    error_text = (Path(cfg.dir) / cfg.error_file_name).read_text(encoding="utf-8")
    assert "出错了" in app_text
    assert "出错了" in error_text


def test_默认按天轮转() -> None:
    assert LogSettings().rotation == "00:00"
    assert LogSettings().error_file_name == "error.log"
```

**Step 3: 运行测试确认失败**

Run: `uv run pytest tests/test_logger_sinks.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'core.logger.sinks'`（或在配置断言处失败）

**Step 4: 实现**

创建 `core/logger/sinks.py`：

```python
"""日志 sink 装配。

三个 sink 共用同一套渲染逻辑，仅在输出目标、级别门槛与着色上有差异：
- 控制台 stderr：级别跟随配置，仅在 TTY 下着色。
- 主日志文件：级别跟随配置，按天轮转。
- 错误日志文件：级别固定 ERROR，按天轮转，便于运维只翻错误。

为什么用 filter 而不是 format 函数：loguru 的 format 传函数时，返回值里的换行
会被它自己追加的换行逻辑吃掉，多行日志会挤成一行；而 filter 能在输出前改写
record，配合 format="{message}" 可原样保留换行。
"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

from core.config import LogSettings
from core.logger.formatters import colorize, format_exception, format_json, format_tree
from core.logger.types import FilterFunction, Record

#: 错误日志文件的级别门槛
ERROR_LEVEL = "ERROR"


def _is_tty() -> bool:
    """当前 stderr 是否连接到终端。"""
    stream = sys.stderr
    return hasattr(stream, "isatty") and bool(stream.isatty())


def build_filter(cfg: LogSettings, *, color: bool) -> FilterFunction:
    """构造 per-sink filter：把 record 预渲染为完整文本。

    filter 可能被 loguru 多次调用，因此改写必须幂等——用 extra 里的标记位判重，
    否则消息会被重复拼接。Record 是 TypedDict，直接按键读写即可。

    堆栈由我们自己渲染进 message，因此这里同时把 record["exception"] 置空，
    阻止 loguru 再追加一份。
    """

    def _filter(record: Record) -> bool:
        extra = record["extra"]
        if extra.get("_rendered"):
            return True

        text = format_json(record) if cfg.json_output else format_tree(record)
        if color and not cfg.json_output:
            text = colorize(text, record["level"].name)
        record["message"] = text
        extra["_rendered"] = True
        # 堆栈已由 format_exception 渲染，清空以免 loguru 重复追加
        record["exception"] = None
        return True

    return _filter


def add_console_sink(cfg: LogSettings) -> None:
    """控制台 sink。"""
    _ = logger.add(
        sys.stderr,
        level=cfg.level.upper(),
        format="{message}",
        colorize=False,
        backtrace=False,
        diagnose=False,
        filter=build_filter(cfg, color=_is_tty()),
    )


def add_file_sinks(cfg: LogSettings) -> tuple[Path, Path]:
    """装配主日志与错误日志两个文件 sink，返回两者的路径。"""
    log_dir = Path(cfg.dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    app_log = log_dir / cfg.file_name
    error_log = log_dir / cfg.error_file_name

    common: dict[str, object] = {
        "format": "{message}",
        "rotation": cfg.rotation,
        "retention": cfg.retention,
        "compression": cfg.compression,
        "encoding": "utf-8",
        "backtrace": False,
        "diagnose": False,
    }

    _ = logger.add(
        app_log,
        level=cfg.level.upper(),
        filter=build_filter(cfg, color=False),
        **common,  # type: ignore[arg-type]
    )
    _ = logger.add(
        error_log,
        level=ERROR_LEVEL,
        filter=build_filter(cfg, color=False),
        **common,  # type: ignore[arg-type]
    )
    return app_log, error_log


__all__ = [
    "ERROR_LEVEL",
    "add_console_sink",
    "add_file_sinks",
    "build_filter",
]
```

注意：`format_exception` 在本模块未直接调用，如导入后 ruff 报未使用，请从 import 行删掉它。

**Step 5: 更新 `setup.py` 使用新模块**

这一步与 Task 4 合并进行（Task 4 会重写 `setup.py`）。先只跑 context 分测试确认 `sinks.py` 可导入：

Run: `uv run python -c "import core.logger.sinks; print('ok')"`
Expected: `ok`

**Step 6: 提交**

```bash
git add core/logger/sinks.py core/config/settings.py tests/test_logger_sinks.py
git commit -m "feat(logger): 新增 sink 装配模块，主日志与错误日志分离"
```

---

### Task 4: 重写 `setup.py`（门面瘦身 + 装配编排）

**Files:**
- Modify: `core/logger/setup.py`
- Modify: `core/logger/__init__.py`
- Test: `tests/test_logger_sinks.py`

**Step 1: 写失败的测试**

在 `tests/test_logger_sinks.py` 末尾追加：

```python
def test_门面自动携带上下文字段(tmp_path: Path) -> None:
    """log.info 时，上下文里的键自动成为树形字段。"""
    from core.logger import context, log

    setup_logging(_cfg(tmp_path))
    with context.request_scope("rid-1"):
        log.info("带上下文")
    logger.remove()

    text = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
    assert "带上下文" in text
    assert "rid-1" in text
    assert "request_id" in text


def test_调用点字段覆盖上下文(tmp_path: Path) -> None:
    """kwargs 显式传值与上下文同名时，以调用点为准。"""
    from core.logger import context, log

    setup_logging(_cfg(tmp_path))
    with context.request_scope("ctx-id"):
        log.info("覆盖", request_id="explicit-id")
    logger.remove()

    text = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
    assert "explicit-id" in text
    assert "ctx-id" not in text


def test_log_context_上下文管理器(tmp_path: Path) -> None:
    from core.logger import log

    setup_logging(_cfg(tmp_path))
    with log.context(会话="s-9"):
        log.info("会话内")
    log.info("会话外")
    logger.remove()

    text = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
    inside, _, outside = text.partition("会话外")
    assert "s-9" in inside
    assert "s-9" not in outside


def test_不再暴露_bind_request() -> None:
    from core.logger import log

    assert not hasattr(log, "bind_request")
```

**Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_logger_sinks.py -v -k "上下文 or 覆盖 or bind_request"`
Expected: FAIL，`AttributeError: 'Log' object has no attribute 'context'`

**Step 3: 实现**

把 `core/logger/setup.py` 整体替换为：

```python
"""日志装配与门面。

职责：
- 暴露 log 对象：调用 log.info("消息", face=..., 任意字段=值) 时字段自动成树，
  并把 context.py 里的上下文自动并入字段。
- 编排 sink 装配（委托给 sinks.py）。
- 桥接标准库 logging（uvicorn、sqlalchemy 等）到 loguru，统一格式。

导入关系：setup 依赖 sinks / formatters / context，反向不成立，无导入环。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from types import FrameType, ModuleType
from typing import override

from loguru import logger

import core.logger.faces as faces_module
from core.config import LogSettings, get_settings
from core.logger import context as context_module
from core.logger.sinks import add_console_sink, add_file_sinks
from core.logger.types import Record

_INTERCEPTED_LOGGERS = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "fastapi",
    "sqlalchemy",
    "asyncio",
)


class Log:
    """日志门面：把 kwargs 与上下文合并为结构化字段并注入 loguru。

    类型注解约定：face 为颜文字字符串，fields 为任意键值的结构化字段，
    因此这里必须宽松（object），这也是 loguru 自身的签名风格。
    """

    #: 颜文字常量表，便于 log.face.CHEER 这样取用
    face: ModuleType = faces_module

    def _emit(self, level: str, message: str, face: str | None = None, **fields: object) -> None:
        resolved = face or faces_module.DEFAULT_BY_LEVEL.get(level.upper(), "")
        merged = {**context_module.current(), **fields}
        logger.bind(face=resolved, fields=merged).log(level.upper(), message)

    def debug(self, message: str, face: str | None = None, **fields: object) -> None:
        self._emit("DEBUG", message, face, **fields)

    def info(self, message: str, face: str | None = None, **fields: object) -> None:
        self._emit("INFO", message, face, **fields)

    def success(self, message: str, face: str | None = None, **fields: object) -> None:
        self._emit("SUCCESS", message, face, **fields)

    def warning(self, message: str, face: str | None = None, **fields: object) -> None:
        self._emit("WARNING", message, face, **fields)

    def error(self, message: str, face: str | None = None, **fields: object) -> None:
        self._emit("ERROR", message, face, **fields)

    def critical(self, message: str, face: str | None = None, **fields: object) -> None:
        self._emit("CRITICAL", message, face, **fields)

    def exception(self, message: str, face: str | None = None, **fields: object) -> None:
        """记录异常并附带 traceback。"""
        resolved = face or faces_module.DEFAULT_BY_LEVEL.get("ERROR", "")
        merged = {**context_module.current(), **fields}
        logger.bind(face=resolved, fields=merged).exception(message)

    @contextmanager
    def context(self, **kv: object) -> Iterator[None]:
        """临时附加业务维度到日志上下文，退出时自动还原。"""
        token = context_module.bind(**kv)
        try:
            yield
        finally:
            context_module.reset(token)


#: 全局日志对象
log = Log()


class InterceptHandler(logging.Handler):
    """把标准库 logging 记录转发给 loguru。"""

    @override
    def emit(self, record: logging.LogRecord) -> None:
        try:
            level_name = logger.level(record.levelname).name
        except ValueError:
            level_name = str(record.levelno)

        frame: FrameType | None = logging.currentframe()
        depth = 1
        while frame is not None and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level_name, record.getMessage())


def setup_logging(settings: LogSettings | None = None) -> None:
    """装配日志。可重复调用，会先移除已有 sink。"""
    cfg = settings or get_settings().log

    # 返回的 handler id 在运行期不需要，显式赋给 _ 表示有意忽略
    _ = logger.remove()

    add_console_sink(cfg)
    app_log, error_log = add_file_sinks(cfg)

    _intercept_stdlib()

    log.info(
        "日志已就绪",
        face=faces_module.START,
        级别=cfg.level.upper(),
        主日志=str(app_log),
        错误日志=str(error_log),
    )


def _intercept_stdlib() -> None:
    """接管标准库与第三方库日志。"""
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    for name in _INTERCEPTED_LOGGERS:
        target = logging.getLogger(name)
        target.handlers = [InterceptHandler()]
        target.propagate = False


__all__ = ["InterceptHandler", "Log", "log", "setup_logging"]
```

注意：删除了对 `Record` / `FilterFunction` 的运行时依赖（filter 已移到 `sinks.py`）。若 `Record` 未使用则从 import 中移除。

**Step 4: 更新 `__init__.py`**

把 `core/logger/__init__.py` 替换为：

```python
"""日志模块对外出口。

用法：
    from core.logger import log, setup_logging

    setup_logging()
    log.info("用户回合已入队", 参与者="qq:6329133635628374381", 已取消旧计划=0)

导入顺序说明：formatters 会 `from core.logger import faces`，若本文件在顶层
再导入 setup，就会形成 `__init__ → setup → formatters → __init__` 的导入环。
因此这里先导入 faces 与 context（供子模块使用），再导入 formatters，最后导入
setup，且不使用包内互相回导。
"""

from __future__ import annotations

from core.logger import context, faces
from core.logger.formatters import colorize, format_exception, format_json, format_tree
from core.logger.setup import Log, log, setup_logging

__all__ = [
    "Log",
    "colorize",
    "context",
    "faces",
    "format_exception",
    "format_json",
    "format_tree",
    "log",
    "setup_logging",
]
```

**Step 5: 运行全部日志测试**

Run: `uv run pytest tests/test_logger.py tests/test_logger_context.py tests/test_logger_sinks.py -v`
Expected: PASS，全部通过

**Step 6: 提交**

```bash
git add core/logger/setup.py core/logger/__init__.py tests/test_logger_sinks.py
git commit -m "refactor(logger): setup 瘦身为门面与装配编排，删除 bind_request"
```

---

### Task 5: `api` 层移除手写 request_id

**Files:**
- Modify: `core/api/middleware.py`
- Modify: `core/api/errors.py`
- Test: `tests/test_logger_sinks.py`（追加集成用例）

**Step 1: 写失败的测试**

在 `tests/test_logger_sinks.py` 末尾追加：

```python
async def test_请求日志自动携带_request_id(tmp_path: Path) -> None:
    """中间件不再手写 request_id，字段由上下文自动注入。"""
    from httpx import ASGITransport, AsyncClient

    from core.api import create_app
    from core.config import AppSettings, Settings
    from core.service import ServiceManager
    from core.service.item_service import ItemService

    settings = Settings(
        app=AppSettings(app_name="t", env="test", debug=False),
        log=_cfg(tmp_path),
    )
    mgr = ServiceManager()
    _ = mgr.register(ItemService())
    app = create_app(settings=settings, manager=mgr)

    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/health", headers={"X-Request-ID": "header-id"})
    logger.remove()

    assert resp.status_code == 200
    text = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
    assert "header-id" in text
    assert "请求完成" in text


def test_中间件不再显式传_request_id() -> None:
    """回归防护：middleware 源码里不应再出现 request_id=request_id 这种手写字段。"""
    import inspect

    from core.api import middleware

    source = inspect.getsource(middleware)
    assert "request_id=request_id" not in source
    assert "bind_request" not in source
```

**Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_logger_sinks.py -v -k "request_id"`
Expected: FAIL，源码里仍含 `request_id=request_id`

**Step 3: 实现中间件**

把 `core/api/middleware.py` 替换为：

```python
"""请求上下文中间件。

为每个请求生成 request_id，写入 request.state 与响应头，并进入日志上下文，
使该请求链路中所有日志自动携带该 id（无需调用点手写字段）。
请求结束时打一条 access 日志。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import override

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from core.logger import faces, log
from core.logger import context as log_context

REQUEST_ID_HEADER = "X-Request-ID"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """注入 request_id 并记录访问日志。"""

    @override
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        with log_context.request_scope(request.headers.get(REQUEST_ID_HEADER)) as request_id:
            request.state.request_id = request_id

            begin = time.perf_counter()
            try:
                response = await call_next(request)
            except Exception as exc:
                elapsed_ms = round((time.perf_counter() - begin) * 1000, 1)
                log.error(
                    "请求处理异常",
                    face=faces.BOOM,
                    方法=request.method,
                    路径=request.url.path,
                    耗时=f"{elapsed_ms}ms",
                    异常=type(exc).__name__,
                )
                raise

            elapsed_ms = round((time.perf_counter() - begin) * 1000, 1)
            response.headers[REQUEST_ID_HEADER] = request_id
            log.info(
                "请求完成",
                方法=request.method,
                路径=request.url.path,
                状态码=response.status_code,
                耗时=f"{elapsed_ms}ms",
            )
            return response
```

**Step 4: 实现异常处理器改动**

修改 `core/api/errors.py`：删除每个 handler 里的 `request_id = _request_id(request)` 行与 `request_id=request_id,` 参数行，并删除文件末尾的 `_request_id` 函数。同时把 `from typing import ClassVar, cast` 改为 `from typing import ClassVar, cast`（`cast` 仍被 `_describe_validation_errors` 使用，保留）。

具体四处 handler 改为：

```python
    @app.exception_handler(AppError)
    async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        log.warning(
            "业务异常",
            face=faces.THINK,
            路径=request.url.path,
            错误码=exc.code,
            原因=exc.message,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.code, exc.message, _request_id(request), exc.extra),
        )

    @app.exception_handler(ItemNotFoundError)
    async def _handle_item_not_found(request: Request, exc: ItemNotFoundError) -> JSONResponse:
        log.warning(
            "条目不存在",
            face=faces.THINK,
            路径=request.url.path,
            条目编号=exc.item_id,
        )
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content=error_body("ITEM_NOT_FOUND", str(exc), _request_id(request)),
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        log.warning(
            "参数校验失败",
            face=faces.THINK,
            路径=request.url.path,
            问题数=len(exc.errors()),
        )
        detail = _describe_validation_errors(exc)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=error_body("INVALID_ARGUMENT", "请求参数不合法", _request_id(request), {"错误": detail}),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(f"HTTP_{exc.status_code}", str(exc.detail), _request_id(request)),
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # exception 会带上完整堆栈，仅在此处记录，对外不暴露
        log.exception(
            "未捕获异常",
            face=faces.BOOM,
            路径=request.url.path,
            异常=type(exc).__name__,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=error_body("INTERNAL_ERROR", "服务内部错误", _request_id(request)),
        )
```

`_request_id` 函数**保留**，因为它仍负责填充对外响应信封的 `request_id` 字段（那是 HTTP 契约，不是日志字段）。只需去掉 handler 内部对它的重复调用。

**Step 5: 运行全部测试**

Run: `uv run pytest -v`
Expected: PASS，全部通过

**Step 6: 提交**

```bash
git add core/api/middleware.py core/api/errors.py tests/test_logger_sinks.py
git commit -m "refactor(api): request_id 改由日志上下文自动携带"
```

---

### Task 6: 更新 README 与最终验证

**Files:**
- Modify: `README.md`

**Step 1: 更新配置表**

在 README 的配置表格里，把 `APP_LOG__ROTATION` 的默认值改为 `00:00`，并新增一行：

```markdown
| `APP_LOG__ERROR_FILE_NAME` | `error.log` | 错误日志文件名 |
```

**Step 2: 更新日志章节**

在 README 的"日志"章节末尾追加：

```markdown
请求链路的 `request_id` 会自动携带，调用点无需手写字段：

```python
from core.logger import log

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
    ├─ 路径: /api/v1/items
    └─ 异常: KeyError
    └─ 堆栈
       ...
```

日志文件有两个：`logs/app.log`（跟随 `APP_LOG__LEVEL`）与 `logs/error.log`（固定 ERROR 级），均按天轮转。
```

**Step 3: 全量验证**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
Expected: 测试全绿，ruff 无告警，格式无差异

**Step 4: 提交**

```bash
git add README.md
git commit -m "docs: 补充日志上下文与错误日志说明"
```

---

## 完成标准

- `uv run pytest` 全绿，且新增测试覆盖上下文隔离、堆栈渲染、三 sink 分离。
- `uv run ruff check .` 与 `uv run ruff format --check .` 无输出。
- `core/logger/` 为 6 个文件：`__init__.py`、`faces.py`、`context.py`、`formatters.py`、`sinks.py`、`setup.py`。
- 全仓库 grep 不到 `request_id=request_id` 与 `bind_request`。
