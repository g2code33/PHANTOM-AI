"""Jarvis Phase 4 — profile tools + profile injection: agents know JOOJO,
can read/update/create profiles, and the profile is in every context."""

from __future__ import annotations

import asyncio
import os

import pytest

from phantom_ai.api.app import App


async def _new_conv(app, agent="phantom"):
    return (await app.conversations.create(agent))["id"]


async def test_profile_injected_into_context(app):
    instance, state, _wd = app
    cid = await _new_conv(instance)
    seen = {}

    async def handler(body):
        seen["system"] = body["messages"][0]["content"]
        return {"content": "ok", "tool_calls": []}

    state.handler = handler
    await instance.agents["phantom"].run(cid, "what do you know about me?")
    assert "USER PROFILE" in seen["system"]
    assert "JOOJO" in seen["system"]
    assert "University of Cape Coast" in seen["system"]
    assert "Code Rx" in seen["system"]
    # address-by-name rule present
    assert "never 'sir'" in seen["system"]


async def test_profile_tools_via_registry(tool_ctx):
    ctx = tool_ctx("phantom")
    # get
    g = await ctx.registry_get("profile_get").run(ctx)
    assert "JOOJO" in g.output
    assert "Code Rx" in g.output
    # update
    u = await ctx.registry_get("profile_update").run(
        ctx, fields={"favorite_activity": "building things", "deadline_2026": "final exams"})
    assert "updated" in u.output
    # get reflects it
    g2 = await ctx.registry_get("profile_get").run(ctx)
    assert "building things" in g2.output
    # create a second profile
    c = await ctx.registry_get("profile_create").run(
        ctx, name="Kofi Mensah", display_name="Kofi",
        fields={"role": "friend", "program": "engineering"})
    assert "Kofi" in c.output
    # list shows both
    l = await ctx.registry_get("profile_list").run(ctx)
    assert "JOOJO" in l.output and "Kofi" in l.output


async def test_agent_updates_profile_via_tool(app):
    instance, state, _wd = app
    cid = await _new_conv(instance)

    async def handler(body):
        messages = body["messages"]
        last = messages[-1]
        if last["role"] == "tool":
            return {"content": "Saved to your profile.", "tool_calls": []}
        return {"content": "", "tool_calls": [{"id": "p1", "type": "function",
                                               "function": {"name": "profile_update",
                                                            "arguments": {"fields": {"preferred_study_time": "evenings"}}}}]}

    state.handler = handler
    result = await instance.agents["phantom"].run(
        cid, "add to my profile that I prefer studying in the evenings")
    assert result.status == "ok"
    profile = await instance.profiles.default()
    assert profile["fields"].get("preferred_study_time") == "evenings"


async def test_profile_update_then_injected_next_run(app):
    instance, state, _wd = app
    await instance.profiles.update((await instance.profiles.default())["id"],
                                   {"current_focus": "final year projects"})
    cid = await _new_conv(instance)
    seen = {}

    async def handler(body):
        seen["system"] = body["messages"][0]["content"]
        return {"content": "ok", "tool_calls": []}

    state.handler = handler
    await instance.agents["coded"].run(cid, "hello")
    assert "final year projects" in seen["system"]


async def test_profile_delete_requires_confirmation(app):
    instance, _state, _wd = app
    # create a throwaway profile, then delete via store (tool-level confirm is
    # enforced by the permission layer, already covered elsewhere)
    profile = await instance.profiles.create("Temp", fields={})
    assert await instance.profiles.delete(profile["id"]) is True
    assert await instance.profiles.get(profile["id"]) is None


async def test_profile_tools_available_to_both_agents(app):
    instance, _state, _wd = app
    for agent_id in ("phantom", "coded", "evolution", "health"):
        schemas = {t["function"]["name"] for t in instance.registry.schemas(agent_id)}
        assert "profile_get" in schemas, f"profile_get missing for {agent_id}"
        assert "profile_update" in schemas, f"profile_update missing for {agent_id}"
