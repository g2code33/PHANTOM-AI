"""TIER 13 — voice-first layer: config, ephemeral Deepgram tokens, proactive
speech events, kill-switch voice stop, specialist brain seeding."""

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


async def test_voice_config_defaults(app):
    instance, _state, _wd = app
    async with await _client(instance) as client:
        res = await client.get("/api/voice/config")
        assert res.status_code == 200
        cfg = res.json()
        assert cfg["stt"]["provider"] == "server"
        # JARVIS default: browser/system TTS so speech works with zero config
        assert cfg["tts"]["provider"] == "browser"
        assert cfg["stt_priority"] == ["deepgram", "groq", "local_whisper"]
        assert cfg["tts_priority"] == ["deepgram", "cloud", "local"]
        assert cfg["mode"] in ("private", "push", "conversation")
        assert cfg["proactive_speech"] in (True, False)
        assert cfg["deepgram_configured"] is False


async def test_voice_config_update_and_key_masking(app):
    instance, _state, _wd = app
    async with await _client(instance) as client:
        res = await client.put("/api/voice/config", json={
            "mode": "push",
            "proactive_speech": True,
            "deepgram_api_key": "dg-secret-key-abc123"})
        assert res.status_code == 200
        cfg = res.json()
        assert cfg["mode"] == "push"
        assert cfg["proactive_speech"] is True
        assert cfg["deepgram_configured"] is True
        # key is masked, never returned
        assert "dg-secret-key-abc123" not in res.text
        assert cfg["deepgram_masked"] != ""
        # stored server-side only
        assert instance.secrets.get("DEEPGRAM_API_KEY") == "dg-secret-key-abc123"


async def test_voice_speak_publishes_event(app):
    instance, _state, _wd = app
    subscriber = await instance.events.subscribe("test-voice")
    try:
        async with await _client(instance) as client:
            res = await client.post("/api/voice/speak", json={"text": "Good morning, Alex."})
            assert res.status_code == 200
        event = await asyncio.wait_for(subscriber.get(), timeout=5)
        assert event["event"] == "voice.speak"
        assert event["data"]["text"] == "Good morning, Alex."
    finally:
        await instance.events.unsubscribe("test-voice")


async def test_killswitch_publishes_voice_stop(app):
    instance, _state, _wd = app
    subscriber = await instance.events.subscribe("test-ks")
    try:
        async with await _client(instance) as client:
            await client.post("/api/killswitch/engage", json={"reason": "voice test"})
        events = []
        for _ in range(4):
            try:
                events.append(await asyncio.wait_for(subscriber.get(), timeout=3))
            except asyncio.TimeoutError:
                break
        assert any(e["event"] == "voice.stop" for e in events)
    finally:
        await instance.events.unsubscribe("test-ks")
        await instance.killswitch.disengage()


async def test_deepgram_token_requires_key(app):
    instance, _state, _wd = app
    async with await _client(instance) as client:
        res = await client.post("/api/voice/deepgram-token")
        assert res.status_code == 400  # no key configured


async def test_specialist_brains_are_agents(app):
    instance, _state, _wd = app
    for bid in ("planner", "tutor", "research", "security"):
        assert bid in instance.agents, f"{bid} agent missing"
        assert instance.agents[bid].meta["display_name"]
    # specialists share the platform provider (no separate model required)
    assert instance.agents["research"].provider.name in ("nvidia", "offline")
    # restricted tool allowlists are enforced
    research_tools = instance.agents["research"].tool_allowlist
    assert "web_search" in research_tools
    assert "delete_file" not in research_tools


async def test_status_includes_voice_config(app):
    instance, _state, _wd = app
    async with await _client(instance) as client:
        data = (await client.get("/api/status")).json()
        assert "voice" in data
        assert data["voice"]["stt"]["provider"] in ("browser", "deepgram", "server")


async def test_heartbeat_proactive_speech_off_by_default(app):
    """Without voice.proactive_speech enabled, heartbeat runs must NOT emit
    voice.speak events (quiet by default)."""
    instance, _state, _wd = app
    subscriber = await instance.events.subscribe("test-proactive")
    try:
        await instance.settings.set("voice.proactive_speech", False, "*")
        sched = await instance.scheduler.store.create(
            "phantom", "quiet check", "every 5m", "do nothing",
            quiet_start=None, quiet_end=None)
        from datetime import datetime, timedelta, timezone

        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        await instance.scheduler.store.update(sched["id"], next_run_at=past)
        from phantom_ai.heartbeat.scheduler import HeartbeatScheduler

        scheduler = HeartbeatScheduler(
            store=instance.scheduler.store, settings=instance.settings,
            task_manager=instance.tasks, agent_runner=_async_runner_quiet,
            events=instance.events, killswitch=instance.killswitch,
            notification_store=instance.notifications)
        await scheduler._tick()
        await asyncio.sleep(0.3)
        spoke = False
        try:
            while True:
                e = await asyncio.wait_for(subscriber.get(), timeout=0.3)
                if e["event"] == "voice.speak":
                    spoke = True
        except asyncio.TimeoutError:
            pass
        assert spoke is False, "proactive speech must be off by default"
    finally:
        await instance.events.unsubscribe("test-proactive")


async def test_heartbeat_proactive_speech_when_enabled(app):
    instance, _state, _wd = app
    subscriber = await instance.events.subscribe("test-proactive-on")
    try:
        await instance.settings.set("voice.proactive_speech", True, "*")
        await instance.settings.set("voice.last_proactive_ts", 0, "*")
        sched = await instance.scheduler.store.create(
            "phantom", "loud check", "every 5m", "say something",
            quiet_start=None, quiet_end=None)
        from datetime import datetime, timedelta, timezone

        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        await instance.scheduler.store.update(sched["id"], next_run_at=past)
        from phantom_ai.heartbeat.scheduler import HeartbeatScheduler

        scheduler = HeartbeatScheduler(
            store=instance.scheduler.store, settings=instance.settings,
            task_manager=instance.tasks, agent_runner=_async_runner_speak,
            events=instance.events, killswitch=instance.killswitch,
            notification_store=instance.notifications)
        await scheduler._tick()
        await asyncio.sleep(0.3)
        spoke = False
        try:
            while True:
                e = await asyncio.wait_for(subscriber.get(), timeout=0.5)
                if e["event"] == "voice.speak":
                    spoke = True
        except asyncio.TimeoutError:
            pass
        assert spoke is True, "proactive speech should fire when enabled"
    finally:
        await instance.events.unsubscribe("test-proactive-on")
        await instance.settings.set("voice.proactive_speech", False, "*")


async def _async_runner_quiet(**kw):
    return {"status": "ok", "content": "all quiet"}


async def _async_runner_speak(**kw):
    return {"status": "ok", "content": "your build finished successfully"}
