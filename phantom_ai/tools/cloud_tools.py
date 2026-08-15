"""Cloud-sync tools — 'save this specifically to cloud' for the agents."""

from __future__ import annotations

from .base import PermissionLevel, ToolContext, ToolError, ToolResult, ToolSpec


async def _save_to_cloud(ctx: ToolContext, content: str, kind: str = "fact",
                         tags: list[str] | None = None) -> ToolResult:
    if ctx.cloudsync is None:
        raise ToolError("cloud sync unavailable", kind="unavailable")
    try:
        result = await ctx.cloudsync.save_to_cloud(content, kind, tags)
    except (ValueError, RuntimeError) as exc:
        raise ToolError(str(exc), kind="invalid" if isinstance(exc, ValueError)
                        else "unavailable") from exc
    return ToolResult.ok(
        f"☁️ Saved to cloud: {content[:120]}",
        data=result)


async def _cloud_status(ctx: ToolContext) -> ToolResult:
    if ctx.cloudsync is None:
        raise ToolError("cloud sync unavailable", kind="unavailable")
    cfg = await ctx.cloudsync.config()
    if not cfg["url"]:
        return ToolResult.ok("Portable Phantom not configured yet — set the "
                             "Worker URL in Settings → Cloud.",
                             data=cfg)
    return ToolResult.ok(f"Portable Phantom: {cfg['url_masked']}"
                         f"{' (token set)' if cfg['token_configured'] else ''}",
                         data=cfg)


def register_cloud_tools(registry) -> None:
    agents = ("phantom", "coded")
    registry.register(ToolSpec(
        name="save_to_cloud",
        description="Save a fact/reminder/preference SPECIFICALLY to the cloud "
                    "(portable Phantom) so it's available on the go — synced "
                    "with local memory.",
        purpose="Cloud memory", category="cloud",
        parameters={"content": {"type": "string", "required": True},
                    "kind": {"type": "string", "enum": ["fact", "preference",
                                                        "project", "decision"],
                             "default": "fact"},
                    "tags": {"type": "array", "items": {"type": "string"}}},
        handler=_save_to_cloud, permission=PermissionLevel.SAFE_ACTION,
        timeout=20, agents=agents,
    ))
    registry.register(ToolSpec(
        name="cloud_status",
        description="Show whether the portable (cloud) Phantom is configured "
                    "and connected.",
        purpose="Cloud status", category="cloud",
        parameters={}, handler=_cloud_status,
        permission=PermissionLevel.READ_ONLY, timeout=10, agents=agents,
    ))
