"""向量服务测试。

约定：
- 不走 start() 建内核：容器只认配置节点，内核只能白盒塞进 service._encoder，
  因此除了「未配 key」那条走容器外，其余用例直接构造服务再注入假内核。
- 不起 mock server、不 mock HTTP：假内核按预设返回，不碰网络。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import override

import httpx2
import pytest
from loguru import logger
from openai import APIConnectionError, APIStatusError, APITimeoutError
from pydantic import ValidationError

from core.config import EmbeddingSettings, LogSettings, Settings
from core.embedding import (
    EmbeddingConfigError,
    EmbeddingConnectionError,
    EmbeddingInputError,
    EmbeddingRequestError,
    EmbeddingResponseError,
    EmbeddingService,
    EmbeddingTimeoutError,
    EncodedVector,
    Encoder,
    EncodeResult,
)
from core.logger import setup_logging
from core.service.base import ServiceState
from core.service.manager import ServiceManager

#: 测试用的端点，只用于构造假请求
_URL = "https://embed.test/v1/embeddings"


class _FakeEncoder:
    """Encoder 替身：记录每次入参，按预设返回或抛错。

    向量首元素就是入参位置，便于断言归位结果；默认兑现传入的 dimensions，
    `ignore_dimensions=True` 时不兑现（用来造「端点静默忽略该参数」的场景）。
    """

    def __init__(
        self,
        *,
        dimension: int = 4,
        prompt_tokens: int | None = 7,
        error: Exception | None = None,
        ignore_dimensions: bool = False,
    ) -> None:
        self.calls: list[dict[str, object]] = []
        self.texts: list[list[str]] = []
        self.batch_sizes: list[int] = []
        self.close_count: int = 0
        self.dimension: int = dimension
        self.prompt_tokens: int | None = prompt_tokens
        self.error: Exception | None = error
        self.ignore_dimensions: bool = ignore_dimensions
        self.shuffle: bool = False
        self.indices: list[int] | None = None

    async def encode(self, texts: Sequence[str], *, dimensions: int | None = None) -> EncodeResult:
        self.calls.append({"texts": list(texts), "dimensions": dimensions})
        self.texts.append(list(texts))
        self.batch_sizes.append(len(texts))
        if self.error is not None:
            raise self.error
        size = self.dimension if self.ignore_dimensions or dimensions is None else dimensions
        items = [
            EncodedVector(index=index, vector=[float(index)] * size) for index in range(len(texts))
        ]
        if self.shuffle:
            items.reverse()
        if self.indices is not None:
            items = [
                EncodedVector(index=self.indices[i], vector=item.vector)
                for i, item in enumerate(items[: len(self.indices)])
            ]
        return EncodeResult(items=tuple(items), prompt_tokens=self.prompt_tokens)

    async def aclose(self) -> None:
        self.close_count += 1


def _settings(
    *,
    api_key: str = "k",
    model: str = "embed-m",
    batch_size: int = 10,
    dimensions: int | None = None,
) -> EmbeddingSettings:
    return EmbeddingSettings(
        api_key=api_key,
        model=model,
        base_url="https://embed.test/v1",
        batch_size=batch_size,
        dimensions=dimensions,
    )


def _attach(service: EmbeddingService, fake: _FakeEncoder) -> None:
    """塞入替身：容器无法注入内核（构造器只认配置节点），这是唯一接缝。"""
    service._encoder = fake  # pyright: ignore[reportPrivateUsage]


def _status_error(status: int = 500, request_id: str = "req-1") -> APIStatusError:
    response = httpx2.Response(
        status, request=httpx2.Request("POST", _URL), headers={"x-request-id": request_id}
    )
    return APIStatusError("boom", response=response, body=None)


def _log_cfg(tmp_path: Path) -> LogSettings:
    """构造只关心落盘位置的日志配置。"""
    return LogSettings(level="DEBUG", dir=str(tmp_path / "logs"), retention="1 day")


class _CountingService(EmbeddingService):
    """记 _make_encoder 被调了几次，用来验证 start 的幂等。"""

    def __init__(self, config: EmbeddingSettings) -> None:
        super().__init__(config)
        self.build_count: int = 0

    @override
    def _make_encoder(self, config: EmbeddingSettings) -> Encoder:
        self.build_count += 1
        return super()._make_encoder(config)


# ---- 生命周期与配置 ----


async def test_未配key时start不建内核() -> None:
    mgr = ServiceManager(Settings(embedding=_settings(api_key="")))
    _ = mgr.register(EmbeddingService)
    await mgr.start_all()

    service = mgr.get(EmbeddingService)
    assert service.state is ServiceState.RUNNING
    assert service._encoder is None  # pyright: ignore[reportPrivateUsage]
    status = await service.health()
    assert status.healthy is False
    assert "未配置" in status.detail


async def test_未配key时调用才报错() -> None:
    service = EmbeddingService(_settings(api_key=""))
    await service.start()

    with pytest.raises(EmbeddingConfigError, match="api_key"):
        _ = await service.embed("你好")


async def test_start重复调用不重建内核() -> None:
    """重复 start 不该建出第二个内核（前一个会被覆盖、没人 close）。"""
    service = _CountingService(_settings())
    await service.start()
    encoder = service._encoder  # pyright: ignore[reportPrivateUsage]

    await service.start()
    await service.stop()

    assert service.build_count == 1
    assert encoder is not None


async def test_有内核时health健康且不发请求() -> None:
    service = EmbeddingService(_settings())
    fake = _FakeEncoder()
    _attach(service, fake)

    status = await service.health()

    assert status.healthy is True
    assert status.detail == ""
    assert fake.calls == []


async def test_stop幂等() -> None:
    service = EmbeddingService(_settings())
    fake = _FakeEncoder()
    _attach(service, fake)

    await service.stop()
    await service.stop()
    assert fake.close_count == 1


def test_非法配置被拒() -> None:
    with pytest.raises(ValidationError):
        _ = EmbeddingSettings(timeout=0)
    with pytest.raises(ValidationError):
        _ = EmbeddingSettings(retries=-1)
    with pytest.raises(ValidationError):
        _ = EmbeddingSettings(batch_size=0)


# ---- 单条 ----


async def test_embed返回单条向量() -> None:
    service = EmbeddingService(_settings())
    fake = _FakeEncoder(dimension=3)
    _attach(service, fake)

    vector = await service.embed("你好")

    assert vector == [0.0, 0.0, 0.0]
    assert fake.texts[0] == ["你好"]


async def test_dimensions三级优先() -> None:
    """调用覆盖 > 配置 > 不传。"""
    service = EmbeddingService(_settings(dimensions=512))
    fake = _FakeEncoder()
    _attach(service, fake)

    _ = await service.embed("文本", dimensions=768)
    _ = await service.embed("文本")

    assert fake.calls[0]["dimensions"] == 768
    assert fake.calls[1]["dimensions"] == 512


async def test_未配置dimensions时不传该参数() -> None:
    """不传而不是传 None：端点收到 null 多半当 0 或直接 400。"""
    service = EmbeddingService(_settings())
    fake = _FakeEncoder()
    _attach(service, fake)

    _ = await service.embed("文本")

    assert fake.calls[0]["dimensions"] is None


# ---- 批量 ----


async def test_批量按batch_size切分() -> None:
    service = EmbeddingService(_settings(batch_size=10))
    fake = _FakeEncoder()
    _attach(service, fake)

    vectors = await service.embed_many([f"文本{i}" for i in range(25)])

    assert fake.batch_sizes == [10, 10, 5]
    assert len(vectors) == 25


async def test_批量乱序返回按index归位() -> None:
    """兼容端点可能乱序返回：结果必须按入参顺序，而不是端点顺序。"""
    service = EmbeddingService(_settings())
    fake = _FakeEncoder()
    fake.shuffle = True
    _attach(service, fake)

    vectors = await service.embed_many(["a", "bb", "ccc"])

    assert [vector[0] for vector in vectors] == [0.0, 1.0, 2.0]


async def test_index越界或重复时抛错() -> None:
    service = EmbeddingService(_settings())
    fake = _FakeEncoder()
    _attach(service, fake)

    fake.indices = [0, 1, 3]  # 越界：合法位置只有 0..2
    with pytest.raises(EmbeddingResponseError, match="index"):
        _ = await service.embed_many(["a", "b", "c"])

    fake.indices = [0, 0, 1]
    with pytest.raises(EmbeddingResponseError, match="index"):
        _ = await service.embed_many(["a", "b", "c"])


async def test_条数不符时抛错() -> None:
    service = EmbeddingService(_settings())
    fake = _FakeEncoder()
    fake.indices = [0]
    _attach(service, fake)

    with pytest.raises(EmbeddingResponseError, match="index"):
        _ = await service.embed_many(["a", "b", "c"])


async def test_空序列早退不发请求() -> None:
    service = EmbeddingService(_settings())
    fake = _FakeEncoder()
    _attach(service, fake)

    assert await service.embed_many([]) == []
    assert fake.calls == []


async def test_空序列未配key也返回空() -> None:
    """刻意行为：空输入不需要服务，不查密钥也不报错（README 已写明）。"""
    service = EmbeddingService(_settings(api_key=""))

    assert await service.embed_many([]) == []


async def test_空串与全空白串抛EmbeddingInputError() -> None:
    service = EmbeddingService(_settings())
    fake = _FakeEncoder()
    _attach(service, fake)

    with pytest.raises(EmbeddingInputError):
        _ = await service.embed("")
    with pytest.raises(EmbeddingInputError):
        _ = await service.embed("   ")
    assert fake.calls == []


async def test_批量中有空串时一批都不发() -> None:
    """先全量校验再发请求：否则前几批白花钱才发现第 3 条是空串。"""
    service = EmbeddingService(_settings(batch_size=1))
    fake = _FakeEncoder()
    _attach(service, fake)

    with pytest.raises(EmbeddingInputError):
        _ = await service.embed_many(["a", "b", ""])

    assert fake.calls == []


# ---- 返回体校验 ----


async def test_声明维度与返回不符时抛错() -> None:
    """端点静默忽略 dimensions 时，只有这里能发现。"""
    service = EmbeddingService(_settings())
    fake = _FakeEncoder(dimension=4, ignore_dimensions=True)
    _attach(service, fake)

    with pytest.raises(EmbeddingResponseError, match="512"):
        _ = await service.embed("你好", dimensions=512)


async def test_返回空向量时抛错() -> None:
    service = EmbeddingService(_settings())
    _attach(service, _FakeEncoder(dimension=0))

    with pytest.raises(EmbeddingResponseError, match="空向量"):
        _ = await service.embed("你好")


# ---- 错误映射 ----


async def test_状态码错误映射成EmbeddingRequestError() -> None:
    service = EmbeddingService(_settings())
    _attach(service, _FakeEncoder(error=_status_error()))

    with pytest.raises(EmbeddingRequestError) as info:
        _ = await service.embed("你好")

    assert info.value.status_code == 500
    assert info.value.request_id == "req-1"
    assert info.value.model == "embed-m"


async def test_超时映射成EmbeddingTimeoutError() -> None:
    """APITimeoutError 是 APIConnectionError 的子类，捕错顺序错了就会误判。"""
    service = EmbeddingService(_settings())
    _attach(service, _FakeEncoder(error=APITimeoutError(request=httpx2.Request("POST", _URL))))

    with pytest.raises(EmbeddingTimeoutError):
        _ = await service.embed("你好")


async def test_连接错误映射成EmbeddingConnectionError() -> None:
    service = EmbeddingService(_settings())
    _attach(
        service,
        _FakeEncoder(
            error=APIConnectionError(message="连不上", request=httpx2.Request("POST", _URL))
        ),
    )

    with pytest.raises(EmbeddingConnectionError):
        _ = await service.embed("你好")


# ---- 日志 ----


async def test_批数大于1时打汇总行(tmp_path: Path) -> None:
    cfg = _log_cfg(tmp_path)
    setup_logging(cfg)
    service = EmbeddingService(_settings(batch_size=2))
    _attach(service, _FakeEncoder(prompt_tokens=7))

    _ = await service.embed_many(["a", "bb", "ccc"])
    logger.remove()

    text = (Path(cfg.dir) / cfg.file_name).read_text(encoding="utf-8")
    assert "[embedding] 向量化完成" in text
    assert "[embedding] 批量向量化完成" in text
    assert "批数: 2" in text
    assert "文本数: 2" in text
    assert "维度: 4" in text
    assert "耗时毫秒: " in text
    assert "输入token: 7" in text


async def test_单批不打汇总行且无用量时省略字段(tmp_path: Path) -> None:
    """端点没返回 usage 时省略该字段，不因为没得记而报错。"""
    cfg = _log_cfg(tmp_path)
    setup_logging(cfg)
    service = EmbeddingService(_settings())
    _attach(service, _FakeEncoder(prompt_tokens=None))

    _ = await service.embed("你好")
    logger.remove()

    text = (Path(cfg.dir) / cfg.file_name).read_text(encoding="utf-8")
    assert "[embedding] 向量化完成" in text
    assert "批量向量化完成" not in text
    assert "输入token" not in text
