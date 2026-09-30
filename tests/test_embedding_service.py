"""向量服务测试。

约定：
- 不走 start() 建内核：容器只认配置节点，内核只能白盒塞进 service._encoder，
  因此除了「未配 key」那条走容器外，其余用例直接构造服务再注入假内核。
- 不起 mock server、不 mock HTTP：假内核按预设返回，不碰网络。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import cast

import httpx2
import pytest
from loguru import logger
from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, AsyncOpenAI, omit
from openai.types.create_embedding_response import CreateEmbeddingResponse, Usage
from openai.types.embedding import Embedding
from pydantic import ValidationError

from core.config import EmbeddingSettings, LogSettings, Settings
from core.embedding import (
    EmbeddingConfigError,
    EmbeddingConnectionError,
    EmbeddingError,
    EmbeddingInputError,
    EmbeddingRequestError,
    EmbeddingResponseError,
    EmbeddingService,
    EmbeddingTimeoutError,
    EncodedVector,
    EncodeResult,
    RemoteEncoder,
    build_encoder,
)
from core.logger import setup_logging
from core.service.base import ServiceState
from core.service.manager import ServiceManager

#: 测试用的端点，只用于构造假请求
_URL = "https://embed.test/v1/embeddings"


class _FakeEncoder:
    """内核替身：记录每次入参，按预设返回或抛错。

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
    """塞入替身：容器无法注入内核（构造器只认配置节点），这是唯一接缝。

    两个类型不重叠，先转 object 再转目标类型，否则撞 reportInvalidCast。
    """
    service._encoder = cast("RemoteEncoder", cast("object", fake))  # pyright: ignore[reportPrivateUsage]


def _status_error(status: int = 500, request_id: str = "req-1") -> APIStatusError:
    response = httpx2.Response(
        status, request=httpx2.Request("POST", _URL), headers={"x-request-id": request_id}
    )
    return APIStatusError("boom", response=response, body=None)


def _log_cfg(tmp_path: Path) -> LogSettings:
    """构造只关心落盘位置的日志配置。

    布局显式钉成 tree：下面的断言写的是树形字段格式（`键: 值`），与默认布局解耦。
    """
    return LogSettings(level="DEBUG", dir=str(tmp_path / "logs"), retention="1 day", layout="tree")


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
    # 缺密钥时也报出配置的身份：日志上才看得出这个服务接的是谁
    assert status.extra["模型"] == "embed-m"
    assert status.extra["端点"] == "https://embed.test/v1"


async def test_未配key时调用才报错() -> None:
    service = EmbeddingService(_settings(api_key=""))
    await service.start()

    with pytest.raises(EmbeddingConfigError, match="api_key"):
        _ = await service.embed("你好")


async def test_start重复调用不重建内核() -> None:
    """重复 start 不该建出第二个内核（前一个会被覆盖、没人 close）。"""
    service = EmbeddingService(_settings())
    await service.start()
    encoder = service._encoder  # pyright: ignore[reportPrivateUsage]

    await service.start()
    assert service._encoder is encoder  # pyright: ignore[reportPrivateUsage]

    await service.stop()


async def test_有内核时health健康且不发请求() -> None:
    service = EmbeddingService(_settings())
    fake = _FakeEncoder()
    _attach(service, fake)

    status = await service.health()

    assert status.healthy is True
    assert status.detail == ""
    assert status.extra == {"模型": "embed-m", "端点": "https://embed.test/v1"}
    assert fake.calls == []


async def test_stop幂等() -> None:
    service = EmbeddingService(_settings())
    fake = _FakeEncoder()
    _attach(service, fake)

    await service.stop()
    await service.stop()
    assert fake.close_count == 1


async def test_停止后health报未运行而不是未配密钥() -> None:
    """stop() 之后内核被清空，但密钥是配了的——不能再说「未配置 api_key」。"""
    mgr = ServiceManager(Settings(embedding=_settings()))
    _ = mgr.register(EmbeddingService)
    await mgr.start_all()
    await mgr.stop_all()

    status = await mgr.get(EmbeddingService).health()

    assert status.healthy is False
    assert status.state is ServiceState.STOPPED
    assert "未配置" not in status.detail
    assert "未运行" in status.detail


def test_非法配置被拒() -> None:
    with pytest.raises(ValidationError):
        _ = EmbeddingSettings(timeout=0)
    with pytest.raises(ValidationError):
        _ = EmbeddingSettings(retries=-1)
    with pytest.raises(ValidationError):
        _ = EmbeddingSettings(batch_size=0)
    # dimensions 是唯一的可空数值项：None 合法，0 / 负数不是
    assert EmbeddingSettings(dimensions=None).dimensions is None
    with pytest.raises(ValidationError):
        _ = EmbeddingSettings(dimensions=0)
    with pytest.raises(ValidationError):
        _ = EmbeddingSettings(dimensions=-1)


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


async def test_传字符串而不是序列时抛错() -> None:
    """str 本身就是 Sequence[str]：不拦就会按字符拆成 N 条向量（条数还自洽），静默出错。

    空串也要报错而不是走「空序列早退」——所以这道判断在早退之前。
    """
    service = EmbeddingService(_settings())
    fake = _FakeEncoder()
    _attach(service, fake)

    for text in ("你好", ""):
        with pytest.raises(EmbeddingInputError, match="embed"):
            _ = await service.embed_many(text)

    assert fake.calls == []


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


