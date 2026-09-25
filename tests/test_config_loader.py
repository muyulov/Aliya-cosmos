"""配置加载测试：来源、占位符插值、类型转换与错误处理。

约定：除单例用例外，全部通过参数传入临时路径，不依赖工作目录。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.config import ConfigError, Settings, get_settings, load_settings
from core.config.loader import interpolate, read_env, read_yaml


def _write(path: Path, text: str) -> Path:
    """写一个临时文件，返回其路径。"""
    _ = path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(text, encoding="utf-8")
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
    with pytest.raises(ConfigError, match=r"app\.app_name"):
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


# ---- 文件级加载 ----


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


def test_带默认值的占位符变量缺失时取默认(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)
    settings = _load(tmp_path, "app:\n  env: ${APP_ENV:dev}\n")
    assert settings.app.env == "dev"


def test_带默认值的占位符优先取环境变量(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_单例缓存且配置文件按工作目录解析(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
