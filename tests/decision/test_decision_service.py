"""判断服务测试。

约定：
- 客户端在 start() 里建，容器又不认它，所以除「未启用」那条外一律白盒塞 service._clients。
- 用 httpx2.MockTransport 注入 handler：不起 mock server、不打桩 socket，
  handler 直接拿到 httpx2.Request，可断言请求体与请求头（本层最值得断言的东西）。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import cast

import httpx2
import pytest

import core.decision.service as decision_service
from core.config import DecisionEndpointSettings, DecisionSettings, LogSettings, Settings
from core.decision import (
    DecisionConfigError,
    DecisionConnectionError,
    DecisionRequestError,
    DecisionResponseError,
    DecisionResult,
    DecisionService,
    DecisionTimeoutError,
    choice,
    noul,
    score,
)
from core.decision.service import _QID  # pyright: ignore[reportPrivateUsage]
from core.logger import setup_logging
from core.service.base import ServiceState
from core.service.manager import ServiceManager


def _service(
    handler: Callable[[httpx2.Request], httpx2.Response],
) -> DecisionService:
    """构造一个已「启动」的服务：客户端直接白盒塞进去。"""
    settings = DecisionSettings()
    service = DecisionService(settings)
    service._clients = {  # pyright: ignore[reportPrivateUsage]
        "laya": httpx2.AsyncClient(
            transport=httpx2.MockTransport(handler), base_url=settings.laya.base_url
        )
    }
    return service


async def test_predict_请求体与返回体() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(
            200,
            json={
                "model": "laya-multilingual",
                "answers": {"退款": {"type": "noul", "noul": 0.93}},
                "routing": {"model": "multilingual"},
                "usage": {"input_tokens": 11, "output_tokens": 0},
            },
        )

    service = _service(handler)

    result = await service.predict("我要退款", {"退款": noul("是否要求退款？")})

    assert isinstance(result, DecisionResult)
    assert result.model == "laya-multilingual"
    assert result.answers["退款"] == {"type": "noul", "noul": 0.93}
    assert (result.input_tokens, result.output_tokens) == (11, 0)
    assert result.routing == {"model": "multilingual"}
    assert seen[0].url.path == "/v1/systemone"
    assert seen[0].method == "POST"
    body = cast("dict[str, object]", json.loads(seen[0].content))
    assert body["state"] == "我要退款"
    assert body["questions"] == {"退款": {"type": "noul", "instructions": "是否要求退款？"}}


async def test_laya默认端点且model为空时不带该字段() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"model": "m", "answers": {}, "usage": {}})

    service = _service(handler)

    _ = await service.predict("state", {})

    assert "model" not in json.loads(seen[0].content)


async def test_jev端点带上配置的model() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"model": "jev-1.13.0", "answers": {}, "usage": {}})

    service = _service(handler)
    service._clients["jev"] = httpx2.AsyncClient(  # pyright: ignore[reportPrivateUsage]
        transport=httpx2.MockTransport(handler), base_url="https://api.typesafe.ai/v1"
    )

    _ = await service.predict("state", {}, backend="jev")

    assert json.loads(seen[0].content)["model"] == "jev-latest"


async def test_usage缺失时不报错() -> None:
    service = _service(lambda request: httpx2.Response(200, json={"model": "m", "answers": {}}))

    result = await service.predict("state", {})

    assert (result.input_tokens, result.output_tokens) == (None, None)


async def test_未启用端点调用报配置错误() -> None:
    service = DecisionService(DecisionSettings())

    with pytest.raises(DecisionConfigError) as excinfo:
        _ = await service.predict("state", {})

    assert excinfo.value.__cause__ is None


async def test_显式选jev但未启用报配置错误() -> None:
    """默认走 laya 那条不算数：层内不兜底，显式点名未启用的端点也要当场报错。

    这里 laya 是启用的，如果实现里做了兜底（静默换另一头）就会悄悄成功——正是
    设计上要避免的：两端 confidence 语义不同，换后端必然出错。
    """
    service = DecisionService(_enabled())
    await service.start()

    with pytest.raises(DecisionConfigError, match="jev"):
        _ = await service.predict("state", {}, backend="jev")

    await service.stop()


async def test_答案不是对象时报返回体错误() -> None:
    """回归：answers 只校验到「是不是 dict」，value 形态漏到下游会成 AttributeError。"""
    service = _service(
        lambda request: httpx2.Response(200, json={"model": "m", "answers": {_QID: 5}})
    )

    with pytest.raises(DecisionResponseError) as excinfo:
        _ = await service.ask("state", "是否？")

    assert "answer" in str(excinfo.value)


async def test_状态码错误带上对账坐标() -> None:
    service = _service(
        lambda request: httpx2.Response(
            422, json={"error": "bad"}, headers={"x-request-id": "req-7"}
        )
    )

    with pytest.raises(DecisionRequestError) as excinfo:
        _ = await service.predict("state", {})

    assert excinfo.value.status_code == 422
    assert excinfo.value.request_id == "req-7"
    assert excinfo.value.endpoint == DecisionSettings().laya.base_url


async def test_超时映射成超时错误() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("too slow", request=request)

    with pytest.raises(DecisionTimeoutError):
        _ = await _service(handler).predict("state", {})


async def test_连不上映射成连接错误() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route", request=request)

    with pytest.raises(DecisionConnectionError):
        _ = await _service(handler).predict("state", {})


async def test_非法JSON报返回体错误() -> None:
    service = _service(lambda request: httpx2.Response(200, content=b"not json"))

    with pytest.raises(DecisionResponseError, match="不是合法 JSON"):
        _ = await service.predict("state", {})


async def test_响应体不是合法UTF8报返回体错误() -> None:
    """回归：非 UTF-8 响应体抛的是 UnicodeDecodeError，不是 json.JSONDecodeError。

    它抛在 `_body` 里、又在 `_wrap_errors` 之外，只收 JSONDecodeError 会裸抛出去。
    b"\\xff\\xfe" 是 UTF-16LE 的 BOM，因此报错消息里带 utf-16-le，可据此确认
    确实走的是 UnicodeDecodeError 那条 except 而不是别的路径。
    """
    service = _service(lambda request: httpx2.Response(200, content=b"\xff\xfe\xfa"))

    with pytest.raises(DecisionResponseError, match="不是合法 JSON") as excinfo:
        _ = await service.predict("state", {})

    assert isinstance(excinfo.value.__cause__, UnicodeDecodeError)


async def test_其余请求错误映射成连接错误() -> None:
    """回归：RequestError 的宽底座。

    TooManyRedirects 是 RequestError 却既不是 TransportError 也不是 DecodingError，
    专门捕子类的写法兜不住它；宽底座兜住，避免裸抛穿透 DecisionError 树。
    """
    settings = DecisionSettings()
    service = DecisionService(settings)
    service._clients = {  # pyright: ignore[reportPrivateUsage]
        "laya": httpx2.AsyncClient(
            transport=httpx2.MockTransport(
                lambda request: httpx2.Response(302, headers={"location": "/loop"})
            ),
            base_url=settings.laya.base_url,
            follow_redirects=True,
            max_redirects=3,
        )
    }

    with pytest.raises(DecisionConnectionError, match="请求") as excinfo:
        _ = await service.predict("state", {})

    assert isinstance(excinfo.value.__cause__, httpx2.TooManyRedirects)


async def test_响应体解不开压缩时报返回体错误() -> None:
    """回归：声明了 Content-Encoding: gzip 但内容不是 gzip 体。

    httpx2 在 client.post 内部解码响应体时抛 DecodingError——它是 RequestError
    的子类，却**不是** TransportError，`_wrap_errors` 少了宽底座就会裸抛。
    """
    service = _service(
        lambda request: httpx2.Response(
            200, content=b"not gzip", headers={"content-encoding": "gzip"}
        )
    )

    with pytest.raises(DecisionResponseError, match="返回体解码失败") as excinfo:
        _ = await service.predict("state", {})

    assert isinstance(excinfo.value.__cause__, httpx2.DecodingError)


async def test_端点返回答案里的额外字段原样透传() -> None:
    """设计决策 7：answers 原样透传、不建模，Laya 专有字段不能被静默吃掉。

    全量 pydantic 建模（配合 extra="ignore"）会把这些字段吞掉，而这正是选薄层
    的理由，所以断言值本身而不只是键存在。
    """
    service = _service(
        lambda request: httpx2.Response(
            200,
            json={
                "model": "m",
                "answers": {
                    _QID: {
                        "type": "choice",
                        "choice": "billing",
                        "probabilities": {"billing": 0.9, "other": 0.1},
                        "confidence": 0.8,
                        "answer_confidence": 0.55,
                        "abstention": False,
                        "low_confidence": True,
                    }
                },
                "usage": {},
            },
        )
    )

    result = await service.predict("state", {_QID: choice("选一个", {"billing": None})})

    answer = result.answers[_QID]
    assert answer["answer_confidence"] == 0.55
    assert answer["abstention"] is False
    assert answer["low_confidence"] is True


async def test_顶层不是对象报返回体错误() -> None:
    service = _service(lambda request: httpx2.Response(200, json=[1, 2]))

    with pytest.raises(DecisionResponseError, match="顶层不是对象"):
        _ = await service.predict("state", {})


async def test_缺answers报返回体错误() -> None:
    service = _service(lambda request: httpx2.Response(200, json={"model": "m"}))

    with pytest.raises(DecisionResponseError, match="缺少 answers"):
        _ = await service.predict("state", {})


async def test_429退避后成功(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(decision_service, "_BACKOFF_BASE", 0.0)
    calls: list[int] = []

    def handler(_request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx2.Response(429, json={})
        return httpx2.Response(200, json={"model": "m", "answers": {}, "usage": {}})

    _ = await _service(handler).predict("state", {})

    assert len(calls) == 2


async def test_529退避后成功(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(decision_service, "_BACKOFF_BASE", 0.0)
    calls: list[int] = []

    def handler(_request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx2.Response(529, json={})
        return httpx2.Response(200, json={"model": "m", "answers": {}, "usage": {}})

    _ = await _service(handler).predict("state", {})

    assert len(calls) == 2


async def test_重试耗尽后报错(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(decision_service, "_BACKOFF_BASE", 0.0)
    calls: list[int] = []

    def handler(_request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        return httpx2.Response(429, json={})

    with pytest.raises(DecisionRequestError):
        _ = await _service(handler).predict("state", {})

    assert len(calls) == 3  # retries=2 → 总请求数 3


async def test_retries为零时不重试(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(decision_service, "_BACKOFF_BASE", 0.0)
    calls: list[int] = []

    def handler(_request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        return httpx2.Response(429, json={})

    service = DecisionService(
        DecisionSettings(
            laya=DecisionEndpointSettings(enabled=True, base_url="http://laya.test/v1", retries=0)
        )
    )
    service._clients = {  # pyright: ignore[reportPrivateUsage]
        "laya": httpx2.AsyncClient(
            transport=httpx2.MockTransport(handler), base_url="http://laya.test/v1"
        )
    }

    with pytest.raises(DecisionRequestError):
        _ = await service.predict("state", {})

    assert len(calls) == 1


async def test_其他4xx不重试() -> None:
    calls: list[int] = []

    def handler(_request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        return httpx2.Response(400, json={})

    with pytest.raises(DecisionRequestError):
        _ = await _service(handler).predict("state", {})

    assert len(calls) == 1


_CHOICE_BODY = {
    "model": "m",
    "answers": {
        _QID: {
            "type": "choice",
            "choice": "billing",
            "probabilities": {"billing": 0.88, "other": 0.12},
            "confidence": 0.81,
        }
    },
    "usage": {"input_tokens": 5, "output_tokens": 0},
}
_SCORE_BODY = {
    "model": "m",
    "answers": {
        _QID: {
            "type": "score",
            "score": 1.05,
            "legend": {"0": "平静", "1": "不满", "2": "愤怒"},
            "probabilities": {"0": 0.0, "1": 0.95, "2": 0.05},
            "confidence": 0.92,
        }
    },
    "usage": {"input_tokens": 5, "output_tokens": 0},
}
_NOUL_BODY = {
    "model": "m",
    "answers": {_QID: {"type": "noul", "noul": 0.93}},
    "usage": {"input_tokens": 5, "output_tokens": 0},
}


async def test_choose返回选中选项与分布() -> None:
    service = _service(lambda request: httpx2.Response(200, json=_CHOICE_BODY))

    answer = await service.choose("要退款", "归哪个部门？", {"billing": "账单", "other": None})

    assert answer.choice == "billing"
    assert answer.probabilities["billing"] == 0.88
    assert answer.confidence == 0.81


async def test_rate返回得分与档位说明() -> None:
    service = _service(lambda request: httpx2.Response(200, json=_SCORE_BODY))

    answer = await service.rate("钱扣了两次", "愤怒程度？", ["平静", "不满", "愤怒"])

    assert answer.score == 1.05
    assert answer.legend["1"] == "不满"
    assert answer.probabilities["1"] == 0.95
    assert answer.confidence == 0.92


async def test_ask返回是概率() -> None:
    service = _service(lambda request: httpx2.Response(200, json=_NOUL_BODY))

    assert await service.ask("我要退款", "是否要求退款？") == 0.93


async def test_便捷方法缺答案键时报错() -> None:
    service = _service(lambda request: httpx2.Response(200, json={"model": "m", "answers": {}}))

    with pytest.raises(DecisionResponseError) as excinfo:
        _ = await service.ask("state", "是否？")

    assert "answer" in str(excinfo.value)


async def test_便捷方法字段不完整时报错() -> None:
    service = _service(
        lambda request: httpx2.Response(
            200,
            json={
                "model": "m",
                "answers": {_QID: {"type": "choice", "choice": "billing"}},
                "usage": {},
            },
        )
    )

    with pytest.raises(DecisionResponseError):
        _ = await service.choose("state", "选一个", {"billing": None})


async def test_score字段不完整时报错() -> None:
    service = _service(
        lambda request: httpx2.Response(
            200,
            json={
                "model": "m",
                "answers": {_QID: {"type": "score", "score": 1.0}},
                "usage": {},
            },
        )
    )

    with pytest.raises(DecisionResponseError):
        _ = await service.rate("state", "打分", ["低", "高"])


async def test_noul字段类型不对时报错() -> None:
    service = _service(
        lambda request: httpx2.Response(
            200,
            json={"model": "m", "answers": {_QID: {"type": "noul", "noul": "yes"}}, "usage": {}},
        )
    )

    with pytest.raises(DecisionResponseError):
        _ = await service.ask("state", "是否？")


async def test_predict把三原语混进同一趟() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=_NOUL_BODY)

    service = _service(handler)

    _ = await service.predict(
        "state",
        {
            "a": noul("是否？"),
            "b": choice("选一个", {"x": None}),
            "c": score("打分", ["低", "高"]),
        },
    )

    body = cast("dict[str, object]", json.loads(seen[0].content))
    assert sorted(cast("dict[str, object]", body["questions"])) == ["a", "b", "c"]
    assert len(seen) == 1  # 三题只发一次请求


async def test_重试会记日志(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(decision_service, "_BACKOFF_BASE", 0.0)
    files = setup_logging(LogSettings(dir=str(tmp_path), level="TRACE"))
    calls: list[int] = []

    def handler(_request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx2.Response(429, json={})
        return httpx2.Response(200, json={"model": "m", "answers": {}, "usage": {}})

    _ = await _service(handler).predict("state", {})

    assert len(calls) == 2
    assert "判断调用重试" in files.app.read_text(encoding="utf-8")


async def test_重试日志的端点是URL而非裸名(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """回归：重试日志的 端点 字段要与成功日志同源（都是 settings.base_url）。

    一头写裸名 "laya"、另一头写 URL，同一字段两种取值，采集端按端点聚合会分叉。
    哪一头由 后端 字段表达，所以这里也断言它记的是裸名。
    """
    monkeypatch.setattr(decision_service, "_BACKOFF_BASE", 0.0)
    files = setup_logging(LogSettings(dir=str(tmp_path), level="TRACE"))
    base_url = DecisionSettings().laya.base_url
    calls: list[int] = []

    def handler(_request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx2.Response(429, json={})
        return httpx2.Response(200, json={"model": "m", "answers": {}, "usage": {}})

    _ = await _service(handler).predict("state", {})

    lines = files.app.read_text(encoding="utf-8").splitlines()
    retry_line = next(line for line in lines if "判断调用重试" in line)
    assert f"端点={base_url}" in retry_line
    assert "后端=laya" in retry_line


async def test_成功日志不记正文(tmp_path: Path) -> None:
    """约定「不记正文」的负例：本次 predict 只记了元数据，state 正文一个字都没进日志。

    用可辨认串而非通用词，避免「碰巧不含」的真空通过：先锚定「判断调用完成」
    确实落在文件里（日志管道在工作），再断言正文串不在全文里。
    """
    files = setup_logging(LogSettings(dir=str(tmp_path), level="TRACE"))
    secret = "绝密内容XYZ"
    service = _service(lambda request: httpx2.Response(200, json=_NOUL_BODY))

    _ = await service.predict(secret, {_QID: noul("是否要求退款？")})

    text = files.app.read_text(encoding="utf-8")
    assert "判断调用完成" in text
    assert secret not in text


# ---- 生命周期与健康检查 ----
#
# 这一组真的走 start() / stop()：客户端的键是端点名字符串，断言键集才等价于
# 「只为启用的端点建了客户端」。除「一个端点是空的」那条以外，都不需要请求。
#
# 状态由 manager 摆：它调 start 前后都写 state（STARTING → RUNNING），服务自己
# 从不写。所以下面要造「已在跑」「已停」这类状态时，状态直接写在 service.state 上，
# 而不是指望 start()/stop() 自己去改。


def _enabled() -> DecisionSettings:
    """造一份只启用 laya 端点的配置（jev 走 DecisionSettings 的默认值，不启用）。"""
    return DecisionSettings(
        laya=DecisionEndpointSettings(
            enabled=True, base_url="http://laya.test/v1", model="multilingual"
        )
    )


async def test_start只为启用的端点建客户端() -> None:
    service = DecisionService(_enabled())

    await service.start()

    assert set(service._clients) == {"laya"}  # pyright: ignore[reportPrivateUsage]
    await service.stop()


async def test_未启用端点不产生警告(tmp_path: Path) -> None:
    """两个端点都关着时 start() 只建空客户端，不报警告。

    纯否定断言有真空通过的风险（日志根本没写盘时也成立），所以先打一条正向锚：
    经本服务的门面写一行 INFO，证明这条日志管道确实会落盘，再断言全文无 warning。
    `start()` 自身不打日志是设计（未启用是用户的显式选择），这条锚因此由测试自己产生。
    """
    files = setup_logging(LogSettings(dir=str(tmp_path), level="TRACE"))
    service = DecisionService(DecisionSettings())

    await service.start()
    service.log.info("正向锚：这条门面的日志落盘即证明日志已生效")

    text = files.app.read_text(encoding="utf-8")
    assert "正向锚" in text  # 日志管道确实在工作
    assert "warning" not in text.lower()


async def test_stop幂等() -> None:
    service = DecisionService(_enabled())
    await service.start()
    client = service._clients["laya"]  # pyright: ignore[reportPrivateUsage]

    await service.stop()
    await service.stop()

    assert client.is_closed


async def test_未启动时stop不报错() -> None:
    await DecisionService(DecisionSettings()).stop()


async def test_健康检查三态() -> None:
    """三态由真容器驱动：未启动不健康、start_all 后健康、stop_all 后「服务未运行」。"""
    settings = Settings(decision=_enabled())
    manager = ServiceManager(settings)
    manager.register(DecisionService)
    service = manager.get(DecisionService)

    assert (await service.health()).healthy is False  # 未启动

    await manager.start_all()
    healthy = await service.health()
    assert healthy.healthy is True
    assert healthy.detail == ""
    assert healthy.extra == {
        "jev模型": "jev-latest",
        "jev端点": "https://api.typesafe.ai/v1",
        "laya模型": "multilingual",
        "laya端点": "http://laya.test/v1",
    }

    await manager.stop_all()
    stopped = await service.health()
    assert stopped.healthy is False
    assert "服务未运行" in stopped.detail


async def test_一个端点都没启用时的详情() -> None:
    """手写 RUNNING 覆盖 health() 的「跑着但一个客户端都没有」分支：

    该分支看的是配置问题而非状态问题，manager 驱动路径下打不到（start_all 后
    只要有一端启用就 healthy），因此这里直接摆状态。
    """
    service = DecisionService(DecisionSettings())
    service.state = ServiceState.RUNNING  # 状态就位，但一个客户端都没有

    status = await service.health()

    assert status.healthy is False
    assert "未启用任何判断端点" in status.detail
    assert status.extra == {
        "jev模型": "jev-latest",
        "jev端点": "https://api.typesafe.ai/v1",
        "laya模型": "",
        "laya端点": "http://127.0.0.1:8000/v1",
    }


async def test_已在跑时start不重建客户端() -> None:
    """状态已是 RUNNING 再调 start：直接早退，不重建、不泄漏旧客户端。"""
    service = DecisionService(_enabled())
    await service.start()
    first = service._clients["laya"]  # pyright: ignore[reportPrivateUsage]

    service.state = ServiceState.RUNNING  # 状态由 manager 摆，早退只看 running
    await service.start()

    assert service._clients["laya"] is first  # pyright: ignore[reportPrivateUsage]
    assert not first.is_closed
    await service.stop()


async def test_stop后再start会重建客户端() -> None:
    """stop 后状态留在 STOPPED，此时再 start 不会早退，又建一套客户端。

    这条路径现实中打得到：`ServiceManager.start_all` 把非 RUNNING（含 STOPPED）的
    服务纳入 pending 再次 start()，因此 `start_all → stop_all → start_all` 会走到这里，
    且行为正确——老客户端已在 stop 里关掉（`first.is_closed`），新客户端重建后可用。
    """
    service = DecisionService(_enabled())
    await service.start()
    first = service._clients["laya"]  # pyright: ignore[reportPrivateUsage]
    await service.stop()

    await service.start()

    rebuilt = service._clients["laya"]  # pyright: ignore[reportPrivateUsage]
    assert rebuilt is not first
    assert first.is_closed
    await service.stop()