async def test_未知API错误映射成基类EmbeddingError() -> None:
    """不落在超时 / 状态码 / 连接三支里的 APIError，兜底映射到基类（不是某个子类）。"""
    service = EmbeddingService(_settings())
    _attach(
        service,
        _FakeEncoder(error=APIError("未知错误", httpx2.Request("POST", _URL), body=None)),
    )

    with pytest.raises(EmbeddingError) as info:
        _ = await service.embed("你好")

    assert type(info.value) is EmbeddingError
    assert "未知错误" in str(info.value)


# ---- 日志 ----


async def test_批数大于1时打汇总行(tmp_path: Path) -> None:
    cfg = _log_cfg(tmp_path)
    files = setup_logging(cfg)
    service = EmbeddingService(_settings(batch_size=2))
    _attach(service, _FakeEncoder(prompt_tokens=7))

    _ = await service.embed_many(["a", "bb", "ccc"])
    logger.remove()

    text = files.app.read_text(encoding="utf-8")
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
    files = setup_logging(cfg)
    service = EmbeddingService(_settings())
    _attach(service, _FakeEncoder(prompt_tokens=None))

    _ = await service.embed("你好")
    logger.remove()

    text = files.app.read_text(encoding="utf-8")
    assert "[embedding] 向量化完成" in text
    assert "批量向量化完成" not in text
    assert "输入token" not in text


# ---- 远程内核（真适配层）----


class _FakeEmbeddings:
    """embeddings 资源替身：记录入参并返回预设响应。"""

    def __init__(self, response: object) -> None:
        self.calls: list[dict[str, object]] = []
        self.response: object = response

    async def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return self.response


class _FakeSDKClient:
    """AsyncOpenAI 替身：只实现 RemoteEncoder 用到的那两个入口。"""

    def __init__(self, response: object) -> None:
        self.embeddings: _FakeEmbeddings = _FakeEmbeddings(response)
        self.close_count: int = 0

    async def close(self) -> None:
        self.close_count += 1


def _remote(*, usage: bool = True) -> tuple[RemoteEncoder, _FakeSDKClient]:
    """构造真内核 + SDK 替身。

    返回体故意把 index 倒序，用来验证内核不自行重排；usage=False 时不带用量字段。
    """
    data = [
        Embedding(embedding=[0.3, 0.4], index=1, object="embedding"),
        Embedding(embedding=[0.1, 0.2], index=0, object="embedding"),
    ]
    if usage:
        response: object = CreateEmbeddingResponse(
            data=data, model="embed-m", object="list", usage=Usage(prompt_tokens=7, total_tokens=7)
        )
    else:
        # 兼容端点不返回 usage 时该字段会被填成 None，类型上表达不出来，只能绕过校验构造
        response = CreateEmbeddingResponse.model_construct(
            data=data, model="embed-m", object="list", usage=None
        )
    client = _FakeSDKClient(response)
    return RemoteEncoder(cast("AsyncOpenAI", cast("object", client)), "embed-m"), client


async def test_远程内核把位置与用量原样报出() -> None:
    """回归：SDK 返回体到 EncodeResult 的映射只有这里验证，真适配层没有别处覆盖。"""
    encoder, client = _remote()

    result = await encoder.encode(["a", "b"], dimensions=None)

    assert [item.index for item in result.items] == [1, 0]  # 不重排，位置原样上报
    assert [item.vector for item in result.items] == [[0.3, 0.4], [0.1, 0.2]]
    assert result.prompt_tokens == 7
    call = client.embeddings.calls[0]
    assert call["model"] == "embed-m"
    assert call["input"] == ["a", "b"]


async def test_未声明维度时传_omit_而不是_none() -> None:
    """回归：显式 None 会被序列化成 JSON null（端点多半 400），「不传」必须走 omit。"""
    encoder, client = _remote()

    _ = await encoder.encode(["a"], dimensions=None)

    assert client.embeddings.calls[0]["dimensions"] is omit


async def test_声明的维度原样传下去() -> None:
    encoder, client = _remote()

    _ = await encoder.encode(["a"], dimensions=512)

    assert client.embeddings.calls[0]["dimensions"] == 512


async def test_端点不返回用量时用量为空() -> None:
    """回归：缺 usage 时 construct_type 会把字段填成 None，直接取属性会炸。"""
    encoder, _ = _remote(usage=False)

    result = await encoder.encode(["a"], dimensions=None)

    assert result.prompt_tokens is None


async def test_远程内核关闭时转发给_SDK_客户端() -> None:
    encoder, client = _remote()

    await encoder.aclose()

    assert client.close_count == 1


async def test_工厂显式传入超时与重试() -> None:
    """SDK 默认 timeout 是 10 分钟、max_retries 是 2：不能让默认值随版本漂移。"""
    config = EmbeddingSettings(api_key="k", model="embed-m", timeout=12.5, retries=3)
    encoder = build_encoder(config)
    client = cast("AsyncOpenAI", cast("object", encoder._client))  # pyright: ignore[reportPrivateUsage]

    try:
        assert client.max_retries == 3
        assert client.timeout == 12.5
    finally:
        await encoder.aclose()
