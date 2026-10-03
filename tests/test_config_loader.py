"""配置加载测试：来源、占位符插值、类型转换与错误处理。

约定：除单例用例外，全部通过参数传入临时路径，不依赖工作目录。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import cast

import pytest
from pydantic import BaseModel, ValidationError

from core.config import (
    ConfigError,
    LogSettings,
    MemorySettings,
    Settings,
    get_settings,
    load_settings,
)
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


def test_双美元符号转义为字面占位符() -> None:
    """$${A} 里的 {A} 不再参与取值，配置里因此能写出字面 ${VAR}。"""
    assert interpolate("$${A}", {"A": "1"}) == "${A}"


def test_双美元符号转义为字面美元() -> None:
    assert interpolate("价格：100$$", {}) == "价格：100$"


def test_单个美元符号保持原样() -> None:
    assert interpolate("$A ${A}", {"A": "1"}) == "$A 1"


def test_多趟展开链式变量() -> None:
    """rounds=2 时变量值里的占位符继续展开，支持 A → B 的链式取值。"""
    assert interpolate("${A}", {"A": "${B}", "B": "2"}, rounds=2) == "2"


def test_多趟展开不改动已转义的字面占位符() -> None:
    """回归：转义记录必须留到全部展开结束才还原，中途还原会被下一趟当成占位符吃掉。"""
    assert interpolate("$${A}", {"A": "1"}, rounds=2) == "${A}"


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


def test_读yaml文件缺失时返回空值(tmp_path: Path) -> None:
    assert read_yaml(tmp_path / "missing.yaml") is None


def test_读yaml正常返回数据(tmp_path: Path) -> None:
    config_file = _write(tmp_path / "cosmos.yaml", "app:\n  port: 9000\n")

    assert read_yaml(config_file) == {"app": {"port": 9000}}


def test_yaml语法错误报_ConfigError(tmp_path: Path) -> None:
    config_file = _write(tmp_path / "cosmos.yaml", "app: [未闭合\n")

    with pytest.raises(ConfigError, match="解析失败"):
        _ = read_yaml(config_file)


def test_yaml顶层不是映射时报错(tmp_path: Path) -> None:
    config_file = _write(tmp_path / "cosmos.yaml", "- a\n- b\n")

    with pytest.raises(ConfigError, match="顶层"):
        _ = read_yaml(config_file)


def test_yaml编码非法时报_ConfigError(tmp_path: Path) -> None:
    """回归：非 UTF-8 的配置文件也要收敛成 ConfigError，而不是裸 UnicodeDecodeError。"""
    config_file = tmp_path / "cosmos.yaml"
    _ = config_file.write_bytes(b"app: \xff\xfe\n")

    with pytest.raises(ConfigError, match="UTF-8"):
        _ = read_yaml(config_file)


def test_yaml含控制字符时报_ConfigError(tmp_path: Path) -> None:
    """回归：ReaderError 是 YAMLError 的子类却不是 MarkedYAMLError，只捕后者会裸冒出去。"""
    config_file = tmp_path / "cosmos.yaml"
    _ = config_file.write_bytes(b"app:\n  app_name: \x00bad\n")

    with pytest.raises(ConfigError, match="解析失败"):
        _ = read_yaml(config_file)


def test_读yaml遇_OSError_时报_ConfigError(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """回归：读不动（权限等）也要收敛成 ConfigError，而不是裸 OSError。"""
    config_file = _write(tmp_path / "cosmos.yaml", "app: {}\n")

    def _boom(*_args: object, **_kwargs: object) -> str:
        raise PermissionError("没有权限")

    monkeypatch.setattr(Path, "read_text", _boom)

    with pytest.raises(ConfigError, match="读取失败"):
        _ = read_yaml(config_file)


def test_空yaml文件返回空字典(tmp_path: Path) -> None:
    config_file = _write(tmp_path / "cosmos.yaml", "")

    assert read_yaml(config_file) == {}


# ---- 文件级加载 ----


def _load(tmp_path: Path, yaml_text: str, env_text: str | None = None) -> Settings:
    """把 YAML（可选 .env）写进临时目录后加载。"""
    config_file = _write(tmp_path / "cosmos.yaml", yaml_text)
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
    config_file = _write(tmp_path / "cosmos.yaml", "app:\n  env: prod\nlog:\n  level: DEBUG\n")

    settings = load_settings(config_file=config_file, env_file=tmp_path / ".env")

    assert settings.app.env == "prod"
    assert settings.log.level == "DEBUG"
    assert settings.config_source == str(config_file)


def test_空文件来源标出全部走默认值(tmp_path: Path) -> None:
    config_file = _write(tmp_path / "cosmos.yaml", "")

    settings = load_settings(config_file=config_file, env_file=tmp_path / ".env")

    assert settings.app.app_name == "aliya-cosmos"
    assert settings.config_source == f"{config_file}（空文件，全部走默认值）"


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


def test_加载时可开启多趟展开(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """默认单趟，expand_rounds=2 时变量值里的占位符继续展开。"""
    monkeypatch.setenv("HOST_INNER", "api.example.com")
    config_file = _write(tmp_path / "cosmos.yaml", "app:\n  app_name: ${HOST_OUTER}\n")
    env_file = _write(tmp_path / ".env", "HOST_OUTER=${HOST_INNER}\n")

    settings = load_settings(config_file=config_file, env_file=env_file, expand_rounds=2)

    assert settings.app.app_name == "api.example.com"


# ---- pydantic 校验 ----


def test_非法枚举值被pydantic拦截(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        _ = _load(tmp_path, "app:\n  env: staging\n")


def test_非法日志级别在加载期被拦下(tmp_path: Path) -> None:
    """回归：级别写错要在配置加载期报错，而不是等 setup_logging 抛裸 ValueError。"""
    with pytest.raises(ValidationError, match="level"):
        _ = _load(tmp_path, "log:\n  level: BOGUS\n")


def test_日志级别大小写不敏感(tmp_path: Path) -> None:
    assert _load(tmp_path, "log:\n  level: debug\n").log.level == "DEBUG"


# ---- 单例与路径语义 ----


def test_单例缓存且配置文件按工作目录解析(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    try:
        first = get_settings()
        assert get_settings() is first
        assert first.config_source.startswith("内置默认值")

        _ = _write(tmp_path / "data" / "config" / "cosmos.yaml", "app:\n  app_name: from-file\n")
        get_settings.cache_clear()
        assert get_settings().app.app_name == "from-file"
    finally:
        get_settings.cache_clear()


# ---- 服务与时钟配置节点 ----


def test_服务与时钟配置节点缺省值() -> None:
    settings = Settings()

    assert settings.service.start_timeout == 30.0
    assert settings.service.stop_timeout == 30.0
    assert settings.clock.tz == "UTC"


def test_服务与时钟配置可被yaml覆盖(tmp_path: Path) -> None:
    settings = _load(
        tmp_path,
        "service:\n  start_timeout: 1.5\n  stop_timeout: 2\nclock:\n  tz: Asia/Shanghai\n",
    )

    assert settings.service.start_timeout == 1.5
    assert settings.service.stop_timeout == 2.0
    assert settings.clock.tz == "Asia/Shanghai"


def test_时钟时区可走占位符(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOCK_TZ", "Asia/Tokyo")

    settings = _load(tmp_path, "clock:\n  tz: ${CLOCK_TZ:UTC}\n")

    assert settings.clock.tz == "Asia/Tokyo"


# ---- 判断层配置节点 ----


def test_判断层配置节点缺省值() -> None:
    settings = Settings()

    assert settings.decision.jev.enabled is False
    assert settings.decision.jev.base_url == "https://api.typesafe.ai/v1"
    assert settings.decision.jev.model == "jev-latest"
    assert settings.decision.laya.model == ""
    assert settings.decision.laya.base_url == "http://127.0.0.1:8000/v1"
    assert settings.decision.jev.retries == 2


def test_判断层配置可被yaml覆盖(tmp_path: Path) -> None:
    settings = _load(
        tmp_path,
        "decision:\n  laya:\n    enabled: true\n    model: multilingual\n    timeout: 12.5\n",
    )

    assert settings.decision.laya.enabled is True
    assert settings.decision.laya.model == "multilingual"
    assert settings.decision.laya.timeout == 12.5


def test_判断层超时必须为正数(tmp_path: Path) -> None:
    config = tmp_path / "cosmos.yaml"
    _ = config.write_text("decision:\n  jev:\n    timeout: 0\n", encoding="utf-8")

    with pytest.raises(ValidationError):
        _ = load_settings(config_file=config, env_file=tmp_path / ".env")


# ---- 出厂配置守护 ----

#: 出厂骨架：需要真实读一份，按相对本文件的路径取以免依赖工作目录
_SKELETON = Path(__file__).resolve().parent.parent / "data" / "config" / "cosmos.yaml"


def test_日志文件名前缀不互为包含关系() -> None:
    """回归：主日志的反查 glob 会把「以它为前缀」的错误日志一起匹配进来。

    模板 `cosmos-{time}.log` 的 glob 是 `cosmos-*.log`，而 `cosmos-error-{time}.log`
    正好满足它，主日志曾被解析成错误日志文件（见 core/logger/sinks.py）。
    代码默认值与出厂骨架都不许踩这个坑。
    """
    skeleton_log = load_settings(config_file=_SKELETON, env_file=_SKELETON.parent / ".env").log

    for config in (LogSettings(), skeleton_log):
        app_prefix = config.file_name.split("{", 1)[0]
        error_prefix = config.error_file_name.split("{", 1)[0]

        assert not app_prefix.startswith(error_prefix), config
        assert not error_prefix.startswith(app_prefix), config


# ---- 记忆层配置节点 ----


def test_记忆段的值从临时yaml读进模型(tmp_path: Path) -> None:
    """防 extra="ignore" 静默失效：写进 YAML 的记忆段必须真的被解析进模型。

    探针字段刻意取与代码默认值不同的值（骨架里写死），再断言模型里就是这些值。
    若段名或缩进写错被 extra="ignore" 静默丢掉，模型会落回默认值，此处即红。
    """
    yaml_text = (
        "memory:\n"
        "  db_path: data/记忆探针\n"
        "  dimensions: 32\n"
        "  queue_size: 7\n"
        "  gate:\n"
        "    backend: jev\n"
        "  retry:\n"
        "    deadletter_path: data/记忆探针/死信.jsonl\n"
    )
    config_file = _write(tmp_path / "cosmos.yaml", yaml_text)

    settings = load_settings(config_file=config_file, env_file=tmp_path / ".env")

    assert settings.memory.db_path == "data/记忆探针"
    assert settings.memory.dimensions == 32
    assert settings.memory.queue_size == 7
    assert settings.memory.gate.backend == "jev"
    assert settings.memory.retry.deadletter_path == "data/记忆探针/死信.jsonl"
    assert settings.config_source == str(config_file)


def _字段名与别名的并集(model: type[BaseModel]) -> set[str]:
    """一个模型「认识的键」：字段名与别名的并集。

    YAML 里既可以写字段名也可以写别名（如 log.json 之于 json_output），两者都算被认识。
    """
    return {
        name
        for name, field in model.model_fields.items()
        for name in (name, field.alias)
        if name is not None
    }


def _按_yaml键取子模型(model: type[BaseModel], key: object) -> type[BaseModel] | None:
    """按 YAML 里的键（字段名或别名）取到对应字段，若其注解是模型类就返回该模型。

    只认 BaseModel 子类：字段类型是 dict[str, str] 这类普通映射时返回 None，
    调用方据此停下，不拿它去做字段对拍（否则会误报）。
    """
    for name, field in model.model_fields.items():
        if key == name or key == field.alias:
            annotation = field.annotation
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                return annotation
            return None
    return None


def _核对键树(raw: dict[object, object], model: type[BaseModel], 键路径: str) -> None:
    """递归对拍：raw 的每个键都要落在 model 认识的键里，dict 值继续下钻到叶子。

    `键路径` 是当前层相对骨架根的路径（顶层为空串），报错时拼成完整路径——
    例如 `memory.retry.deadletter_pth`，三级之后也能定位到具体是哪一段。
    - 只在「值是 dict 且对应字段是 BaseModel 子类」时下钻，叶子与非模型字段都停下。
    """
    known = _字段名与别名的并集(model)
    unknown = set(raw) - known
    where = f"骨架 {键路径} 段" if 键路径 else "骨架顶层"
    assert not unknown, f"{where}里有模型不认识的键（会静默失效）：{sorted(map(str, unknown))}"

    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        child = _按_yaml键取子模型(model, key)
        if child is None:
            continue
        子路径 = f"{键路径}.{key}" if 键路径 else str(key)
        _核对键树(cast("dict[object, object]", value), child, 子路径)


def test_骨架的段与字段都被模型认识() -> None:
    """防 extra="ignore" 静默失效：对拍原始骨架的键与模型的字段树。

    从顶层一路递归到叶子：每一层的键都必须落在对应模型认识的键里，只要骨架里写了
    某个模型不认识的键（含三级嵌套，如 memory.retry 下的键），它就会被 extra="ignore"
    悄悄丢掉，这里当场指出是哪个键，并给出完整键路径。
    """
    raw = read_yaml(_SKELETON)
    assert raw is not None, f"出厂骨架必须在：{_SKELETON}"

    _核对键树(cast("dict[object, object]", raw), Settings, "")


def test_llm的memory段被模型认识且不抢chat(tmp_path: Path) -> None:
    """回归：llm.memory 段必须被解析进模型（曾因缺字段被 extra="ignore" 整段丢掉）。"""
    settings = _load(tmp_path, "llm:\n  memory:\n    model: 记忆模型\n")

    assert settings.llm.memory.model == "记忆模型"
    # memory 端点独立于 chat，不该把 chat 的模型顶掉。
    assert settings.llm.chat.model != "记忆模型"


def test_记忆层默认值全部合法() -> None:
    """不读文件直接构造也要有一组合法默认值（进程无配置也能起）。"""
    memory = MemorySettings()
    assert memory.queue_size > 0
    assert 0.0 <= memory.gate.importance_threshold <= 1.0
    assert memory.recall.hops >= 1
    assert memory.consolidate.min_events >= 1
    assert 0.0 < memory.decay.floor <= 1.0
    assert memory.retry.max_attempts >= 1


#: 越界的记忆层字段（YAML 里的完整路径 → 越界值）+ 出错字段名，用于守住加载期拦截。
#: 路径要对：top_k / hops / budget 在 recall 下，不在 gate 下。
_越界记忆字段 = [
    ("memory:\n  recall:\n    top_k: 0\n", "top_k"),
    ("memory:\n  recall:\n    hops: 4\n", "hops"),
    ("memory:\n  recall:\n    budget: 0\n", "budget"),
    ("memory:\n  gate:\n    importance_threshold: 1.1\n", "importance_threshold"),
    ("memory:\n  gate:\n    novelty_threshold: -0.1\n", "novelty_threshold"),
    ("memory:\n  queue_size: 0\n", "queue_size"),
    ("memory:\n  dimensions: 0\n", "dimensions"),
    ("memory:\n  decay:\n    tau: 0\n", "tau"),
    ("memory:\n  decay:\n    floor: 0\n", "floor"),
    ("memory:\n  consolidate:\n    min_events: 0\n", "min_events"),
    ("memory:\n  retry:\n    max_attempts: 0\n", "max_attempts"),
    ("memory:\n  retry:\n    backoff: 0\n", "backoff"),
]


@pytest.mark.parametrize(("yaml_text", "出错字段"), _越界记忆字段)
def test_记忆层越界值在加载期被拦下(tmp_path: Path, yaml_text: str, 出错字段: str) -> None:
    """守住 Field 约束的拒绝路径：越界值必须在 YAML 加载期就被 pydantic 拦下。

    直接构造模型只覆盖了一半的失败面；走 load_settings 才吃到真实的「配置 → 模型」路径。
    """
    with pytest.raises(ValidationError, match=出错字段):
        _ = _load(tmp_path, yaml_text)
