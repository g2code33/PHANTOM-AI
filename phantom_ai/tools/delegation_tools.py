"""Agent-to-agent delegation tools.

- delegate_to_coded   (available to Phantom) — hands a task to Coded.
- delegate_to_phantom (available to Coded) — hands a task to Phantom.

Delegations carry a task ID, objective, context, permissions, timeout, result
and status, and are subject to loop prevention and concurrency limits.
"""

from __future__ import annotations

from .base import PermissionLevel, ToolContext, ToolError, ToolResult, ToolSpec


async def _delegate(ctx: ToolContext, objective: str, context: str = "",
                    timeout_sec: int = 300, permissions: str = "") -> ToolResult:
    target = "coded" if ctx.agent_id == "phantom" else "phantom"
    perms_override = None
    if permissions.strip():
        try:
            perms_override = {p.strip().lower(): None for p in permissions.split(",") if p.strip()}
        except Exception:  # noqa: BLE001
            perms_override = None
    result = await ctx.delegation_manager.delegate(
        origin=ctx.agent_id,
        target=target,
        objective=objective,
        context=context,
        timeout_sec=max(10, min(timeout_sec, 1800)),
        permissions_override=perms_override,
        origin_conversation_id=ctx.conversation_id,
        origin_session_id=ctx.session_id,
    )
    if result["status"] != "completed":
        raise ToolError(
            f"delegation to {target} did not complete (status={result['status']}): "
            f"{result.get('error') or result.get('result', {}).get('summary', '')}",
            kind="error",
        )
    return ToolResult.ok(
        f"Delegation to {target} completed.\n\n{result['result'].get('summary', '(no summary)')}",
        data={"delegation": result},
    )


def register_delegation_tools(registry) -> None:
    registry.register(ToolSpec(
        name="delegate_to_coded",
        description="Delegate a technical task to Coded (the technical specialist agent) and wait for "
                    "its structured result. Use for programming, debugging, terminal, git, servers, databases.",
        purpose="Agent-to-agent delegation", category="delegation",
        parameters={"objective": {"type": "string", "required": True, "description": "Clear task for Coded"},
                    "context": {"type": "string", "default": "", "description": "Background/context to hand over"},
                    "timeout_sec": {"type": "integer", "minimum": 10, "maximum": 1800, "default": 300},
                    "permissions": {"type": "string", "default": "",
                                    "description": "Comma-separated tool names to allow (optional)"}},
        handler=_delegate, permission=PermissionLevel.SAFE_ACTION, timeout=1810, parallel_safe=False,
        agents=("phantom",),
    ))
    registry.register(ToolSpec(
        name="delegate_to_phantom",
        description="Delegate a research/planning/general task to Phantom and wait for its structured result.",
        purpose="Agent-to-agent delegation", category="delegation",
        parameters={"objective": {"type": "string", "required": True},
                    "context": {"type": "string", "default": ""},
                    "timeout_sec": {"type": "integer", "minimum": 10, "maximum": 1800, "default": 300}},
        handler=_delegate, permission=PermissionLevel.SAFE_ACTION, timeout=1810, parallel_safe=False,
        agents=("coded",),
    ))
