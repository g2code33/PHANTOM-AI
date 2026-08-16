"""TIER 14 (Jarvis Phase 1) — wake engine / presence state machine, per-agent
wake routing, stay-silent, idle timeout, kill switch, ready cue, profiles."""

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


async def test_presence_starts_sleeping(app):
    instance, _state, _wd = app
    p = instance.wake.state_dict()
    assert p["state"] == "sleeping"
    assert p["active_agent"] == ""


async def test_wake_phantom_routes_to_phantom(app):
    instance, _state, _wd = app
    sub = await instance.events.subscribe("t-wake")
    try:
        p = await instance.wake.wake("phantom", source="wakeword")
        assert p["state"] == "listening"
        assert p["active_agent"] == "phantom"
        assert p["ready_cue_sent_at"]
        got = []
        while True:
            try:
                got.append(await asyncio.wait_for(sub.get(), timeout=1))
            except asyncio.TimeoutError:
                break
        events = [e["event"] for e in got]
        assert "wake.detected" in events
        assert "ready_cue" in events
        assert "presence.state" in events
        ready = next(e for e in got if e["event"] == "ready_cue")
        assert ready["data"]["agent"] == "phantom"
    finally:
        await instance.events.unsubscribe("t-wake")


async def test_wake_coded_while_phantom_awake(app):
    instance, _state, _wd = app
    await instance.wake.wake("phantom", source="wakeword")
    p = await instance.wake.wake("coded", source="wakeword")
    assert p["active_agent"] == "coded"  # Coded addressed separately, anytime
    assert p["state"] == "listening"


async def test_stay_silent_until_wake(app):
    instance, _state, _wd = app
    await instance.wake.wake("phantom")
    p = await instance.wake.stay_silent()
    assert p["state"] == "silenced"
    # wake word reactivates
    p2 = await instance.wake.wake("phantom", source="wakeword")
    assert p2["state"] == "listening"
    assert p2["silenced_until"] == ""


async def test_idle_timeout_returns_to_sleep(app):
    instance, _state, _wd = app
    await instance.wake.set_idle_minutes(1)  # tiny window for test
    await instance.wake.wake("phantom")
    # fast-forward: call the internal loop directly with a 0-minute window
    instance.wake._state.idle_minutes = 0
    instance.wake._state.last_activity = 0  # force stale
    await instance.wake._idle_loop()
    p = instance.wake.state_dict()
    assert p["state"] == "sleeping"


async def test_killswitch_sets_presence_killed(app):
    instance, _state, _wd = app
    await instance.wake.wake("phantom")
    await instance.killswitch.engage("test")
    await asyncio.sleep(1.2)  # allow watcher to react
    p = instance.wake.state_dict()
    assert p["state"] == "killed"
    assert p["kill_engaged"] is True
    await instance.killswitch.disengage()


async def test_presence_api_wake_sleep(app):
    instance, _state, _wd = app
    async with await _client(instance) as client:
        r = await client.post("/api/presence/wake", json={"agent": "coded", "source": "test"})
        assert r.status_code == 200
        assert r.json()["active_agent"] == "coded"
        r2 = await client.post("/api/presence/sleep", json={"reason": "test"})
        assert r2.json()["state"] == "sleeping"
        # invalid agent rejected
        r3 = await client.post("/api/presence/wake", json={"agent": "evil"})
        assert r3.status_code == 400


async def test_profile_seeded_joojo(app):
    instance, _state, _wd = app
    profile = await instance.profiles.default()
    assert profile is not None
    fields = profile["fields"]
    assert fields["display_name"] == "JOOJO"
    assert "Blessing Jojo Ewusi" in fields["name"]
    assert "University of Cape Coast" in fields["university"]
    assert "Code Rx" in str(fields["organizations"])


async def test_profile_api_create_update_delete(app):
    instance, _state, _wd = app
    async with await _client(instance) as client:
        p = (await client.post("/api/profiles", json={
            "name": "Test Person", "display_name": "Tester",
            "fields": {"role": "student"}})).json()
        assert p["id"]
        upd = (await client.put(f"/api/profiles/{p['id']}", json={
            "fields": {"role": "developer"}})).json()
        assert upd["fields"]["role"] == "developer"
        ok = (await client.delete(f"/api/profiles/{p['id']}")).json()
        assert ok["ok"] is True


async def test_presence_config_endpoints(app):
    instance, _state, _wd = app
    async with await _client(instance) as client:
        cfg = (await client.get("/api/presence/config")).json()
        assert cfg["idle_minutes"] == 60
        assert cfg["words"] == {"phantom": "phantom", "coded": "coded"}
        r = (await client.put("/api/presence/config", json={"idle_minutes": 45})).json()
        assert r["idle_minutes"] == 45


async def test_presence_survives_restart_sleeping(app, workdir):
    instance, _state, _wd = app
    await instance.wake.wake("phantom")
    await instance.shutdown()
    app2 = App(db_path=os.path.join(workdir, "test.db"),
               data_dir=os.path.join(workdir, "data"), workspace_root=workdir)
    await app2.startup()
    try:
        # presence always starts sleeping on restart (no fake awake state)
        assert app2.wake.state_dict()["state"] == "sleeping"
        # profile persisted
        profile = await app2.profiles.default()
        assert profile and "JOOJO" in profile["fields"]["display_name"]
    finally:
        await app2.shutdown()

def test_find_resemblyzer_weights_meipass(tmp_path, monkeypatch):
    """Frozen-app path: weights found via _MEIPASS bundle dir even when the
    installed resemblyzer package has no pretrained.pt next to it."""
    from phantom_ai.voice import speaker as sp

    bundled = tmp_path / "resemblyzer" / "pretrained.pt"
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b"FAKEWEIGHTS")

    monkeypatch.setattr(sp, "sys", type("S", (), {"_MEIPASS": str(tmp_path)})())
    monkeypatch.delenv("RESEMBLYZER_WEIGHTS", raising=False)
    # resemblyzer isn't installed in the test env → pkg_dir branch is empty,
    # so the bundled _MEIPASS path must win
    assert sp.find_resemblyzer_weights() == str(bundled)


def test_find_resemblyzer_weights_env_wins(tmp_path, monkeypatch):
    """Explicit RESEMBLYZER_WEIGHTS env beats everything."""
    from phantom_ai.voice import speaker as sp

    env_weights = tmp_path / "custom.pt"
    env_weights.write_bytes(b"X")
    monkeypatch.setenv("RESEMBLYZER_WEIGHTS", str(env_weights))
    assert sp.find_resemblyzer_weights() == str(env_weights)


def test_find_resemblyzer_weights_none_honest(tmp_path, monkeypatch):
    """No weights anywhere → None (caller reports 'engine unavailable')."""
    from phantom_ai.voice import speaker as sp

    monkeypatch.delenv("RESEMBLYZER_WEIGHTS", raising=False)
    monkeypatch.setattr(sp, "sys", type("S", (), {"_MEIPASS": str(tmp_path / "empty_meipass")})())
    assert sp.find_resemblyzer_weights() is None
