"""Profile tools — the user's editable identity that Phantom & Coded know and
maintain. JOOJO can say "add this to my profile" and the agent updates it for
real; new profiles (other people) can be created too.

Honesty: the profile only contains what the user (or an agent, on the user's
instruction) puts in it — never invented.
"""

from __future__ import annotations

from typing import Any

from .base import PermissionLevel, ToolContext, ToolError, ToolResult, ToolSpec


def _profiles(ctx: ToolContext):
    if ctx.profiles is None:
        raise ToolError("profile store unavailable", kind="unavailable")
    return ctx.profiles


async def _profile_get(ctx: ToolContext, profile_id: str = "") -> ToolResult:
    store = _profiles(ctx)
    if profile_id:
        profile = await store.get(profile_id)
        if not profile:
            raise ToolError(f"no profile with id {profile_id}", kind="not_found")
    else:
        profile = await store.default()
        if not profile:
            raise ToolError("no profile yet", kind="not_found")
    text = store.compact(profile)
    return ToolResult.ok(f"User profile:\n{text}",
                         data={"profile": profile})


async def _profile_update(ctx: ToolContext, fields: dict[str, Any],
                          profile_id: str = "") -> ToolResult:
    store = _profiles(ctx)
    if not fields:
        raise ToolError("fields required", kind="invalid")
    if profile_id:
        profile = await store.update(profile_id, fields)
        if not profile:
            raise ToolError(f"no profile with id {profile_id}", kind="not_found")
    else:
        profile = await store.default()
        if not profile:
            profile = await store.create("User", fields=fields)
        else:
            profile = await store.update(profile["id"], fields)
    await ctx.audit.record(ctx.agent_id, "profile.updated",
                           {"fields": list(fields)[:10], "profile": profile["id"]})
    added = ", ".join(list(fields)[:8])
    return ToolResult.ok(f"Profile updated — added/changed: {added}.",
                         data={"profile": profile})


async def _profile_create(ctx: ToolContext, name: str,
                          display_name: str = "",
                          fields: dict[str, Any] | None = None) -> ToolResult:
    store = _profiles(ctx)
    profile = await store.create(name, display_name, fields or {})
    await ctx.audit.record(ctx.agent_id, "profile.created",
                           {"profile": profile["id"], "name": name})
    return ToolResult.ok(f"Created profile for {name} ({profile['id'][:8]}).",
                         data={"profile": profile})


async def _profile_list(ctx: ToolContext) -> ToolResult:
    store = _profiles(ctx)
    profiles = await store.list()
    if not profiles:
        return ToolResult.ok("No profiles yet.", data={"profiles": []})
    lines = [f"{p['display_name'] or p['name']} ({p['id'][:8]})"
             for p in profiles]
    return ToolResult.ok("\n".join(lines), data={"profiles": profiles})


async def _profile_delete(ctx: ToolContext, profile_id: str) -> ToolResult:
    store = _profiles(ctx)
    ok = await store.delete(profile_id)
    if not ok:
        raise ToolError(f"no profile with id {profile_id}", kind="not_found")
    await ctx.audit.record(ctx.agent_id, "profile.deleted",
                           {"profile": profile_id})
    return ToolResult.ok(f"Deleted profile {profile_id[:8]}.",
                         data={"deleted": profile_id})


def register_profile_tools(registry) -> None:
    agents = ("phantom", "coded", "evolution", "health")
    registry.register(ToolSpec(
        name="profile_get",
        description="Read the user's profile sheet (name, school, projects, "
                    "preferences, goals). Call with NO profile_id to get the "
                    "current user's profile — do NOT invent an id.",
        purpose="Know the user", category="profile",
        parameters={"profile_id": {"type": "string", "default": ""}},
        handler=_profile_get, permission=PermissionLevel.READ_ONLY, timeout=10,
        agents=agents,
    ))
    registry.register(ToolSpec(
        name="profile_update",
        description="Add or update fields on the user's profile (e.g. when they "
                    "tell you something new about themselves). Keys like "
                    "'favorite_activity', 'deadline_2026', 'preferred_study_time'.",
        purpose="Maintain the user profile", category="profile",
        parameters={"fields": {"type": "object", "minProperties": 1},
                    "profile_id": {"type": "string", "default": ""}},
        handler=_profile_update, permission=PermissionLevel.SAFE_ACTION, timeout=10,
        agents=agents,
    ))
    registry.register(ToolSpec(
        name="profile_create",
        description="Create a new profile (e.g. for another person the user "
                    "wants you to know separately).",
        purpose="Multi-profile support", category="profile",
        parameters={"name": {"type": "string", "required": True},
                    "display_name": {"type": "string", "default": ""},
                    "fields": {"type": "object", "default": {}}},
        handler=_profile_create, permission=PermissionLevel.SAFE_ACTION, timeout=10,
        agents=agents,
    ))
    registry.register(ToolSpec(
        name="profile_list",
        description="List all saved profiles.",
        purpose="Multi-profile support", category="profile",
        parameters={}, handler=_profile_list,
        permission=PermissionLevel.READ_ONLY, timeout=10, agents=agents,
    ))
    registry.register(ToolSpec(
        name="profile_delete",
        description="Delete a profile (permanent).",
        purpose="Manage profiles", category="profile",
        parameters={"profile_id": {"type": "string", "required": True}},
        handler=_profile_delete, permission=PermissionLevel.CONFIRM_REQUIRED,
        timeout=10, agents=agents,
        required_permission_notes="Deleting a profile is permanent; confirmation required.",
    ))
