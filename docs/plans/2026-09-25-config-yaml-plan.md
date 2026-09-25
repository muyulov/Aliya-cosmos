# config 层 YAML 化重构 实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 把配置层从「环境变量为中心」改为「`data/config/app.yaml` 为中心」，`.env` 只负责给 YAML 里的 `${VAR}` 占位符喂值。

**Architecture:** `core/config/` 拆为 `settings.py`（模型 + 单例 + 组装）与 `loader.py`（纯文件/文本层：读 `.env`、读 YAML、占位符插值），`__init__.py` 做对外门面。依赖方向 `settings → loader`，单向无环。移除 `pydantic-settings`，模型改继承 `pydantic.BaseModel`，YAML 由 `PyYAML` 解析、`.env` 由 `python-dotenv` 解析。

**Tech Stack:** Python 3.12、pydantic 2、PyYAML、python-dotenv、pytest、ruff、basedpyright。

**前置说明：与设计文档的一处偏差**

设计文档 §6 把 `load_settings()` 列在 `loader.py` 下，本计划把它放在 `settings.py`。原因：`settings.py` 原本就是 `get_settings()` 的所在地，若 `get_settings()` 反向调用 `loader.py` 的 `load_settings()`，而 `loader.py` 又需要 `Settings`，就会形成 `settings ↔ loader` 循环导入（basedpyright 的 `reportImportCycles` 会报）。现在的切法是 `loader.py` 不含任何 `core.*` 导入（纯工具），`settings.py` 单向依赖它。功能与接口不变，`core/config/__init__.py` 的导出一致。

**前置说明二：基线变更（2026-09-25 补记）**

实施期间 main 被推进了 `e341b0a refactor: 移除 api 层与 ItemService 示例服务`，项目由四层砍为 `service / logger / config` 三层：删除 `core/api/`（全部）、`core/service/item_service.py`、`tests/conftest.py`、`tests/helpers.py`、`test_health.py`、`test_items.py`；依赖去掉 `fastapi` / `uvicorn` / `httpx`；`AppSettings` 删掉 `host` / `port`；`core/main.py` 改为信号驱动的服务容器入口。设计文档已同步改写：配置源启动日志的目标从 `core/api/app.py` 改为 `core/main.py`。

本计划据此就地调整（不再逐条标注）：

- Task 2：`settings.py` 不再生成 `host` / `port`；删掉 `test_端口越界被pydantic拦截`，其余 port 用例改用 `env`；`tests/conftest.py` 已不存在，本 Task 不再涉及。
- Task 3：`fastapi` / `uvicorn` / `httpx` 已由 `e341b0a` 移除，本 Task 只负责删 `pydantic-settings`、加 `PyYAML` / `python-dotenv`。
- Task 4：启动日志改落 `core/main.py`（`core/api/app.py` 已不存在）。`core/main.py` 是常驻等待信号的入口，且被 `[tool.coverage.run] omit` 排除，**不再保留自动化用例**，改为手工验收。
- Task 5：`data/config/app.yaml` 与 README 配置项表去掉 `host` / `port`；占位符示例改用 `env`。

测试基线：新基线为 `77 passed`（原 93），本计划完成后预期 `104 passed`。

**工作区**：`.worktrees/config-yaml`（分支 `feature/config-yaml`）。所有命令在**该目录**下执行。

---

## Task 1: `loader.py` 纯工具层（读环境变量 / 读 YAML / 占位符插值）

**Files:**
- Create: `core/config/loader.py`
- Test: `tests/test_config_loader.py`

**Step 1: 写失败测试**

新建 `tests/test_config_loader.py`，本步只写纯函数部分（文件级加载的用例留到 Task 2）：

