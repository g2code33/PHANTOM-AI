"""Notifications: in-app records + OS-level notification when available."""

from __future__ import annotations

import os
import shutil
import subprocess

from .base import PermissionLevel, ToolContext, ToolResult, ToolSpec


async def _send_notification(ctx: ToolContext, title: str, body: str = "") -> ToolResult:
    row = await ctx.notifications.create(ctx.agent_id, title, body)
    if ctx.events:
        await ctx.events.publish("notification.new", {"notification": row, "agent": ctx.agent_id})
    os_notified = False
    if os.name == "posix" and shutil.which("notify-send"):
        try:
            subprocess.run(["notify-send", "-a", "PHANTOM+CODED", title, body[:200]],
                           capture_output=True, timeout=5)
            os_notified = True
        except (subprocess.SubprocessError, OSError):
            pass
    return ToolResult.ok(
        f"Notification sent: {title}" + (f" — {body[:200]}" if body else "") +
        (" (desktop notification)" if os_notified else " (in-app)"),
        data={"notification": row, "desktop": os_notified})


def register_notify_tools(registry) -> None:
    registry.register(ToolSpec(
        name="send_notification",
        description="Send the user a notification (in-app always; OS notification when supported).",
        purpose="Notify the user", category="system",
        parameters={"title": {"type": "string", "required": True, "maxLength": 200},
                    "body": {"type": "string", "default": "", "maxLength": 2000}},
        handler=_send_notification, permission=PermissionLevel.CONFIRM_REQUIRED, timeout=10,
        required_permission_notes="Interrupting the user warrants confirmation.",
    ))
