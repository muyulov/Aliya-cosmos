"""示例子 agent：把收到的文本原样返回。

联调用的最小 agent，**不是**真实业务 agent。它演示了契约的最小用法：
参数自包含、上报一次进度、返回结构化结果，不接触对话上下文与用户通道。
"""

from __future__ import annotations

from agent.cosmos.contracts import (
    AgentCard,
    AgentContext,
    AgentRequest,
    AgentResult,
    DesktopMeta,
    ExecutionPolicy,
)

CARD = AgentCard(
    id="echo",
    name="回声台",
    icon="🔁",
    summary="原样返回收到的指令",
    description="联调用的最小工具：把收到的文本原样返回，用来验证调度、进度与结果链路。",
    capabilities=["echo"],
    params_schema={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
    desktop=DesktopMeta(group="general", order=100),
    execution=ExecutionPolicy(timeout_seconds=30, max_concurrent=1, queue=True),
)


async def handle(request: AgentRequest, ctx: AgentContext) -> AgentResult:
    """回显 `params["text"]`。"""
    await ctx.report(progress=50, step="回显")
    text = str(request.params.get("text", ""))
    if not text:
        return AgentResult.failed(
            code="invalid_params",
            message="缺少 text",
            retriable=False,
            user_hint="需要给一句话才能回显",
        )
    return AgentResult.ok(summary=f"收到：{text}")
