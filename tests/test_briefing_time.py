"""Jarvis Phase 6 — changeable briefing time: JOOJO can move the 7:30 briefing
anytime; all briefing schedules stay in sync; validation."""

from __future__ import annotations

import httpx
import pytest

from phantom_ai.api.app import App
from phantom_ai.api.server import create_app


async def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                             base_url="http://test")


async def test_briefing_time_defaults_0730(app):
    instance, _state, _wd = app
    cfg = await instance.settings.get("briefing.time", "*", "07:30")
    assert cfg == "07:30"
    schedules = await instance.scheduler.store.list()
    dbs = [s for s in schedules if s["name"] == "Daily briefing"]
    assert dbs and dbs[0]["expression"] == "daily at 07:30"


async def test_change_briefing_time_updates_schedules(app):
    instance, _state, _wd = app
    result = await instance.set_briefing_time("06:15")
    assert result["time"] == "06:15"
    assert result["expression"] == "daily at 06:15"
    assert set(result["updated_schedules"]) == {"Daily briefing", "Daily health briefing"}
    # both schedules updated
    for sched in await instance.scheduler.store.list():
        if sched["name"] in ("Daily briefing", "Daily health briefing"):
            assert sched["expression"] == "daily at 06:15"
            assert sched["next_run_at"]  # re-scheduled
    # persisted
    assert await instance.settings.get("briefing.time", "*") == "06:15"


async def test_change_briefing_time_via_api(app):
    instance, _state, _wd = app
    async with await _client(instance) as client:
        r = await client.put("/api/briefing/config", json={"time": "08:45"})
        assert r.status_code == 200
        assert r.json()["time"] == "08:45"
        cfg = (await client.get("/api/briefing/config")).json()
        assert cfg["time"] == "08:45"
        assert all(s["expression"] == "daily at 08:45" for s in cfg["schedules"])


async def test_invalid_briefing_time_rejected(app):
    instance, _state, _wd = app
    async with await _client(instance) as client:
        for bad in ("25:00", "07", "abc", "12:99", "-1:00"):
            r = await client.put("/api/briefing/config", json={"time": bad})
            assert r.status_code == 400, f"{bad} should be rejected"
    # unchanged
    assert await instance.settings.get("briefing.time", "*", "07:30") == "07:30"


async def test_restart_keeps_changed_time(app, workdir):
    instance, _state, _wd = app
    await instance.set_briefing_time("09:00")
    await instance.shutdown()

    app2 = App(db_path=workdir + "/test.db",
               data_dir=workdir + "/data", workspace_root=workdir)
    await app2.startup()
    try:
        assert await app2.settings.get("briefing.time", "*", "07:30") == "09:00"
        dbs = [s for s in await app2.scheduler.store.list()
               if s["name"] == "Daily briefing"]
        assert dbs and dbs[0]["expression"] == "daily at 09:00"
    finally:
        await app2.shutdown()
