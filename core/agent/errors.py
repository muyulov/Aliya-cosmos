"""Agent 层错误树。

约定：
- 全部继承 AgentError，调用方一次 `except AgentError` 兜住整层。
- 只抛「调用方写错了」或「部署配置错了」这一类编程/配置错误；工具执行期的失败
  （参数校验不过、handler 抛异常）会被转成回填文本，不在这里上报。
"""

from __future__ import annotations


class AgentError(Exception):
    """Agent 层错误基类。"""


class ToolDefinitionError(AgentError):
    """工具定义非法：缺 docstring、参数缺注解、`*args` / `**kwargs`、重名注册。"""


class ToolNotFoundError(AgentError):
    """执行了未注册的工具名。"""

    def __init__(self, name: str) -> None:
        super().__init__(f"未注册的工具：{name}")
        #: 出错的工具名，便于调用方对账
        self.name: str = name


class PromptError(AgentError):
    """system 提示的所有分段都为空。"""


class SkillError(AgentError):
    """skill 定义非法：frontmatter 缺失或非法、缺字段、重名、文件读不动。"""


class SkillNotFoundError(SkillError):
    """按名取 skill 时没找到。

    由 SkillLibrary 抛出、注册表回填成文本；模型看到后能改成正确的名字。
    """

    def __init__(self, name: str) -> None:
        super().__init__(f"未找到 skill：{name}")
        #: 出错的 skill 名
        self.name: str = name


class MCPConfigError(AgentError):
    """`data/mcp.json` 读不动、JSON 非法、结构或字段不合法、占位符缺值。"""


class MCPConnectionError(AgentError):
    """连接 MCP server 失败：建连、initialize 或 list_tools 抛错。"""


class MCPTimeoutError(MCPConnectionError):
    """连接 MCP server 超时。

    MCPConnectionError 的子类：调用方一次 except 就能兜住「连不上」与「连太慢」。
    """
