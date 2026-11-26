"""Aliya 的工具面：16 个工具，主动回合给 12 个可自行安排的。

用闭包把 runtime 绑进工具，避免 `self` 被当成工具参数。

描述写法约定：第一行说「做什么」，随后一句说「什么时候用 / 什么时候别用」，
最后一句说「返回什么」。模型选工具与填参数只看这段文字，写得含糊就会误调。
"""

from __future__ import annotations

import ast
import asyncio
from datetime import datetime
from typing import Protocol

from agent.aliya.memory import MemoryKind, MemoryScope
from agent.aliya.turn.context import format_moment
from core.agent import ToolRegistry, tool
from core.agent.workspace import (
    WorkspacePathError,
    aliya_workspace_root,
    read_text,
    resolve_in_workspace,
)

#: 一次最多回读的行数与单行长度，避免把整份文件塞进上下文
MAX_READ_LINES = 200
#: 计算表达式长度与指数上限，挡住 `9**9**9` 这类把回合卡死的输入
MAX_EXPRESSION_CHARS = 200
MAX_EXPONENT = 100

class _CalcError(ValueError):
    """表达式不受支持，或结果没有意义。"""


class RuntimePort(Protocol):
    async def send(self, text: str) -> None: ...

    def now(self) -> datetime: ...

    async def describe_desktop(self) -> str: ...

    async def dispatch(
        self,
        task_text: str,
        *,
        agent_id: str | None = None,
        params: dict[str, object] | None = None,
        hint: str | None = None,
    ) -> str: ...

    async def task_status(self, task_id: str | None = None) -> str: ...

    async def cancel(self, task_id: str) -> str: ...

    async def retry(self, task_id: str) -> str: ...

    async def refresh_world(self) -> str: ...

    async def remember(self, note: str, kind: MemoryKind) -> str: ...

    async def check_memory(self, scope: MemoryScope | None = None) -> str: ...

    async def resolve_topic(self, topic: str) -> str: ...

    async def plan_followup(self, text: str, after_minutes: int) -> str: ...

    async def cancel_followup(self, text: str) -> str: ...

    async def defer(self, minutes: int, reason: str = "") -> str: ...


