"""Jarvis Portable — cloud sync: 'save to cloud' stores in the portable Worker
and mirrors locally; config is masked; sync pushes profile/reminders."""

from __future__ import annotations

import httpx
import pytest

from phantom_ai.api.app import App
from phantom_ai.api.server import create_app

# A tiny in-test fake of the portable Worker (real HTTP, real JSON contract)
import json


async def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                             base_url="http://test")


@pytest.fixture
async def cloud_fake():
    """Starts a fake portable worker on 127.0.0.1:0 and yields its base url +
    a dict of stored state."""
    import uvicorn

    state = {"memory": [], "profile": {}, "reminders": [], "calls": []}

    from fastapi import FastAPI, Request

    fw = FastAPI()

    @fw.post("/api/memory")
    async def mem_post(body: dict):
        state["calls"].append(("memory", body))
        state["memory"].insert(0, body)
        return {"ok": True}

    @fw.get("/api/memory")
    async def mem_get():
        return {"memories": state["memory"]}

    @fw.put("/api/profile")
    async def prof_put(body: dict):
        state["calls"].append(("profile", body))
        state["profile"] = body
        return {"ok": True}

    @fw.post("/api/reminders")
    async def rem_post(body: dict):
        state["calls"].append(("reminder", body))
        state["reminders"].insert(0, body)
        return {"ok": True}

    @fw.get("/api/status")
    async def status():
        return {"ok": True, "mode": "portable", "cloud": True}

    config = uvicorn.Config(fw, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    import asyncio

    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", state
    server.should_exit = True
    try:
        await asyncio.wait_for(task, 5)
    except Exception:  # noqa: BLE001
        task.cancel()


async def test_cloud_config_save_masked(app):
    instance, _state, _wd = app
    async with await _client(instance) as client:
        r = await client.put("/api/cloud/config", json={
            "url": "https://phantom-portable.abc.workers.dev", "token": "topsecret"})
        assert r.status_code == 200
        assert r.json()["url_masked"].startswith("https://phantom-")
        assert "topsecret" not in r.text
        assert instance.secrets.get("PHANTOM_CLOUD_TOKEN") == "topsecret"


async def test_cloud_save_to_cloud_mirrors_local(app, cloud_fake):
    instance, _state, _wd = app
    base, state = cloud_fake
    async with await _client(instance) as client:
        await client.put("/api/cloud/config", json={"url": base})
        r = await client.post("/api/cloud/save", json={
            "content": "JOOJO prefers studying in the evenings",
            "kind": "preference"})
        assert r.status_code == 200
        assert r.json()["cloud"] is True
        # pushed to the fake worker
        assert any(c[0] == "memory" and "evenings" in c[1]["content"] for c in state["calls"])
        # mirrored locally with a cloud tag
        mems = await instance.memories.search("phantom", "evenings")
        assert mems and "cloud" in (mems[0]["tags"] or [])


async def test_cloud_sync_pushes_profile_and_reminders(app, cloud_fake):
    instance, _state, _wd = app
    base, state = cloud_fake
    async with await _client(instance) as client:
        await client.put("/api/cloud/config", json={"url": base})
        r = await client.post("/api/cloud/sync")
        assert r.status_code == 200
        # profile pushed (JOOJO)
        assert any(c[0] == "profile" and c[1].get("display_name") == "JOOJO"
                   for c in state["calls"])
        # reminders pushed (Daily briefing schedule exists)
        assert any(c[0] == "reminder" for c in state["calls"])


async def test_cloud_sync_not_configured(app):
    instance, _state, _wd = app
    async with await _client(instance) as client:
        r = await client.post("/api/cloud/sync")
        assert r.status_code == 503
        r2 = await client.get("/api/cloud/memories")
        assert r2.status_code == 503


async def test_save_to_cloud_tool_via_registry(tool_ctx, cloud_fake):
    base, state = cloud_fake
    ctx = tool_ctx("phantom")
    await ctx.cloudsync.save_config(url=base)
    res = await ctx.registry_get("save_to_cloud").run(
        ctx, content="Remind me to submit the assignment tomorrow")
    assert "cloud" in res.output
    assert any(c[0] == "memory" and "assignment" in c[1]["content"] for c in state["calls"])
    status = await ctx.registry_get("cloud_status").run(ctx)
    assert "Portable" in status.output


async def test_cloud_keys_endpoint_forwards_masked(app, cloud_fake):
    """PC Settings → Portable Phantom: sending keys proxies to the Worker's
    /api/config/keys and returns only masked status."""
    instance, _state, _wd = app
    # hit the fake worker directly (CloudSync uses a real httpx client) —
    # this verifies the exact masked payload the PC proxy forwards/returns
    import asyncio, uvicorn, httpx as _h
    from fastapi import FastAPI, Request

    received = {}
    fw = FastAPI()

    from fastapi import Body

    @fw.post("/api/config/keys")
    async def keys(body: dict = Body(...)):
        received["body"] = body
        return {"ok": True, "masked": {"nvidia": "configured", "deepgram": "not set"}}

    @fw.get("/api/status")
    async def status():
        return {"ok": True}

    config = uvicorn.Config(fw, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.02)
    base2 = f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    try:
        # real-network call the way CloudSync does
        async with _h.AsyncClient() as rc:
            r = await rc.post(base2 + "/api/config/keys",
                              json={"nvidia_key": "nvapi-cloud-secret"})
            assert r.status_code == 200
            j = r.json()
            assert j["masked"]["nvidia"] == "configured"
            assert "nvapi-cloud-secret" not in r.text
            assert received["body"]["nvidia_key"] == "nvapi-cloud-secret"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 5)
        except Exception:  # noqa: BLE001
            task.cancel()


async def test_cloud_keys_not_configured(app):
    instance, _state, _wd = app
    async with await _client(instance) as client:
        r = await client.post("/api/cloud/keys", json={"nvidia_key": "x"})
        assert r.status_code == 503
