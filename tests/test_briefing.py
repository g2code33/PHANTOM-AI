"""Jarvis Phase 5 — monitoring + daily briefing engine: real signals, cached
instant output, prioritized sections, tools."""

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


async def test_monitor_snapshot_real_signals(app):
    instance, _state, _wd = app
    snap = await instance.monitor.snapshot()
    assert "system" in snap and "cpu_percent" in snap["system"]
    assert "tasks" in snap and "counts" in snap["tasks"]
    assert "signals" in snap and "tool_failures_24h" in snap["signals"]
    assert "upcoming" in snap
    assert "wellness" in snap
    assert "opportunities" in snap
    # cached path returns instantly
    snap2 = await instance.monitor.snapshot(cached=True)
    assert snap2["captured_at"] == snap["captured_at"]


async def test_monitor_reflects_failures(app):
    instance, _state, _wd = app
    await instance.audit.record("phantom", "tool.error",
                                {"tool": "web_search", "kind": "network"})
    await instance.audit.record("phantom", "tool.error",
                                {"tool": "read_file", "kind": "not_found"})
    await instance.audit.record("phantom", "tool.error",
                                {"tool": "http_request", "kind": "timeout"})
    snap = await instance.monitor.snapshot(cached=False)
    assert snap["signals"]["tool_failures_24h"] >= 3


async def test_briefing_builds_and_caches(app):
    instance, _state, _wd = app
    b1 = await instance.briefing.get()
    assert b1["for_user"] == "JOOJO"
    assert "top_priorities" in b1 and "sections" in b1
    assert "spoken" in b1 and "JOOJO" in b1["spoken"]
    assert "generated_at" in b1
    # cached instant
    b2 = await instance.briefing.get()
    assert b2["generated_at"] == b1["generated_at"]
    # force regenerates
    b3 = await instance.briefing.refresh()
    assert b3["generated_at"] != b1["generated_at"]


async def test_briefing_api_instant_and_refresh(app):
    instance, _state, _wd = app
    async with await _client(instance) as client:
        r = await client.get("/api/briefing")
        assert r.status_code == 200
        b = r.json()
        assert b["for_user"] == "JOOJO"
        assert "spoken" in b
        # refresh
        r2 = await client.post("/api/briefing/refresh")
        assert r2.status_code == 200
        assert r2.json()["for_user"] == "JOOJO"


async def test_monitor_api(app):
    instance, _state, _wd = app
    async with await _client(instance) as client:
        m = (await client.get("/api/monitor")).json()
        assert "system" in m and "cpu_percent" in m["system"]
        watch = (await client.get("/api/monitor/watch")).json()
        assert "watchers" in watch and len(watch["watchers"]) >= 4


async def test_briefing_tools_via_registry(tool_ctx):
    ctx = tool_ctx("phantom")
    b = await ctx.registry_get("briefing_get").run(ctx)
    assert "JOOJO" in b.output
    assert "Briefing" in b.output
    m = await ctx.registry_get("monitor_snapshot").run(ctx)
    assert "Monitor" in m.output
    assert "System" in m.output


async def test_briefing_schedule_seeded(app):
    instance, _state, _wd = app
    schedules = await instance.scheduler.store.list("phantom")
    assert any(s["name"] == "Daily briefing" for s in schedules)


async def test_briefing_tools_available_to_agents(app):
    instance, _state, _wd = app
    for agent_id in ("phantom", "coded", "evolution", "health"):
        schemas = {t["function"]["name"] for t in instance.registry.schemas(agent_id)}
        assert "briefing_get" in schemas
        assert "monitor_snapshot" in schemas
