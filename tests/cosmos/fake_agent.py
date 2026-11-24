"""测试用子 agent 模块：验证配置驱动注册能真正导入并绑定 handler。"""

from __future__ import annotations

from agent.cosmos.contracts import AgentContext, AgentRequest, AgentResult


async def handle(request: AgentRequest, _ctx: AgentContext) -> AgentResult:
    return AgentResult.ok(summary=f"收到：{request.params.get('text', '')}")


def sync_handle(_request: AgentRequest, _ctx: AgentContext) -> AgentResult:
    """同步 handler：注册期必须被拒绝。"""
    return AgentResult.ok(summary="sync")
