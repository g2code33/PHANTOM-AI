"""Briefing + monitoring tools — Jarvis Phase 5.

Agents can pull the pre-built briefing and the live monitor snapshot as real,
structured data (cached for instant output).
"""

from __future__ import annotations

from .base import PermissionLevel, ToolContext, ToolError, ToolResult, ToolSpec


def _briefing(ctx: ToolContext):
    if ctx.briefing is None:
        raise ToolError("briefing engine unavailable", kind="unavailable")
    return ctx.briefing


async def _briefing_get(ctx: ToolContext, force: bool = False) -> ToolResult:
    briefing = await _briefing(ctx).get(force=force)
    lines = [f"Briefing for {briefing['for_user']} ({briefing['generated_at']})"]
    for group in briefing["top_priorities"]:
        lines.append(f"[{group['level'].upper()}]")
        for item in group["items"]:
            lines.append(f"  • {item['title']}" +
                         (f" — {item['detail']}" if item.get("detail") else ""))
    for key, section in briefing["sections"].items():
        lines.append(f"{section['title']}: " +
                     ", ".join(str(i) for i in section["items"][:5]))
    return ToolResult.ok("\n".join(lines), data=briefing)


async def _monitor_snapshot(ctx: ToolContext) -> ToolResult:
    if ctx.monitor is None:
        raise ToolError("monitor unavailable", kind="unavailable")
    snap = await ctx.monitor.snapshot()
    sys_ = snap["system"]
    tasks = snap["tasks"]["counts"]
    signals = snap["signals"]
    lines = [
        f"Monitor @ {snap['captured_at']}",
        f"System: CPU {sys_.get('cpu_percent')}% · MEM {sys_.get('memory_percent')}% · "
        f"DISK {sys_.get('disk_percent')}%",
        f"Tasks: {tasks}",
        f"Failures 24h: {signals['tool_failures_24h']} tool · "
        f"{signals['api_failures_24h']} api · {signals['denials_24h']} denials",
    ]
    if snap["upcoming"]:
        lines.append("Upcoming: " + ", ".join(f"{u['name']} ({u['in_hours']}h)"
                                              for u in snap["upcoming"][:5]))
    return ToolResult.ok("\n".join(lines), data=snap)


def register_briefing_tools(registry) -> None:
    agents = ("phantom", "coded", "evolution", "health")
    registry.register(ToolSpec(
        name="briefing_get",
        description="Get today's prioritized briefing (cached, instant): "
                    "tasks, upcoming schedules, wellness, projects, "
                    "opportunities + spoken summary.",
        purpose="Daily briefing", category="briefing",
        parameters={"force": {"type": "boolean", "default": False}},
        handler=_briefing_get, permission=PermissionLevel.READ_ONLY, timeout=10,
        agents=agents,
    ))
    registry.register(ToolSpec(
        name="monitor_snapshot",
        description="Get the live system monitor: CPU/memory/disk, tasks, "
                    "failures, upcoming schedules, wellness.",
        purpose="Live monitoring", category="briefing",
        parameters={}, handler=_monitor_snapshot,
        permission=PermissionLevel.READ_ONLY, timeout=10, agents=agents,
    ))