```python
"""配置加载测试：来源、占位符插值、类型转换与错误处理。

约定：除单例用例外，全部通过参数传入临时路径，不依赖工作目录。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.config.loader import ConfigError, interpolate, read_env, read_yaml


def _write(path: Path, text: str) -> Path:
    """写一个临时文件，返回其路径。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---- 纯函数：插值 ----


def test_嵌套字典与列表内的占位符都会展开() -> None:
    sources = {"A": "1", "B": "2"}
    value = {"x": {"y": ["${A}"]}, "z": "${B}"}
    assert interpolate(value, sources) == {"x": {"y": ["1"]}, "z": "2"}


def test_dict的key不插值() -> None:
    assert interpolate({"${A}": "${A}"}, {"A": "1"}) == {"${A}": "1"}


def test_占位符只展开一趟() -> None:
    """变量值里若还有 ${}, 不再二次展开，避免链式取值带来的隐式依赖。"""
    assert interpolate("${A}", {"A": "${B}", "B": "2"}) == "${B}"


def test_非字符串标量保持原样() -> None:
    value = {"n": 1, "b": True, "z": None, "f": 1.5}
    assert interpolate(value, {}) == {"n": 1, "b": True, "z": None, "f": 1.5}


def test_占位符无值且无默认时报错() -> None:
    with pytest.raises(ConfigError, match="MISSING_KEY"):
        _ = interpolate({"app": {"app_name": "${MISSING_KEY}"}}, {})


def test_带默认值的占位符在变量缺失时取默认() -> None:
    assert interpolate("${APP_PORT:8000}", {}) == "8000"


def test_报错信息带键路径() -> None:
    with pytest.raises(ConfigError, match="app.app_name"):
        _ = interpolate({"app": {"app_name": "${MISSING_KEY}"}}, {})


# ---- 纯函数：读环境变量 ----


def test_读env文件不注入进程环境(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONLY_IN_FILE", raising=False)
    env_file = _write(tmp_path / ".env", "ONLY_IN_FILE=1\n")

    values = read_env(env_file)

    assert values["ONLY_IN_FILE"] == "1"
    assert "ONLY_IN_FILE" not in os.environ


def test_进程环境变量优先于env文件(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAYERED_KEY", "from-process")
    env_file = _write(tmp_path / ".env", "LAYERED_KEY=from-dotenv\n")

    assert read_env(env_file)["LAYERED_KEY"] == "from-process"


def test_env文件缺失时只返回进程环境变量(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONLY_IN_PROCESS", "1")

    values = read_env(tmp_path / "missing.env")

    assert values["ONLY_IN_PROCESS"] == "1"


# ---- 纯函数：读 YAML ----


def test_读yaml文件缺失时返回空与未找到标记(tmp_path: Path) -> None:
    data, found = read_yaml(tmp_path / "missing.yaml")
    assert data == {}
    assert found is False


def test_读yaml正常返回数据与已找到标记(tmp_path: Path) -> None:
    config_file = _write(tmp_path / "app.yaml", "app:\n  port: 9000\n")

    data, found = read_yaml(config_file)

    assert data == {"app": {"port": 9000}}
    assert found is True


def test_yaml语法错误报_ConfigError(tmp_path: Path) -> None:
    config_file = _write(tmp_path / "app.yaml", "app: [未闭合\n")

    with pytest.raises(ConfigError, match="解析失败"):
        _ = read_yaml(config_file)


def test_yaml顶层不是映射时报错(tmp_path: Path) -> None:
    config_file = _write(tmp_path / "app.yaml", "- a\n- b\n")

    with pytest.raises(ConfigError, match="顶层"):
        _ = read_yaml(config_file)
```

**Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_config_loader.py | tail -5`
Expected: 收集阶段报 `ImportError: cannot import name 'loader' from 'core.config'`（或 `ModuleNotFoundError: core.config.loader`），全部用例 ERROR。

**Step 3: 写最小实现**

新建 `core/config/loader.py`：

```python
"""配置文件读取与占位符插值。

约定：
- 只负责「文件 / 文本」这一层，不依赖 core 内其他模块，也不感知配置模型。
- .env 的值只放进返回值，绝不注入 os.environ，避免给进程留下全局副作用。
- 进程环境变量优先于 .env；变量名精确匹配（大小写敏感）。
- 所有加载期错误统一为 ConfigError，由调用方决定是否终止进程。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import cast

import yaml
from dotenv import dotenv_values

#: 配置骨架文件，相对当前工作目录
CONFIG_FILE = Path("data/config/app.yaml")
#: 存放密钥等隐私值的文件，相对当前工作目录
ENV_FILE = Path(".env")

#: 占位符：${VAR} 或 ${VAR:默认值}
_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_]\w*)(?::([^}]*))?\}")


class ConfigError(Exception):
    """配置错误。

    不是 AppError，因此不会被转成 4xx：配置在 import 期加载，出错即让进程退出。
    """


def read_env(env_file: Path = ENV_FILE) -> dict[str, str]:
    """读取 .env 与进程环境变量，返回占位符取值表（进程环境变量优先）。

    python-dotenv 遇到文件不存在会走标准库 logging 发警告，这里先判存在，
    让「没有 .env」这个正常情形保持安静。
    """
    values: dict[str, str] = {}
    if env_file.is_file():
        parsed = dotenv_values(env_file)
        values.update({key: value for key, value in parsed.items() if value is not None})
    values.update(os.environ)
    return values


def read_yaml(config_file: Path = CONFIG_FILE) -> tuple[dict[str, object], bool]:
    """读取 YAML，返回 (原始数据, 是否读到文件)。文件缺失时返回空字典。"""
    if not config_file.is_file():
        return {}, False
    try:
        data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    except yaml.MarkedYAMLError as exc:
        raise ConfigError(f"配置文件解析失败：{config_file}：{exc}") from exc
    if data is None:  # 空文件
        return {}, True
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件顶层必须是映射：{config_file}，实际是 {type(data).__name__}")
    return cast("dict[str, object]", data), True


def interpolate(value: object, sources: dict[str, str], *, path: str = "") -> object:
    """递归展开 ${VAR} / ${VAR:默认值}。

    - 只作用于 str 标量，非字符串原样返回；
    - dict 的 key 不插值，只处理 value；
    - 单趟展开，变量值里的占位符不再二次展开。
    """
    if isinstance(value, str):
        return _PLACEHOLDER.sub(lambda match: _resolve(match, sources, path), value)
    if isinstance(value, dict):
        return {
            key: interpolate(item, sources, path=f"{path}.{key}" if path else str(key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            interpolate(item, sources, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    return value


def _resolve(match: re.Match[str], sources: dict[str, str], path: str) -> str:
    """替换单个占位符：优先取值表中的值，其次取默认值，都没有则报错。"""
    name = match.group(1)
    default = match.group(2)
    if name in sources:
        return sources[name]
    if default is not None:
        return default
    raise ConfigError(f"占位符 ${{{name}}} 未定义且无默认值（位置：{path or '顶层'}）")
```

**Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_config_loader.py | tail -3`
Expected: `14 passed`

再跑一次全量确认没有连带破坏：

Run: `uv run pytest | tail -3`
Expected: `107 passed`（93 基线 + 14 新增）

**Step 5: 提交**

```bash
git add core/config/loader.py tests/test_config_loader.py
git commit -m "feat(config): 新增 loader 文件读取与占位符插值"
```

---

## Task 2: `settings.py` 改造 + `load_settings()` + 门面导出

**Files:**
- Modify: `core/config/settings.py`（整体重写）
- Modify: `core/config/__init__.py`
- Test: `tests/test_config_loader.py`（追加文件级用例）

> `tests/conftest.py` 已被 `e341b0a` 删除，本 Task 不再涉及。

**Step 1: 写失败测试**

在 `tests/test_config_loader.py` 顶部把 import 补全：

```python
from pydantic import ValidationError

from core.config import ConfigError, Settings, get_settings, load_settings
from core.config.loader import interpolate, read_env, read_yaml
```

（`ConfigError` 同时从 `core.config` 导入，用于验证门面导出。）

在文件末尾追加：

```python
def _load(tmp_path: Path, yaml_text: str, env_text: str | None = None) -> Settings:
    """把 YAML（可选 .env）写进临时目录后加载。"""
    config_file = _write(tmp_path / "app.yaml", yaml_text)
    env_file = tmp_path / ".env"
    if env_text is not None:
        env_file = _write(env_file, env_text)
    return load_settings(config_file=config_file, env_file=env_file)


# ---- 来源标记 ----


def test_文件缺失时退回默认值并标记来源(tmp_path: Path) -> None:
    config_file = tmp_path / "missing.yaml"

    settings = load_settings(config_file=config_file, env_file=tmp_path / ".env")

    assert settings.app.app_name == "aliya-cosmos"
    assert settings.log.level == "INFO"
    assert settings.config_source == f"内置默认值（未找到 {config_file}）"


def test_直接构造的实例标记为外部注入() -> None:
    assert Settings().config_source == "外部注入"


def test_正常文件覆盖默认值并记录来源(tmp_path: Path) -> None:
    config_file = _write(tmp_path / "app.yaml", "app:\n  env: prod\nlog:\n  level: DEBUG\n")

    settings = load_settings(config_file=config_file, env_file=tmp_path / ".env")

    assert settings.app.env == "prod"
    assert settings.log.level == "DEBUG"
    assert settings.config_source == str(config_file)


def test_未知配置项被忽略(tmp_path: Path) -> None:
    assert _load(tmp_path, "app:\n  unknown_key: 1\n").app.app_name == "aliya-cosmos"


def test_插值从env文件取值(tmp_path: Path) -> None:
    settings = _load(tmp_path, "app:\n  app_name: ${APP_NAME}\n", "APP_NAME=from-dotenv\n")
    assert settings.app.app_name == "from-dotenv"


def test_插值优先取进程环境变量(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_NAME", "from-process")
    settings = _load(tmp_path, "app:\n  app_name: ${APP_NAME}\n", "APP_NAME=from-dotenv\n")
    assert settings.app.app_name == "from-process"


def test_占位符无值且无默认时启动失败(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="MISSING_KEY"):
        _ = _load(tmp_path, "app:\n  app_name: ${MISSING_KEY}\n")


def test_带默认值的占位符变量缺失时取默认(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)
    settings = _load(tmp_path, "app:\n  env: ${APP_ENV:dev}\n")
    assert settings.app.env == "dev"


def test_带默认值的占位符优先取环境变量(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_ENV", "prod")
    settings = _load(tmp_path, "app:\n  env: ${APP_ENV:dev}\n")
    assert settings.app.env == "prod"


def test_插值结果交给pydantic转型(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_LOG_JSON", "true")
    settings = _load(tmp_path, "log:\n  json: ${APP_LOG_JSON:false}\n")
    assert settings.log.json_output is True


def test_引号包住的轮转值保持字符串(tmp_path: Path) -> None:
    """PyYAML 兼容 YAML 1.1，不加引号的 00:00 会被解析成整数 0。"""
    settings = _load(tmp_path, 'log:\n  rotation: "00:00"\n')
    assert settings.log.rotation == "00:00"


# ---- pydantic 校验 ----


def test_非法枚举值被pydantic拦截(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        _ = _load(tmp_path, "app:\n  env: staging\n")


# ---- 单例与路径语义 ----


def test_单例缓存且配置文件按工作目录解析(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    try:
        first = get_settings()
        assert get_settings() is first
        assert first.config_source.startswith("内置默认值")

        _ = _write(tmp_path / "data" / "config" / "app.yaml", "app:\n  app_name: from-file\n")
        get_settings.cache_clear()
        assert get_settings().app.app_name == "from-file"
    finally:
        get_settings.cache_clear()
```

**Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_config_loader.py | tail -5`
Expected: `ImportError: cannot import name 'Settings'...`（因 `load_settings` / `config_source` 尚不存在）。

**Step 3: 写实现**

整体替换 `core/config/settings.py`：

```python
"""配置模块。