def build_tools(runtime: RuntimePort, *, proactive: bool = False) -> ToolRegistry:
    @tool
    async def send_message(text: str) -> str:
        """给用户发一条消息，这是用户唯一能看见你的途径。

        想说话、想回应、想补充说明时用；一次调用只发一段，不要把连续的话拆成多次调用。
        返回发送结果；发不出去时返回失败原因。

        Args:
            text: 要发送的正文。
        """
        await runtime.send(text)
        return "已发送"

    @tool
    async def see_desktop() -> str:
        """查看平板上现在有哪些工具、能否使用、各自在做什么。

        在决定派活或需要指定工具之前先看；只读快照，不会触发任何操作。
        用户随口打招呼、闲聊时不要为了找话说去看它，更不要把结果念出来。
        返回每个工具的 id、名字、状态与能力，派活时把 id 填进 agent_id。
        """
        return await runtime.describe_desktop()

    @tool
    async def dispatch_task(
        task_text: str,
        agent_id: str | None = None,
        params: dict[str, object] | None = None,
        hint: str | None = None,
    ) -> str:
        """把一件需要动手完成的事交给平板上的工具，会真正开始执行。

        只用于查资料、改文件、运行程序这类任务；单纯回话请用 send_message，不要派活。
        不确定该用哪个工具时先调 see_desktop。返回任务 id 与标签，后续查进度或取消都用这个 id。

        Args:
            task_text: 用自然语言描述要做的事，写清目标与验收标准。
            agent_id: 指定工具 id，取自 see_desktop 的结果；不填则由调度器自己挑。
            params: 结构化参数，字段由目标工具决定；不填则由调度器从 task_text 解析。
            hint: 额外约束，例如时间、范围、格式偏好。
        """
        return await runtime.dispatch(
            task_text,
            agent_id=agent_id,
            params=params,
            hint=hint,
        )

    @tool
    async def task_status(task_id: str | None = None) -> str:
        """查看任务进度，返回任务 id、标签、状态与完成百分比。

        想知道派出去的活做到哪了就用它；返回的 id 可直接传给 cancel_task / retry_task。

        Args:
            task_id: 指定任务；不填则返回最近的任务。
        """
        return await runtime.task_status(task_id)

    @tool
    async def cancel_task(task_id: str) -> str:
        """取消一个还在排队或运行中的任务。

        只有确定这件事不该继续做时才用；已经结束的任务不受影响。
        返回是否取消成功；任务不存在或已结束时返回原因。

        Args:
            task_id: 要取消的任务 id，来自 dispatch_task 或 task_status 的结果。
        """
        return await runtime.cancel(task_id)

    @tool
    async def retry_task(task_id: str) -> str:
        """重新执行一个失败或部分完成的任务，会生成新的任务 id。

        只在任务失败且仍想继续时用；成功或运行中的任务不能重试。
        返回新任务 id 与标签，后续用新 id 查进度。

        Args:
            task_id: 要重试的任务 id，来自 dispatch_task 或 task_status 的结果。
        """
        return await runtime.retry(task_id)

    @tool
    async def refresh_world() -> str:
        """重新感知一次当前情境：时间、用户状态、环境有什么变化。

        当上下文里的情境可能过期、或你要判断现在是否适合开口时用；只刷新感知，不改变任何东西。
        返回最新的情境摘要。
        """
        return await runtime.refresh_world()

    @tool
    async def current_time() -> str:
        """看现在是几点、星期几。

        想知道「现在」而不想凭感觉猜时用；只读，不会改变任何东西。
        返回服务端校准过的时间，格式是「年-月-日 时:分（星期）」。
        """
        return format_moment(runtime.now())

    @tool
    async def read_file(path: str, start_line: int = 1, max_lines: int = 80) -> str:
        """读你工作区里某个文件的一小段，带行号。

        想确认文件里到底写了什么时用；只能读 data/Aliya/workspace 内的文本文件，敏感路径会被拒绝。
        返回带行号的片段与总行数；读不到时返回原因。
        """
        try:
            target = resolve_in_workspace(path, root=aliya_workspace_root())
        except WorkspacePathError as exc:
            return str(exc)
        if not target.is_file():
            return f"没有这个文件：{path}"
        try:
            text, _ = await asyncio.to_thread(read_text, target)
        except OSError as exc:
            return f"读不了：{exc}"
        lines = text.splitlines()
        begin = max(1, start_line)
        end = min(len(lines), begin + max(1, min(max_lines, MAX_READ_LINES)) - 1)
        if begin > len(lines):
            return f"{path} 一共 {len(lines)} 行，没有第 {begin} 行"
        body = "\n".join(f"{number:>4}: {lines[number - 1]}" for number in range(begin, end + 1))
        return f"{path}（第 {begin}-{end} 行 / 共 {len(lines)} 行）\n{body}"

    @tool
    def calculate(expression: str) -> str:
        """算一个算术表达式，比如折扣后多少钱、两个数差多少。

        需要精确计算而不是心算时用；只支持加减乘除、整除、取余、乘方与括号，不做单位换算。
        返回计算结果；表达式看不懂或除零时返回原因。
        """
        try:
            value = _evaluate(expression)
        except _CalcError as exc:
            return f"算不了：{exc}"
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)

    @tool
    async def remember(note: str, kind: MemoryKind) -> str:
        """把一件事写进长期记忆，之后的回合仍然能读到。

        只在信息值得长期保留时用；临时状态、客套话不要记。
        kind 取 anchor（锚点）、open_topic（未收尾）、unspoken（未说出口）、fact（事实）。
        返回是否记住；内容疑似敏感或类型不认识时会拒绝。

        Args:
            note: 要记住的内容，写成一句完整的话，不要只写关键词。
            kind: 记忆类型，取上述四种之一。
        """
        return await runtime.remember(note, kind)

    @tool
    async def check_memory(scope: MemoryScope | None = None) -> str:
        """查看自己的长期记忆，确认自己记得什么。

        想回顾锚点、未收尾话题或待跟进安排时用；只读，不会改动记忆。
        返回各分类的内容，空分类不显示。

        Args:
            scope: 只看某一类；不填则返回所有非空分类。
        """
        return await runtime.check_memory(scope)

    @tool
    async def resolve_topic(topic: str) -> str:
        """把一条已经聊完的未收尾话题标记为收尾。

        用户明确说这件事过去了、或你判断已经聊完时用；收尾后主动判断不再把它当成未完成。
        返回是否找到并收尾。

        Args:
            topic: 话题原文或其中一段，取自 check_memory 的「未收尾」。
        """
        return await runtime.resolve_topic(topic)

    @tool
    async def plan_followup(text: str, after_minutes: int) -> str:
        """把一件事排进未来，到时间后主动唤醒自己去处理。

        用户提到之后要发生的事（面试、复查、约定），或你想过一阵再问时用。
        到点后会叫醒你，但不会替你执行任务；要不要动手届时再决定。
        返回约定的提醒时间；即使没人说话，你也会被叫醒。

        Args:
            text: 到时要做什么，写成一句完整的话。
            after_minutes: 多少分钟后跟进，1 到 10080（7 天）之间。
        """
        return await runtime.plan_followup(text, after_minutes)

    @tool
    async def cancel_followup(text: str) -> str:
        """取消一条已经排好的未来跟进。

        用户说不用提醒了、或这件事已经不需要再做时用；只影响待跟进，不动长期记忆。
        返回是否找到并取消。

        Args:
            text: 要取消的跟进内容或其中一段，取自 check_memory 的「待跟进」。
        """
        return await runtime.cancel_followup(text)

    @tool
    async def defer_proactive(minutes: int, reason: str = "") -> str:
        """把下一次主动开口往后推，这次先不说话。

        现在不适合打扰、但过一会儿可能合适时用；主动回合调用后本回合保持静默。
        返回新的检查时间。

        Args:
            minutes: 多少分钟后再考虑，1 到 1440（24 小时）之间。
            reason: 为什么现在不说，只写给自己看。
        """
        return await runtime.defer(minutes, reason)

    registry = ToolRegistry()
    if proactive:
        registry.register(
            send_message,
            see_desktop,
            task_status,
            refresh_world,
            current_time,
            calculate,
            check_memory,
            resolve_topic,
            plan_followup,
            cancel_followup,
            defer_proactive,
            remember,
        )
    else:
        registry.register(
            send_message,
            see_desktop,
            dispatch_task,
            task_status,
            cancel_task,
            retry_task,
            refresh_world,
            current_time,
            read_file,
            calculate,
            check_memory,
            resolve_topic,
            plan_followup,
            cancel_followup,
            defer_proactive,
            remember,
        )
    return registry


