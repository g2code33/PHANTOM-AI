"""Process & application control (real OS-level operations)."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from typing import Any

import psutil

from .base import PermissionLevel, ToolContext, ToolError, ToolResult, ToolSpec

MAX_PROCESS_LIST = 200


def _process_row(p: psutil.Process) -> dict[str, Any]:
    try:
        return {
            "pid": p.pid,
            "name": p.name(),
            "cmdline": " ".join(p.cmdline())[:200] or "",
            "cpu": p.cpu_percent(interval=None),
            "memory_mb": round(p.memory_info().rss / (1024 ** 2), 1),
            "status": p.status(),
            "started": p.create_time(),
            "user": p.username(),
        }
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return {"pid": p.pid, "name": "?", "status": "gone"}


async def _list_processes(ctx: ToolContext, filter: str = "", limit: int = 50) -> ToolResult:
    rows = []
    for p in psutil.process_iter(["pid", "name"]):
        try:
            row = _process_row(p)
        except Exception:  # noqa: BLE001
            continue
        if filter and filter.lower() not in row["name"].lower() and filter.lower() not in row["cmdline"].lower():
            continue
        rows.append(row)
        if len(rows) >= min(limit, MAX_PROCESS_LIST):
            break
    rows.sort(key=lambda r: r.get("memory_mb") or 0, reverse=True)
    text = "\n".join(
        f"{r['pid']:7} {r.get('memory_mb', 0):>9}MB {r.get('name', '?'):24} {r.get('cmdline', '')}"
        for r in rows
    ) or "(no processes)"
    return ToolResult.ok(f"{len(rows)} process(es):\n{text}", data={"processes": rows})


async def _process_info(ctx: ToolContext, pid: int) -> ToolResult:
    try:
        p = psutil.Process(pid)
        row = _process_row(p)
        children = [c.pid for c in p.children()]
        row["children"] = children
    except psutil.NoSuchProcess:
        raise ToolError(f"no process with pid {pid}", kind="not_found") from None
    except psutil.AccessDenied:
        raise ToolError(f"permission denied inspecting pid {pid}", kind="permission") from None
    return ToolResult.ok(
        f"pid: {row['pid']}\nname: {row['name']}\nstatus: {row['status']}\n"
        f"memory: {row['memory_mb']} MB\ncpu: {row['cpu']}%\ncmdline: {row['cmdline']}\n"
        f"children: {row['children']}",
        data=row)


async def _open_application(ctx: ToolContext, name: str = "", command: str = "",
                            arguments: list[str] | None = None) -> ToolResult:
    """Launch an application by name (xdg-open/start) or an explicit command."""
    if not name and not command:
        raise ToolError("provide either `name` (an application) or `command`", kind="invalid")
    if not _display_available() and not _headless_safe(command):
        raise ToolError(
            "no graphical display available in this environment — GUI applications cannot be "
            "launched here (this works on a real desktop)",
            kind="unavailable")
    args = list(arguments or [])
    try:
        if command:
            proc = await asyncio.create_subprocess_exec(
                command, *args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
        else:
            if os.name == "nt":
                proc = await asyncio.create_subprocess_exec(
                    "cmd", "/c", "start", "", name, start_new_session=True)
            else:
                opener = "xdg-open"
                if shutil_which(opener) is None:
                    opener = "gio"
                if shutil_which(opener) is None:
                    raise ToolError("no desktop opener (xdg-open/gio) available", kind="unavailable")
                proc = await asyncio.create_subprocess_exec(
                    opener, name, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True)
        await asyncio.wait_for(proc.wait(), timeout=5)
        return ToolResult.ok(
            f"Launched application: {name or command} (exit {proc.returncode})",
            data={"application": name or command, "command": command, "pid": proc.pid})
    except FileNotFoundError:
        raise ToolError(f"application not found: {name or command}", kind="not_found") from None
    except asyncio.TimeoutError:
        return ToolResult.ok(f"Application launch signal sent for {name or command}",
                             data={"application": name or command})


def shutil_which(name: str):
    import shutil

    return shutil.which(name)


def _display_available() -> bool:
    if os.name == "nt":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _headless_safe(command: str) -> bool:
    """Allow launching non-GUI commands even headless (e.g. servers, CLIs)."""
    gui_markers = ("firefox", "chrome", "browser", "code", "gedit", "nautilus", "xdg-open")
    base = os.path.basename(command)
    return not any(m in base for m in gui_markers)


async def _close_process(ctx: ToolContext, pid: int | None = None, name: str | None = None,
                         force: bool = False) -> ToolResult:
    if pid is None and not name:
        raise ToolError("provide pid or name", kind="invalid")
    targets: list[psutil.Process] = []
    if pid is not None:
        try:
            targets.append(psutil.Process(pid))
        except psutil.NoSuchProcess:
            raise ToolError(f"no process with pid {pid}", kind="not_found") from None
    else:
        for p in psutil.process_iter(["name"]):
            try:
                if p.name().lower() == name.lower():
                    targets.append(p)
            except psutil.NoSuchProcess:
                continue
    if not targets:
        raise ToolError(f"no matching process for {name or pid}", kind="not_found")
    killed = []
    for t in targets:
        try:
            if force:
                t.kill()
            else:
                t.terminate()
            killed.append(t.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
            raise ToolError(f"could not stop pid {t.pid}: {exc}", kind="error") from exc
    return ToolResult.ok(f"Stopped process(es): {killed}", data={"pids": killed})


async def _wait_process(ctx: ToolContext, pid: int, timeout: int = 30) -> ToolResult:
    try:
        p = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return ToolResult.ok(f"Process {pid} already exited", data={"pid": pid, "exited": True})
    try:
        p.wait(timeout=timeout)
        return ToolResult.ok(f"Process {pid} exited", data={"pid": pid, "exited": True})
    except psutil.TimeoutExpired:
        return ToolResult.ok(f"Process {pid} still running after {timeout}s",
                             data={"pid": pid, "exited": False})
    except psutil.NoSuchProcess:
        return ToolResult.ok(f"Process {pid} exited", data={"pid": pid, "exited": True})


def register_process_tools(registry) -> None:
    registry.register(ToolSpec(
        name="list_processes", description="List running processes (optionally filtered by name).",
        purpose="Inspect running applications", category="processes",
        parameters={"filter": {"type": "string", "default": "", "description": "Substring to filter by"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_PROCESS_LIST, "default": 50}},
        handler=_list_processes, permission=PermissionLevel.READ_ONLY, timeout=15,
    ))
    registry.register(ToolSpec(
        name="process_info", description="Inspect a single process by pid.",
        purpose="Inspect a running application", category="processes",
        parameters={"pid": {"type": "integer", "required": True}},
        handler=_process_info, permission=PermissionLevel.READ_ONLY, timeout=10,
    ))
    registry.register(ToolSpec(
        name="open_application",
        description="Launch an application by name (e.g. 'firefox', 'code') or an explicit command.",
        purpose="Launch applications", category="processes",
        parameters={"name": {"type": "string", "description": "Application name"},
                    "command": {"type": "string", "description": "Explicit executable path/command"},
                    "arguments": {"type": "array", "items": {"type": "string"}}},
        handler=_open_application, permission=PermissionLevel.SAFE_ACTION, timeout=30,
    ))
    registry.register(ToolSpec(
        name="close_process", description="Stop a running process by pid or name.",
        purpose="Close applications", category="processes",
        parameters={"pid": {"type": "integer"}, "name": {"type": "string"},
                    "force": {"type": "boolean", "default": False}},
        handler=_close_process, permission=PermissionLevel.CONFIRM_REQUIRED, timeout=20,
        required_permission_notes="Stopping processes can lose unsaved work; confirmation required.",
    ))
    registry.register(ToolSpec(
        name="wait_process", description="Wait for a process to exit (bounded by timeout).",
        purpose="Synchronize with applications", category="processes",
        parameters={"pid": {"type": "integer", "required": True}, "timeout": {"type": "integer", "default": 30}},
        handler=_wait_process, permission=PermissionLevel.SAFE_ACTION, timeout=40,
    ))
