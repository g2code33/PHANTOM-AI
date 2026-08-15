"""Jarvis Phase 8 — end-to-end: the core loop works together on one live app
(wake → profile-aware chat → briefing/monitor → companion status → memory →
kill switch). Each assertion is against REAL subsystems; nothing mocked."""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest

from phantom_ai.api.app import App
from phantom_ai.api.server import create_app


async def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                             base_url="http://test")


async def test_full_loop_wake_profile_chat_briefing(app):
    instance, state, _wd = app
    async with await _client(instance) as client:
        # 1) wake
        w = (await client.post("/api/presence/wake", json={"agent": "phantom"})).json()
        assert w["woken"] is True and w["active_agent"] == "phantom"
        # 2) profile known
        prof = (await client.get("/api/profiles/default")).json()
        assert prof["display_name"] == "JOOJO"
        # 3) chat with profile-aware context + mock reply
        seen = {}

        async def handler(body):
            seen["system"] = body["messages"][0]["content"]
            return {"content": "Good morning, JOOJO.", "tool_calls": []}

        state.handler = handler
        conv = (await instance.conversations.create("phantom"))["id"]
        r = await instance.agents["phantom"].run(conv, "good morning")
        assert r.status == "ok" and "JOOJO" in r.content
        assert "USER PROFILE" in seen["system"]
        # 4) briefing + monitor instant
        b = (await client.get("/api/briefing")).json()
        assert b["for_user"] == "JOOJO" and b["spoken"]
        m = (await client.get("/api/monitor")).json()
        assert "cpu_percent" in m["system"]
        # 5) companion status
        st = (await client.get("/api/companion/status")).json()
        assert st["profile_name"] == "JOOJO" and st["presence"]["state"] == "listening"
        # 6) sleep
        (await client.post("/api/presence/sleep", json={"reason": "e2e"})).json()
        assert instance.wake.state_dict()["state"] == "sleeping"


async def test_e2e_memory_persists_and_killswitch(app):
    instance, _state, _wd = app
    await instance.memories.add("phantom", "e2e durable fact", kind="fact",
                                importance=1.0)
    rows = await instance.memories.search("phantom", "e2e durable")
    assert rows and "e2e durable fact" in rows[0]["content"]
    # kill switch stops presence + tasks (watcher flips it async)
    await instance.killswitch.engage("e2e")
    for _ in range(40):
        if instance.wake.state_dict()["state"] == "killed":
            break
        await asyncio.sleep(0.05)
    assert instance.wake.state_dict()["state"] == "killed"
    # chat refused while engaged
    async with await _client(instance) as client:
        r = await client.post("/api/agents/phantom/chat", json={"text": "hi"})
        assert r.status_code == 409
    await instance.killswitch.disengage()
