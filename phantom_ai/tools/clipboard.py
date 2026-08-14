"""Clipboard tools (best-effort real implementations)."""

from __future__ import annotations

import os
import shutil
import subprocess

from .base import PermissionLevel, ToolContext, ToolError, ToolResult, ToolSpec


def _copy_cli() -> str | None:
    for tool in ("xclip", "xsel", "wl-copy"):
        if shutil.which(tool):
            return tool
    return None


async def _clipboard_get(ctx: ToolContext) -> ToolResult:
    if os.name == "nt":
        raise ToolError("clipboard on Windows needs pyperclip (pip install pyperclip)", kind="unavailable")
    cli = _copy_cli()
    if not cli:
        raise ToolError("no clipboard tool available (install xclip or xsel)", kind="unavailable")
    args = {"xclip": ["-o"], "xsel": ["-b"], "wl-copy": ["--paste"]}[cli]
    try:
        out = subprocess.run([cli, *args], capture_output=True, text=True, timeout=5)
        text = out.stdout or ""
    except (subprocess.SubprocessError, OSError) as exc:
        raise ToolError(f"clipboard read failed: {exc}", kind="unavailable") from None
    return ToolResult.ok(f"Clipboard ({len(text)} chars):\n{text[:2000]}", data={"text": text[:10000]})


async def _clipboard_set(ctx: ToolContext, text: str) -> ToolResult:
    if len(text) > 100_000:
        raise ToolError("text too large for clipboard", kind="invalid")
    if os.name == "nt":
        raise ToolError("clipboard on Windows needs pyperclip", kind="unavailable")
    cli = _copy_cli()
    if not cli:
        raise ToolError("no clipboard tool available (install xclip or xsel)", kind="unavailable")
    try:
        proc = subprocess.Popen([cli], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        proc.communicate(text.encode("utf-8"), timeout=5)
    except (subprocess.SubprocessError, OSError) as exc:
        raise ToolError(f"clipboard write failed: {exc}", kind="unavailable") from None
    return ToolResult.ok(f"Clipboard set ({len(text)} chars)", data={"chars": len(text)})


def register_clipboard_tools(registry) -> None:
    registry.register(ToolSpec(
        name="clipboard_get", description="Read the current clipboard text.",
        purpose="Clipboard", category="system",
        parameters={}, handler=_clipboard_get, permission=PermissionLevel.READ_ONLY, timeout=10,
    ))
    registry.register(ToolSpec(
        name="clipboard_set", description="Set the clipboard text.",
        purpose="Clipboard", category="system",
        parameters={"text": {"type": "string", "required": True, "maxLength": 100000}},
        handler=_clipboard_set, permission=PermissionLevel.SAFE_ACTION, timeout=10,
    ))
