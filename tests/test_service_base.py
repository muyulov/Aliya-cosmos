"""Service 契约层测试：展示名、自带日志与统一错误格式。"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest
from loguru import logger

from core.config import LogSettings
from core.logger import setup_logging
from core.service.base import Service


@pytest.fixture(autouse=True)
def _restore_logger():
    """每个用例前后重置 loguru，避免 sink 泄漏到其他测试。"""
    yield
    _ = logger.remove()


def _cfg(tmp_path: Path) -> LogSettings:
    return LogSettings(level="DEBUG", dir=str(tmp_path / "logs"), retention="1 day")


class DemoService(Service):
    """显式声明 name 的服务。"""

    name: ClassVar[str] = "demo"


class UnnamedService(Service):
    """不声明 name 的服务。"""


def test_展示名缺省取类名() -> None:
    assert UnnamedService().label == "UnnamedService"


def test_展示名显式优先于类名() -> None:
    assert DemoService().label == "demo"


def test_服务日志自动带服务字段与消息前缀(tmp_path: Path) -> None:
    setup_logging(_cfg(tmp_path))
    DemoService().log.info("演示")
    logger.remove()

    text = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
    assert "[demo] 演示" in text
    assert "服务: demo" in text


def test_门面可继续派生(tmp_path: Path) -> None:
    """self.log 是普通门面，仍可 bind / prefix 出更细的维度。"""
    setup_logging(_cfg(tmp_path))
    DemoService().log.bind(阶段="预热").info("派生日志")
    logger.remove()

    text = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
    assert "[demo] 派生日志" in text
    assert "服务: demo" in text
    assert "阶段: 预热" in text


def test_log_error_统一错误格式(tmp_path: Path) -> None:
    setup_logging(_cfg(tmp_path))
    DemoService().log_error("保存失败", RuntimeError("磁盘满"))
    logger.remove()

    text = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
    assert "错误: RuntimeError: 磁盘满" in text


def test_log_error_不传异常时不带错误字段(tmp_path: Path) -> None:
    setup_logging(_cfg(tmp_path))
    DemoService().log_error("仅提示")
    logger.remove()

    text = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
    assert "仅提示" in text
    assert "错误:" not in text


def test_log_error_可附加自定义字段(tmp_path: Path) -> None:
    setup_logging(_cfg(tmp_path))
    DemoService().log_error("保存失败", ValueError("坏值"), 重试次数=2)
    logger.remove()

    text = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
    assert "错误: ValueError: 坏值" in text
    assert "重试次数: 2" in text


def test_log_error_的自动错误字段优先于调用点传入(tmp_path: Path) -> None:
    """同时传 exc 与 错误= 时，以自动推导的「类型: 消息」为准。"""
    setup_logging(_cfg(tmp_path))
    DemoService().log_error("保存失败", RuntimeError("磁盘满"), 错误="自定义")
    logger.remove()

    text = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
    assert "错误: RuntimeError: 磁盘满" in text
    assert "错误: 自定义" not in text
