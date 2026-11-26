"""运行台：在工作区内执行白名单命令，捕获输出并限制超时。

安全边界：
- 只认白名单可执行文件，不经 shell（`create_subprocess_exec`），没有管道与重定向；
- 命令名不能带路径，避免用同名可执行文件绕过白名单；
- 参数不能指向工作区外（绝对路径、`..`），git 只允许只读子命令；
- 子进程只看得到 PATH / HOME / 语言环境，控制面令牌等变量不继承；
- 独立进程组：超时或取消时整组 kill，不留孤儿进程；
- cwd 必须是工作区内的目录。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import signal
from collections import deque
from pathlib import Path

from agent.cosmos.contracts import (
    AgentCard,
    AgentContext,
    AgentExample,
    AgentRequest,
    AgentResult,
    DesktopMeta,
    ExecutionPolicy,
)
from core.agent.workspace import (
    WorkspacePathError,
    aliya_workspace_root,
    resolve_in_workspace,
)

#: 允许执行的可执行文件名
ALLOWED_COMMANDS: frozenset[str] = frozenset(
    {"pytest", "ruff", "python", "python3", "uv", "git", "echo", "pwd"}
)
#: git 只允许只读查询；提交、推送、改配置一律不开放
GIT_SUBCOMMANDS: frozenset[str] = frozenset(
    {"status", "log", "diff", "show", "rev-parse", "branch", "describe"}
)
#: 出现即拒绝的参数（casefold 后比较）
DENIED_TOKENS: frozenset[str] = frozenset(
    {
        "-c",
        "-exec",
        "-execdir",
        "-delete",
        "-fprintf",
        "-fprint",
        "--force",
        "--hard",
        "--no-verify",
        "--output",
        "--git-dir",
        "--work-tree",
        "--exec-path",
        "--upload-pack",
        "--receive-pack",
        "push",
        "reset",
        "clean",
        "checkout",
        "rebase",
        "config",
        "install",
        "publish",
        "sudo",
        "chmod",
        "chown",
        "rm",
        "curl",
        "wget",
        "ssh",
        # 会改文件的选项与能绕过白名单的解释器
        "--fix",
        "--unsafe-fixes",
        "format",
        "bash",
        "sh",
        "zsh",
        "env",
        "xargs",
        "sed",
        "awk",
        "perl",
        "node",
    }
)
#: python -m 允许的模块：只放开测试、检查与字节码编译
ALLOWED_MODULES: frozenset[str] = frozenset({"pytest", "ruff", "compileall", "unittest"})
#: 单条流最多读取的字节数：这是读取的物理上限，不是展示截断。
#: 超过后停止累积并显式标注，避免一条刷屏命令把内存与上下文一起撑爆。
MAX_OUTPUT_BYTES = 4_000_000
#: 超时上限（秒）
MAX_TIMEOUT_SECONDS = 300


CARD = AgentCard(
    id="run",
    name="运行台",
    icon="⚙️",
    summary="在工作区里跑测试与命令",
    description=(
        "在 Aliya 的工作区（data/Aliya/workspace）内执行白名单命令并返回输出："
        "pytest、ruff check、python -m 测试模块、uv run 与 git 只读查询。"
        "不接受安装、发布、推送、删除类操作；参数与 cwd 都不能指向工作区外，超时会终止整个进程组。"
        "不提供文件查看（用检索台或 read_file），也不执行任意脚本。"
    ),
    capabilities=["run_tests", "run_command", "lint"],
    params_schema={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的命令，含参数"},
            "cwd": {"type": "string", "description": "相对工作区的目录，默认工作区根"},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300},
        },
        "required": ["command"],
    },
    examples=[
        AgentExample(input="跑一下工作区里的测试"),
        AgentExample(input="对工作区里的文件跑 ruff 检查"),
    ],
    desktop=DesktopMeta(group="dev", order=30),
    execution=ExecutionPolicy(
        timeout_seconds=300, max_concurrent=1, queue=True, retryable=True, heartbeat_seconds=20
    ),
)


async def handle(request: AgentRequest, ctx: AgentContext) -> AgentResult:
    command = _text(request.params.get("command"))
    raw_cwd = _text(request.params.get("cwd")) or "."
    timeout = min(
        max(_int(request.params.get("timeout_seconds"), default=120), 1), MAX_TIMEOUT_SECONDS
    )

    if not command:
        return _failed(request, "缺少 command", "需要给出要执行的命令")
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        return _failed(request, f"命令格式不对：{exc}", "检查引号是否成对")
    problem = validate_command(parts)
    if problem is not None:
        return _failed(request, problem, "运行台只做测试、检查与只读查询")

    try:
        cwd = resolve_in_workspace(raw_cwd, root=aliya_workspace_root())
    except WorkspacePathError as exc:
        return _failed(request, str(exc), "只能在工作区里运行")
    if not cwd.is_dir():
        return _failed(request, f"目录不存在：{raw_cwd}", "先确认工作目录")

    if ctx.cancelled:
        return AgentResult.cancelled(request_id=request.request_id)
    await ctx.report(progress=30, step="执行中")

    try:
        code, stdout, stderr, timed_out, truncated = await _execute(parts, cwd, timeout)
    except FileNotFoundError:
        return _failed(request, f"命令不存在：{parts[0]}", "环境里没装这个命令")
    except OSError as exc:
        return _failed(request, f"启动失败：{exc}", "命令没能启动", retriable=True)

    if timed_out:
        return AgentResult.failed(
            code="timeout",
            message=f"命令超过 {timeout} 秒，已终止",
            retriable=True,
            user_hint="跑得太久了，可以缩小范围再试",
            request_id=request.request_id,
        )

    await ctx.report(progress=100, step="完成")
    body = _render(code, stdout, stderr, truncated)
    if code != 0:
        return AgentResult.failed(
            code="command_failed",
            message=f"命令返回 {code}",
            retriable=False,
            user_hint="命令没跑通，看输出里的报错",
            summary=f"命令失败（退出码 {code}）",
            content=body,
            request_id=request.request_id,
        )
    return AgentResult.ok(
        summary=f"命令完成：{Path(parts[0]).name}",
        content=body,
        request_id=request.request_id,
    )


def validate_command(parts: list[str]) -> str | None:
    """命令名与参数校验；返回 None 表示放行。

    独立成公开函数便于测试直接覆盖策略，不必真的起子进程。
    """
    if not parts:
        return "缺少命令"
    executable = parts[0]
    if "/" in executable or "\\" in executable:
        return "命令不能带路径"
    if executable not in ALLOWED_COMMANDS:
        return f"不允许执行：{executable}（可用：{'、'.join(sorted(ALLOWED_COMMANDS))}）"

    args = parts[1:]
    for arg in args:
        # `--output=/tmp/x` 这类 `键=值` 写法要按键判断，否则精确匹配形同虚设
        key, _, value = arg.partition("=")
        if key.casefold() in DENIED_TOKENS:
            return f"命令包含不允许的参数：{key}"
        # 绝对路径与 .. 都会把读写带到工作区外
        candidate = value or arg
        if candidate.startswith(("/", "~")) or ".." in Path(candidate).parts:
            return f"参数不能指向工作区外：{candidate}"

    if executable == "git":
        subcommand = next((item for item in args if not item.startswith("-")), "")
        if subcommand.casefold() not in GIT_SUBCOMMANDS:
            return "git 只能用只读子命令：" + "、".join(sorted(GIT_SUBCOMMANDS))
    if executable == "ruff":
        subcommand = next((item for item in args if not item.startswith("-")), "")
        if subcommand.casefold() != "check":
            return "ruff 只支持 check"
    if executable in {"python", "python3"}:
        # 直接跑脚本等于任意代码执行；只放开受控模块
        if "-m" not in args:
            return "python 只支持 -m " + " / -m ".join(sorted(ALLOWED_MODULES))
        index = args.index("-m")
        module = args[index + 1] if index + 1 < len(args) else ""
        if module.casefold() not in ALLOWED_MODULES:
            return f"python -m 只支持：{'、'.join(sorted(ALLOWED_MODULES))}"
    if executable == "uv":
        if len(parts) < 3 or parts[1] != "run":
            return "uv 只支持 uv run <命令>"
        inner = validate_command(parts[2:])
        if inner is not None:
            return inner
    return None


async def _execute(
    parts: list[str], cwd: Path, timeout: int
) -> tuple[int, str, str, bool, bool]:
    """启动子进程并等待；超时或取消时整组 kill，保证没有孤儿进程。"""
    env = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "LANG", "LC_ALL")
        if key in os.environ
    }
    process = await asyncio.create_subprocess_exec(
        *parts,
        cwd=str(cwd),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        async with asyncio.timeout(timeout):
            # 两条流必须并发读：只读其中一条时，另一条写满管道会卡住子进程
            (out, out_cut), (err, err_cut) = await asyncio.gather(
                read_output(process.stdout),
                read_output(process.stderr),
            )
            code = await process.wait()
    except TimeoutError:
        await _terminate(process)
        return -1, "", "", True, False
    except asyncio.CancelledError:
        await _terminate(process)
        raise
    return (
        code,
        out.decode("utf-8", errors="replace"),
        err.decode("utf-8", errors="replace"),
        False,
        out_cut or err_cut,
    )


async def read_output(
    stream: asyncio.StreamReader | None, limit: int = MAX_OUTPUT_BYTES
) -> tuple[bytes, bool]:
    """读完整条流，超限时保留头尾。

    错误与结论通常落在输出末尾，只留头部会把最有用的部分丢掉；
    尾部缓冲最多保留两倍上限，避免长输出把内存吃满。
    """
    if stream is None:
        return b"", False
    head_limit = limit * 2 // 3
    tail_limit = limit - head_limit
    head: list[bytes] = []
    head_size = 0
    tail: deque[bytes] = deque()
    tail_size = 0
    total = 0
    while True:
        chunk = await stream.read(65_536)
        if not chunk:
            break
        total += len(chunk)
        if head_size < head_limit:
            take = min(len(chunk), head_limit - head_size)
            head.append(chunk[:take])
            head_size += take
            chunk = chunk[take:]
        if not chunk:
            continue
        tail.append(chunk)
        tail_size += len(chunk)
        while tail_size > tail_limit * 2 and len(tail) > 1:
            tail_size -= len(tail.popleft())

    head_data = b"".join(head)
    tail_data = b"".join(tail)[-tail_limit:] if tail_limit else b""
    if total <= limit:
        return head_data + tail_data, False
    omitted = total - len(head_data) - len(tail_data)
    marker = f"\n…（中间省略 {omitted} 字节）…\n".encode()
    return head_data + marker + tail_data, True


async def _terminate(process: asyncio.subprocess.Process) -> None:
    """先杀整个进程组，再兜底 kill 并回收，避免 pytest 子进程变成孤儿。"""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        process.kill()
    with contextlib.suppress(ProcessLookupError):
        _ = await process.wait()


def _render(code: int, stdout: str, stderr: str, truncated: bool) -> str:
    """按段完整拼接；只有读取物理上限被触发时才标注。"""
    lines = [f"退出码：{code}"]
    for title, text in (("输出", stdout.strip()), ("错误", stderr.strip())):
        if text:
            lines.append(f"{title}：\n{text}")
    if truncated:
        lines.append(f"（输出超过 {MAX_OUTPUT_BYTES} 字节，已保留头尾）")
    return "\n".join(lines)


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _int(value: object, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return default


def _failed(
    request: AgentRequest,
    message: str,
    hint: str,
    *,
    retriable: bool = False,
) -> AgentResult:
    return AgentResult.failed(
        code="invalid_params" if not retriable else "internal",
        message=message,
        retriable=retriable,
        user_hint=hint,
        request_id=request.request_id,
    )