约定：
- data/config/app.yaml 是配置的唯一骨架，字段缺失时用这里的默认值。
- 需要密钥等隐私值时不写明文，在 YAML 里写 ${VAR} 占位符，值由 .env 提供。
- 通过 get_settings() 获取单例；测试中可用 get_settings.cache_clear() 重置，
  或用 load_settings(config_file=..., env_file=...) 显式指定文件。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from core.config.loader import CONFIG_FILE, ENV_FILE, interpolate, read_env, read_yaml

Environment = Literal["dev", "test", "prod"]

#: 未找到 YAML 骨架文件时的来源描述前缀
SOURCE_DEFAULT = "内置默认值"
#: 直接构造实例（测试或调用方注入）时的来源描述
SOURCE_INJECTED = "外部注入"


class AppSettings(BaseModel):
    """应用层配置。"""

    app_name: str = "aliya-cosmos"
    env: Environment = "dev"
    debug: bool = True

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"


class LogSettings(BaseModel):
    """日志层配置。

    注意 json 字段用别名 json_output：json 与 pydantic 自带的序列化方法撞名，
    直接命名 json 会冲突。YAML 里写 json，代码里读 json_output。
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(populate_by_name=True)

    level: str = "INFO"
    json_output: bool = Field(default=False, alias="json")
    dir: str = "logs"
    file_name: str = "app.log"
    error_file_name: str = "error.log"
    rotation: str = "00:00"
    retention: str = "7 days"
    compression: str = "zip"


class Settings(BaseModel):
    """顶层配置，按子模块分组，避免一个扁平大类。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", populate_by_name=True)

    app: AppSettings = AppSettings()
    log: LogSettings = LogSettings()

    #: 本次配置的来源；由 load_settings() 写入，直接构造实例时保持 None
    _source: str | None = PrivateAttr(default=None)

    @property
    def config_source(self) -> str:
        """本次配置的来源，供启动日志展示。"""
        return self._source or SOURCE_INJECTED

    def mark_source(self, source: str) -> None:
        """记录配置来源。由 load_settings() 在装配完成后调用。"""
        self._source = source


