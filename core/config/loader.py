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
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol, cast

import yaml
from dotenv import dotenv_values

#: 配置骨架文件，相对当前工作目录
CONFIG_FILE = Path("data/config/app.yaml")
#: 存放密钥等隐私值的文件，相对当前工作目录
ENV_FILE = Path(".env")

#: 占位符：${VAR} 或 ${VAR:默认值}；$$ 是字面 $ 的转义写法。
_PLACEHOLDER = re.compile(r"\$\$|\$\{([A-Za-z_]\w*)(?::([^}]*))?\}")
#: $$ 在展开期间的替身，每趟展开结束后统一还原成 $。
#: 用 NUL 是因为 YAML 解析器拒绝含 NUL 的文本，它不可能来自配置本身；若当场还原成 $，
#: 多趟展开时被转义掉的 {…} 会在下一趟重新被当成占位符。
_ESCAPED_DOLLAR = "\x00"


class ConfigError(Exception):
    """配置错误。

    不是 AppError，因此不会被转成 4xx：配置在 import 期加载，出错即让进程退出。
    """


class EnvLookup(Protocol):
    """按名取占位符值的通道：插值只需要这一个能力，普通 dict 也能顶上。

    形参写成仅位置（`/`），才能同时匹配 `dict.__getitem__` 与 `Mapping.__getitem__`。
    """

    def __getitem__(self, name: str, /) -> str: ...


class EnvSource:
    """占位符取值表：进程环境变量优先，.env 文件作为回退。

    构造时不把 os.environ 整表拷进来（进程里可能上千条），而是按需查询：
    插值实际只会碰配置里写到的那几个名字。
    """

    def __init__(self, file_values: Mapping[str, str]) -> None:
        self._file_values: dict[str, str] = dict(file_values)

    def __getitem__(self, name: str) -> str:
        try:
            return os.environ[name]
        except KeyError:
            return self._file_values[name]


def read_env(env_file: Path = ENV_FILE) -> EnvSource:
    """读取 .env 并包成取值表（进程环境变量优先）。

    python-dotenv 遇到文件不存在会走标准库 logging 发警告，这里先判存在，
    让「没有 .env」这个正常情形保持安静。
    """
    values: dict[str, str] = {}
    if env_file.is_file():
        parsed = dotenv_values(env_file)
        values.update({key: value for key, value in parsed.items() if value is not None})
    return EnvSource(values)


def read_yaml(config_file: Path = CONFIG_FILE) -> dict[str, object] | None:
    """读取 YAML，返回原始数据；文件不存在时返回 None，由调用方回退到内置默认值。

    返回 None 与返回空字典是两回事：前者是「文件不在」，后者是「文件在但没写内容」，
    配置来源的描述据此区分。

    读取与解析阶段的错误统一收敛成 ConfigError：编码错、权限错、YAML 语法错
    都不该以裸异常的形式冒到调用方。
    """
    if not config_file.is_file():
        return None
    try:
        text = config_file.read_text(encoding="utf-8")
        # safe_load 的返回类型是 Any，用 cast 显式收敛成「YAML 顶层可能出现的形态」，
        # 否则 Any 会顺着 interpolate 一路渗到 Settings.model_validate 的类型检查里。
        # 注意不能只靠 `x: T = ...` 注解：basedpyright 仍会对赋值行报 reportAny。
        loaded = cast("dict[str, object] | list[object] | None", yaml.safe_load(text))
    except UnicodeDecodeError as exc:
        raise ConfigError(f"配置文件不是合法的 UTF-8 文本：{config_file}：{exc}") from exc
    except OSError as exc:
        raise ConfigError(f"配置文件读取失败：{config_file}：{exc}") from exc
    except yaml.YAMLError as exc:
        # 捕基类而非 MarkedYAMLError：ReaderError（控制字符、非法字节）只是 YAMLError
        # 的子类、不带行列标记，漏掉它就会有裸异常绕过「加载期错误统一为 ConfigError」。
        raise ConfigError(f"配置文件解析失败：{config_file}：{exc}") from exc
    if loaded is None:  # 空文件
        return {}
    if not isinstance(loaded, dict):
        raise ConfigError(f"配置文件顶层必须是映射：{config_file}，实际是 {type(loaded).__name__}")
    return loaded


def interpolate(
    value: object,
    sources: EnvLookup,
    *,
    path: str = "",
    rounds: int = 1,
) -> object:
    """递归展开 ${VAR} / ${VAR:默认值}，并把 $$ 还原成字面 $。

    - 只作用于 str 标量，非字符串原样返回；
    - dict 的 key 不插值，只处理 value；
    - `rounds` 是最大展开趟数：默认 1（单趟），变量值里的占位符不再展开，避免链式取值
      带来的隐式依赖；需要「A 的值里再写 ${B}」时显式传 2 及以上。
    """
    if isinstance(value, str):
        return _expand(value, sources, path, rounds)
    if isinstance(value, dict):
        # 从一个 object 收窄出来的 dict，键值类型是 Unknown；用 cast 收敛成 dict[object, object]
        # 才不会让 Unknown 顺着递归调用继续渗下去（只写注解挡不住，收窄类型会盖掉注解）。
        mapping = cast("dict[object, object]", value)
        return {
            key: interpolate(
                item,
                sources,
                path=f"{path}.{key}" if path else str(key),
                rounds=rounds,
            )
            for key, item in mapping.items()
        }
    if isinstance(value, list):
        sequence = cast("list[object]", value)
        return [
            interpolate(item, sources, path=f"{path}[{index}]", rounds=rounds)
            for index, item in enumerate(sequence)
        ]
    return value


def _expand(text: str, sources: EnvLookup, path: str, rounds: int) -> str:
    """对单个字符串做至多 `rounds` 趟展开，最后还原被转义掉的 $。"""
    result = text
    for _ in range(max(rounds, 1)):
        expanded = _PLACEHOLDER.sub(lambda match: _resolve(match, sources, path), result)
        if expanded == result:  # 没有可展开的占位符了，提前收手
            break
        result = expanded
    return result.replace(_ESCAPED_DOLLAR, "$")


def _resolve(match: re.Match[str], sources: EnvLookup, path: str) -> str:
    """替换单个 token：$$ 记成字面 $，${…} 取值表中的值，其次默认值，都没有则报错。"""
    if match.group(0) == "$$":
        return _ESCAPED_DOLLAR
    name = match.group(1)
    default = match.group(2)
    try:
        return sources[name]
    except KeyError:
        if default is not None:
            return default
        raise ConfigError(
            f"占位符 ${{{name}}} 未定义且无默认值（位置：{path or '顶层'}）",
        ) from None
