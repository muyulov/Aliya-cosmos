"""Agent 工具层对外出口。

用法：
    from core.agent import MCPManager, SkillLibrary, ToolRegistry, system_prompt, tool

    registry = ToolRegistry()
    registry.register(my_tool, *library.tools())

    async with MCPManager.from_file() as mcp:
        registry.register(*mcp.tools)
        reply = await llm.chat_tools(messages, registry.declarations())

本层只提供工具声明、执行回填、Skill 索引与 MCP 接入；ReAct 循环、会话历史由调用方写。
"""

from core.agent.errors import (
    AgentError,
    MCPConfigError,
    MCPConnectionError,
    MCPTimeoutError,
    PromptError,
    SkillError,
    SkillNotFoundError,
    ToolDefinitionError,
    ToolNotFoundError,
)
from core.agent.mcp import MCP_CONFIG_FILE, MCPConfig, MCPManager, MCPServerSettings
from core.agent.prompt import system_prompt
from core.agent.registry import ToolRegistry
from core.agent.skill import SKILLS_ROOT, Skill, SkillLibrary
from core.agent.tool import Tool, tool

__all__ = [
    "MCP_CONFIG_FILE",
    "SKILLS_ROOT",
    "AgentError",
    "MCPConfig",
    "MCPConfigError",
    "MCPConnectionError",
    "MCPManager",
    "MCPServerSettings",
    "MCPTimeoutError",
    "PromptError",
    "Skill",
    "SkillError",
    "SkillLibrary",
    "SkillNotFoundError",
    "Tool",
    "ToolDefinitionError",
    "ToolNotFoundError",
    "ToolRegistry",
    "system_prompt",
    "tool",
]
