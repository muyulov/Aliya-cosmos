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
            interpolate(item, sources, path=f"{path}[{index}]") for index, item in enumerate(value)
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