def load_settings(
    config_file: Path = CONFIG_FILE,
    env_file: Path = ENV_FILE,
) -> Settings:
    """加载配置：读 YAML → 占位符插值 → 校验，并记录配置来源。

    文件缺失不报错（全部走默认值），占位符取不到值则抛 ConfigError。
    """
    raw, found = read_yaml(config_file)
    data = interpolate(raw, read_env(env_file))
    settings = Settings.model_validate(data)
    settings.mark_source(str(config_file) if found else f"{SOURCE_DEFAULT}（未找到 {config_file}）")
    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取配置单例。"""
    return load_settings()
```

替换 `core/config/__init__.py`：

```python
"""配置模块对外出口。"""

from core.config.loader import CONFIG_FILE, ENV_FILE, ConfigError
from core.config.settings import (
    AppSettings,
    LogSettings,
    Settings,
    get_settings,
    load_settings,
)

__all__ = [
    "CONFIG_FILE",
    "ENV_FILE",
    "AppSettings",
    "ConfigError",
    "LogSettings",
    "Settings",
    "get_settings",
    "load_settings",
]
```

`tests/conftest.py` 已被 `e341b0a` 删除（其中原有的 `test_settings` 夹具与 api 层一并移除），本 Task 不再改动它。

**Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_config_loader.py | tail -3`
Expected: `27 passed`（Task 1 的 14 个 + 本步新增的 13 个）

Run: `uv run pytest | tail -3`
Expected: `104 passed`（77 基线 + 27 新增）

Run: `uv run ruff check .`
Expected: `All checks passed!`

**Step 5: 提交**

```bash
git add core/config/settings.py core/config/__init__.py tests/test_config_loader.py
git commit -m "refactor(config): 配置改由 YAML 骨架加载并支持占位符插值"
```

---

## Task 3: 移除 `pydantic-settings` 依赖

**Files:**
- Modify: `pyproject.toml`（`dependencies` 段）

> `fastapi` / `uvicorn` / `httpx` 已由 `e341b0a` 移除，本 Task 只处理配置相关依赖。

**Step 1: 确认已无引用**

Run: `grep -rn "pydantic_settings\|pydantic-settings" core tests pyproject.toml`
Expected: 只剩 `pyproject.toml` 里的一行依赖声明。

**Step 2: 改依赖**

`dependencies` 改为：

```toml
dependencies = [
    "pydantic>=2.9",
    "pyyaml>=6.0",
    "python-dotenv>=1.0",
    "loguru>=0.7",
]
```

**Step 3: 同步环境并验证**

Run: `uv sync`
Expected: 卸载 `pydantic-settings`，`uv.lock` 更新。

Run: `uv run pytest | tail -3`
Expected: `104 passed`

Run: `uv run ruff check .`
Expected: `All checks passed!`

**Step 4: 提交**

```bash
git add pyproject.toml uv.lock
git commit -m "chore(config): 移除 pydantic-settings 依赖"
```

---

## Task 4: 启动日志记录配置来源

**Files:**
- Modify: `core/main.py`（`main()` 里的启动日志）

> `core/api/app.py` 已由 `e341b0a` 删除，启动日志随入口层迁到 `core/main.py`。`core/main.py` 是常驻等待 SIGINT / SIGTERM 的入口，且被 `[tool.coverage.run] omit = ["core/main.py"]` 排除，因此本 Task **不新增自动化用例**（为一行日志让单测去起子进程 + 等信号不划算），改以手工验收为准；`config_source` 的三态取值本身已有 Task 2 的 4 个用例覆盖。

**Step 1: 写实现**

`core/main.py` 的 `main()` 启动日志加 `配置源` 字段：

```python
def main() -> None:
    settings = get_settings()
    setup_logging(settings.log)

    log.info(
        "应用启动中",
        face=faces.START,
        应用=settings.app.app_name,
        环境=settings.app.env,
        配置源=settings.config_source,
    )
    asyncio.run(_run(build_manager(settings)))
    log.info("应用已关闭", face=faces.BYE, 应用=settings.app.app_name)
```

**Step 2: 手工验收**

Run: `rm -rf logs; timeout -s TERM 3 uv run python -m core.main; grep "配置源" logs/app.log`
Expected: `└─ 配置源: data/config/app.yaml`
（`rm -rf logs` 不可省：日志是追加写的，不复位会读到旧内容；服务常驻，故用 `timeout -s TERM` 触发优雅关闭。）

Run: 在别的工作目录下启动，验证 CWD 相对语义确实留下痕迹
```bash
mkdir -p /tmp/cwd-check && cd /tmp/cwd-check && rm -rf logs \
  && PYTHONPATH=/home/cosmos/项目/Aliya-cosmos/.worktrees/config-yaml \
     /home/cosmos/项目/Aliya-cosmos/.worktrees/config-yaml/.venv/bin/python -m core.main
grep "配置源" /tmp/cwd-check/logs/app.log
```
Expected: `└─ 配置源: 内置默认值（未找到 data/config/app.yaml）`

**Step 3: 跑测试与静态检查**

Run: `uv run pytest | tail -3`
Expected: `104 passed`（本 Task 无新增用例）

Run: `uv run ruff check .`
Expected: `All checks passed!`

**Step 4: 提交**

```bash
git add core/main.py
git commit -m "feat(main): 启动日志记录配置来源"
```

---

## Task 5: 落地配置文件与文档

**Files:**
- Create: `data/config/app.yaml`
- Modify: `.env.example`（整体替换）
- Modify: `README.md`（第 5 行技术栈、第 17 行、第 38 行目录结构、第 44-65 行配置章节、第 85 行、第 115 行）

**Step 1: 新增 `data/config/app.yaml`**

内容与模型默认值一致，**不含任何无默认值的占位符**，保证克隆后无 `.env` 也能启动：

```yaml
# 配置骨架。所有字段的默认值见 core/config/settings.py，这里只写需要调整的值。
# 需要密钥等隐私值时不要写明文，用 ${VAR} 占位符，值放在 .env 里。

app:
  app_name: aliya-cosmos
  env: dev
  debug: true

log:
  level: INFO
  json: false
  dir: logs
  rotation: "00:00"           # 必须带引号：不加引号会被 YAML 解析成整数
  retention: 7 days
  compression: zip

# 密钥类字段以占位符声明，由 .env 提供值：
# api_key: ${DEEPSEEK_API_KEY}
```

**Step 2: 替换 `.env.example`**

```bash
# 本文件存放 app.yaml 中占位符的值，通常是密钥。复制为 .env 后按需填写。
#
# 规则：
# - 唯一作用是给 data/config/app.yaml 里的 ${VAR} 提供值，不能直接覆盖配置项。
# - 进程环境变量优先于本文件，变量名大小写敏感（${App_Key} 与 ${APP_KEY} 是两个名字）。
# - 缺失属正常：只要 YAML 里的占位符都带默认值，没有本文件照样能启动。

# 示例：需在 app.yaml 中写 api_key: ${DEEPSEEK_API_KEY} 才会生效
# DEEPSEEK_API_KEY=sk-xxxx
```

**Step 3: 改 README**

第 17 行改为：

```markdown
无需任何配置即可启动，所有配置项都有默认值。要自定义时改 `data/config/app.yaml`；密钥类配置写在 `.env`（参考 `.env.example`）。
```

目录结构块里在 `tests/` 之前插入一行：

```text
data/           配置与运行数据：config/app.yaml 为配置骨架，可安全提交
```

配置章节（原「## 配置」到「## 日志」之间的全部内容）替换为：

```markdown
## 配置

配置以 `data/config/app.yaml` 为唯一来源（路径相对当前工作目录）。该文件可安全提交，因为它不含任何明文密钥。要调整配置，改这个文件即可。

```yaml
app:
  env: ${APP_ENV:dev}        # 部署时可用环境变量顶掉
log:
  level: INFO
  json: false
```

### 占位符

`${VAR}` 与 `${VAR:默认值}` 两种写法，取值来源是进程环境变量与 `.env`（[进程环境变量优先](https://12factor.net/zh_cn/config)）：

| 写法 | 语义 |
| --- | --- |
| `${DEEPSEEK_API_KEY}` | 取不到值就启动失败，进程退出 |
| `${APP_ENV:dev}` | 取不到值时用 `dev` |

占位符只作用于字符串值，`app.yaml` 里 dict 的键不会被替换；展开后一律是字符串，类型由 pydantic 转换（如 `"true"` → `bool`）。

`.env` 的唯一职责就是给占位符喂值，**不能**直接覆盖配置项。变量名大小写敏感：`${App_Key}` 与 `${APP_KEY}` 是两个不同的名字。

### 配置项

| YAML 路径 | 默认值 | 说明 |
| --- | --- | --- |
| `app.app_name` | `aliya-cosmos` | 应用名 |
| `app.env` | `dev` | 环境，只能是 dev / test / prod |
| `app.debug` | `true` | 调试开关 |
| `log.level` | `INFO` | 日志级别 |
| `log.json` | `false` | 是否输出 JSON 日志 |
| `log.dir` | `logs` | 日志目录 |
| `log.file_name` | `app.log` | 主日志文件名 |
| `log.error_file_name` | `error.log` | 错误日志文件名 |
| `log.rotation` | `00:00` | 轮转阈值（默认按天），写进 YAML 时必须加引号 |
| `log.retention` | `7 days` | 保留时长 |
| `log.compression` | `zip` | 归档压缩方式 |

### 失败行为

配置在进程启动时加载，出错即退出：

| 场景 | 行为 |
| --- | --- |
| `data/config/app.yaml` 不存在 | 全部走默认值继续启动，启动日志显示 `配置源=内置默认值（未找到 …）` |
| YAML 语法错误或顶层不是映射 | 报错退出 |
| 占位符取不到值且没写默认值 | 报错退出，并指出是哪个变量 |
| 字段取值非法（如 `env: staging`） | 报错退出 |

启动日志里的 `配置源` 字段会写明本次配置来自哪个文件，路径是相对工作目录解析的，从别处启动时请核对这一项。
```

同时第 85 行的 `APP_LOG__JSON=true` 改为 `` `log.json: true` ``，第 115 行的 `（跟随 `APP_LOG__LEVEL`）` 改为 `（跟随 `log.level`）`。第 5 行技术栈里的 `pydantic-settings` 改为 `PyYAML`，第 38 行目录结构里的 `config/        配置层：pydantic-settings 分组配置` 改为 `config/        配置层：YAML 骨架加载与占位符插值`（这两处旧计划漏列，但 Task 6 的 grep 会要求清干净）。

**Step 4: 手工验收**

先临时把 `data/config/app.yaml` 的 `env: dev` 改成 `env: ${APP_ENV:dev}`（验收完改回 `dev` 再提交），然后：

Run: `uv run python -c "from core.config import get_settings as g; print(g().app.env, g().config_source)"`
Expected: `dev data/config/app.yaml`

Run: `APP_ENV=prod uv run python -c "from core.config import get_settings as g; print(g().app.env, g().config_source)"`
Expected: `prod data/config/app.yaml` —— 环境变量顶掉了 YAML 里的默认值

Run: `mkdir -p /tmp/cwd-check && cd /tmp/cwd-check && PYTHONPATH=/home/cosmos/项目/Aliya-cosmos/.worktrees/config-yaml /home/cosmos/项目/Aliya-cosmos/.worktrees/config-yaml/.venv/bin/python -c "from core.config import get_settings as g; print(g().app.env, g().config_source)"`
Expected: `dev 内置默认值（未找到 data/config/app.yaml）` —— 验证 CWD 相对语义与来源标记（这里不设 `APP_ENV`，走模型默认值）

Run: `cd /home/cosmos/项目/Aliya-cosmos/.worktrees/config-yaml && uv run pytest | tail -3`
Expected: `104 passed`（切换工作目录后回归，确认无状态残留）

**Step 5: 提交**

```bash
git add data/config/app.yaml .env.example README.md
git commit -m "docs(config): 落地 YAML 配置骨架并重写配置文档"
```

---

## Task 6: 收尾校验

**Step 1: 全量测试与静态检查**

Run: `uv run pytest | tail -3`
Expected: `104 passed`

Run: `uv run ruff check .`
Expected: `All checks passed!`

Run: `uv run ruff format --check .`
Expected: 无待格式化文件（若有，跑 `uv run ruff format .` 后复跑测试）。

**Step 2: 复查遗留引用**

Run: `grep -rn "APP_APP__\|APP_LOG__\|pydantic_settings\|pydantic-settings" core tests README.md .env.example pyproject.toml`
Expected: 无输出。

Run: `grep -rn "app_name" README.md`
Expected: 仅剩配置项表里的 `app.app_name` 一行。

**Step 3: 提交剩余改动（如有）**

```bash
git add -A
git commit -m "chore(config): 收尾清理"
```

**Step 4: 回到主工作区做类型检查**

`basedpyright` 在 `.worktrees/` 下会把 `core.*` 解析到主工作区的旧代码，诊断不可信。合并回 `main` 之后必须再查一遍诊断，把结论补到交付说明里。

---

## 完成后的合并流程

```bash
cd /home/cosmos/项目/Aliya-cosmos
git merge --no-ff feature/config-yaml -m "merge: config 层 YAML 化重构"
uv run pytest | tail -3
uv run ruff check .
git worktree remove .worktrees/config-yaml
git branch -d feature/config-yaml
```

合并后需复查 basedpyright 诊断（连续两轮结果一致才算数），确认 `core/config/` 无新增告警。
