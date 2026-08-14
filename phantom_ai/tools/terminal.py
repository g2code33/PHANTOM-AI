"""Terminal access: real subprocess execution with stdout/stderr/exit code/
duration capture, cancellation, timeouts and permission-based command policy."""

from __future__ import annotations

import asyncio
import os
import shlex
import time
from typing import Optional

from ..permissions.validation import PermissionLevel, validate_command
from .base import ToolContext, ToolError, ToolResult, ToolSpec

MAX_OUTPUT_BYTES = 256 * 1024
MAX_COMMAND_LEN = 8000


async def _execute(command: str, cwd: str | None, timeout: float, ctx: ToolContext,
                   shell: bool = True) -> dict:
    if len(command) > MAX_COMMAND_LEN:
        raise ToolError(f"command too long ({len(command)} chars)", kind="invalid")
    if "\n" in command and not shell:
        raise ToolError("newlines not allowed in single commands (use run_script)", kind="invalid")

    verdict = validate_command(command, cwd)
    # The permission manager re-checks with agent overrides at a higher level;
    # here we enforce the hard floor (blocked stays blocked).
    if verdict.level == PermissionLevel.BLOCKED:
        raise ToolError(f"command blocked by policy: {verdict.reason}", kind="permission")

    if shell:
        shell_cmd = _shell_argv(command)
    else:
        try:
            shell_cmd = shlex.split(command)
        except ValueError:
            raise ToolError("cannot parse command arguments", kind="invalid") from None

    env = _safe_env()
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *shell_cmd,
        cwd=cwd or None,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        duration = time.monotonic() - started
        return {
            "stdout": stdout_b[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"),
            "stderr": stderr_b[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"),
            "exit_code": proc.returncode,
            "duration_ms": round(duration * 1000, 1),
            "pid": proc.pid,
        }
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ToolError(
            f"command timed out after {timeout:g}s and was killed",
            kind="timeout", retryable=False,
            data={"stdout": _peek(proc), "command": command[:200]},
        ) from None
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise ToolError("command cancelled", kind="cancelled") from None


def _peek(proc: asyncio.subprocess.Process) -> str:
    return "(output lost on timeout)"


def _shell_argv(command: str) -> list[str]:
    if os.name == "nt":
        return ["powershell", "-NoProfile", "-Command", command]
    return ["/bin/bash", "-c", command]


def _safe_env() -> dict[str, str]:
    """Environment without credentials (defense in depth against leakage)."""
    env = dict(os.environ)
    for k in list(env):
        kl = k.lower()
        if any(s in kl for s in ("api_key", "apikey", "token", "secret", "password", "passwd")):
            env.pop(k, None)
    return env


async def _run_command(ctx: ToolContext, command: str, cwd: str | None = None,
                       timeout: float = 60) -> ToolResult:
    from .base import resolve_safe_path

    real_cwd = None
    if cwd:
        real_cwd = resolve_safe_path(cwd, must_exist=True)
        if not os.path.isdir(real_cwd):
            raise ToolError(f"not a directory: {cwd}", kind="invalid")
    result = await _execute(command, real_cwd, timeout, ctx)
    text = f"$ {command}\n"
    if result["stdout"]:
        text += result["stdout"]
    if result["stderr"]:
        text += f"\n[stderr]\n{result['stderr']}"
    text += f"\n[exit {result['exit_code']} in {result['duration_ms']} ms]"
    if result["exit_code"] != 0:
        return ToolResult.fail(text, data=result)
    return ToolResult.ok(text, data=result)


async def _run_script(ctx: ToolContext, script: str, language: str = "bash",
                      cwd: str | None = None, timeout: float = 120) -> ToolResult:
    if len(script) > 64 * 1024:
        raise ToolError("script too large (max 64 KB)", kind="invalid")
    from .base import resolve_safe_path

    real_cwd = resolve_safe_path(cwd, must_exist=True) if cwd else None
    if language not in ("bash", "python", "python3", "sh"):
        raise ToolError(f"unsupported script language: {language}", kind="invalid")
    interpreter = {"bash": "/bin/bash", "sh": "/bin/sh", "python": "python3", "python3": "python3"}[language]
    verdict = validate_command(script, real_cwd)
    if verdict.level == PermissionLevel.BLOCKED:
        raise ToolError(f"script blocked by policy: {verdict.reason}", kind="permission")

    proc = await asyncio.create_subprocess_exec(
        interpreter, "-c", script,
        cwd=real_cwd or None, env=_safe_env(),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    started = time.monotonic()
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        duration = (time.monotonic() - started) * 1000
        out = stdout_b[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
        err = stderr_b[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
        text = f"[script ({language}) exit {proc.returncode} in {duration:.0f} ms]\n{out}"
        if err:
            text += f"\n[stderr]\n{err}"
        data = {"stdout": out, "stderr": err, "exit_code": proc.returncode,
                "duration_ms": round(duration, 1)}
        if proc.returncode != 0:
            return ToolResult.fail(text, data=data)
        return ToolResult.ok(text, data=data)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ToolError(f"script timed out after {timeout:g}s and was killed", kind="timeout") from None


def register_terminal_tools(registry) -> None:
    registry.register(ToolSpec(
        name="run_command",
        description="Run a shell command on this computer (bash on Linux/macOS, PowerShell on Windows). "
                    "Captures stdout, stderr, exit code and duration. Mutating/system commands require "
                    "user confirmation; dangerous commands are blocked.",
        purpose="Execute terminal commands", category="terminal",
        parameters={"command": {"type": "string", "required": True, "maxLength": MAX_COMMAND_LEN},
                    "cwd": {"type": "string", "description": "Working directory"},
                    "timeout": {"type": "number", "minimum": 1, "maximum": 600, "default": 60}},
        handler=_run_command, permission=PermissionLevel.SAFE_ACTION, timeout=620, parallel_safe=False,
        required_permission_notes="Policy-classified: read-only → auto, mutating → confirm, dangerous → blocked.",
    ))
    registry.register(ToolSpec(
        name="run_script",
        description="Run a multi-line script (bash or python) on this computer with full capture.",
        purpose="Execute scripts", category="terminal",
        parameters={"script": {"type": "string", "required": True, "maxLength": 65536},
                    "language": {"type": "string", "enum": ["bash", "sh", "python"], "default": "bash"},
                    "cwd": {"type": "string"},
                    "timeout": {"type": "number", "minimum": 1, "maximum": 600, "default": 120}},
        handler=_run_script, permission=PermissionLevel.SAFE_ACTION, timeout=620, parallel_safe=False,
    ))