def _evaluate(expression: str) -> float | int:
    """按 AST 白名单求值：只认数字、四则、整除、取余、乘方与一元正负。

    不用 `eval`：表达式来自模型，任何属性访问、下标、函数调用都不该有机会执行。
    """
    text = expression.strip()
    if not text:
        raise _CalcError("表达式是空的")
    if len(text) > MAX_EXPRESSION_CHARS:
        raise _CalcError(f"表达式太长（上限 {MAX_EXPRESSION_CHARS} 字符）")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise _CalcError(f"看不懂这个表达式：{exc.msg}") from exc
    try:
        return _eval_node(tree.body)
    except ZeroDivisionError as exc:
        raise _CalcError("不能除以零") from exc
    except OverflowError as exc:
        raise _CalcError("结果太大，算不出来") from exc


def _eval_node(node: ast.expr) -> float | int:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, int | float):
            raise _CalcError("只支持数字")
        return node.value
    if isinstance(node, ast.UnaryOp):
        operand = _eval_node(node.operand)
        if isinstance(node.op, ast.UAdd):
            return operand
        if isinstance(node.op, ast.USub):
            return -operand
        raise _CalcError("只支持正负号")
    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > MAX_EXPONENT:
            raise _CalcError(f"指数太大（上限 {MAX_EXPONENT}）")
        result: object
        if isinstance(node.op, ast.Add):
            result = left + right
        elif isinstance(node.op, ast.Sub):
            result = left - right
        elif isinstance(node.op, ast.Mult):
            result = left * right
        elif isinstance(node.op, ast.Div):
            result = left / right
        elif isinstance(node.op, ast.FloorDiv):
            result = left // right
        elif isinstance(node.op, ast.Mod):
            result = left % right
        elif isinstance(node.op, ast.Pow):
            result = left**right
        else:
            raise _CalcError("只支持加减乘除、整除、取余与乘方")
        if isinstance(result, complex):
            raise _CalcError("结果不是实数")
        if not isinstance(result, int | float):
            raise _CalcError("结果不是数字")
        if abs(result) > 1e100:
            raise _CalcError("结果太大，算不出来")
        return result
    raise _CalcError("只支持数字与运算符号")
