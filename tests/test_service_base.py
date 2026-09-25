"""Service 契约层测试：展示名、自带日志与统一错误格式。"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest
from loguru import logger

from core.config import LogSettings
from core.logger import faces, setup_logging
from core.service.base import UNSET, HealthStatus, Service, ServiceState, Unset


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


def test_log_error_可指定颜文字(tmp_path: Path) -> None:
    """face 是显式形参，不再依赖 **fields 解包时的偶然绑定。"""
    setup_logging(_cfg(tmp_path))
    DemoService().log_error("保存失败", RuntimeError("磁盘满"), face=faces.THINK)
    logger.remove()

    text = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
    assert faces.THINK in text


class StartTimeoutService(Service):
    """覆盖 start 超时：None 表示该服务不限制。"""

    start_timeout: ClassVar[float | Unset | None] = None


class StopTimeoutService(Service):
    """覆盖 stop 超时：写具体秒数。"""

    stop_timeout: ClassVar[float | Unset | None] = 1.5


def test_超时缺省为未覆盖哨兵() -> None:
    assert Service.start_timeout is UNSET
    assert Service.stop_timeout is UNSET
    assert repr(UNSET) == "UNSET"


def test_服务可覆盖超时三态() -> None:
    assert StartTimeoutService.start_timeout is None
    assert StopTimeoutService.stop_timeout == 1.5


def test_未覆盖的那一项仍跟随全局() -> None:
    """两个 ClassVar 各自独立，覆盖了一个不影响另一个。"""
    assert StartTimeoutService.stop_timeout is UNSET
    assert StopTimeoutService.start_timeout is UNSET


def test_to_dict_的保留键不被_extra_覆盖() -> None:
    """回归：extra 里与保留键同名的项一律让位，不得覆盖健康检查的元数据。"""
    status = HealthStatus(
        name="clock",
        healthy=True,
        state=ServiceState.RUNNING,
        detail="正常",
        extra={"name": "伪造", "healthy": False, "state": "伪造", "detail": "伪造", "延迟": 3},
    )

    payload = status.to_dict()

    assert payload["name"] == "clock"
    assert payload["healthy"] is True
    assert payload["state"] == "running"
    assert payload["detail"] == "正常"
    assert payload["延迟"] == 3
