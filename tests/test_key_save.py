"""API-key persistence + key-test regression tests.

Root-cause regression covered here: UI saves were being sent as POST to PUT
endpoints (405 → silent failure). These tests verify the server side of the
fixed flow:

* PUT /api/settings stores the NVIDIA key, verifies it on disk, and the key
  survives a full app restart (refresh / re-launch simulation).
* PUT /api/voice/config stores Deepgram + Groq keys, verified the same way.
* PUT /api/brains/{id}/config stores per-brain keys.
* POST /api/keys/test checks stored keys live against a mock provider and
  returns friendly ok/messages — never the key itself.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from phantom_ai.api.app import App
from phantom_ai.api.server import create_app

VALID_NVIDIA = "nvapi-valid-000000000000000000000000"
VALID_DEEPGRAM = "dg-valid-token-0000000000000000"
VALID_GROQ = "gsk_valid_groq_key_0000000000000000"

# env vars that must NOT be set, so secrets come from the secrets file
_SHADOW_VARS = ("PHANTOM_NVIDIA_API_KEY", "CODED_NVIDIA_API_KEY",
                "EVOLUTION_NVIDIA_API_KEY", "HEALTH_NVIDIA_API_KEY",
                "DEEPGRAM_API_KEY", "GROQ_API_KEY", "PHANTOM_CLOUD_TOKEN",
                "PHANTOM_CLOUD_URL", "PHANTOM_DEFAULT_MODEL",
                "CODED_DEFAULT_MODEL", "EVOLUTION_DEFAULT_MODEL",
                "HEALTH_DEFAULT_MODEL")


def _mock_app() -> FastAPI:
    """Auth-aware mock: NVIDIA /models, Deepgram /projects, Groq /models."""
    app = FastAPI()

    @app.get("/v1/models")
    async def nvidia_models(request: Request):
        if request.headers.get("Authorization") == f"Bearer {VALID_NVIDIA}":
            return {"object": "list", "data": [{"id": "meta/llama-3.3-70b-instruct"}]}
        return JSONResponse(status_code=401, content={"error": {"message": "unauthorized"}})

    @app.get("/projects")
    async def deepgram_projects(request: Request):
        if request.headers.get("Authorization") == f"Token {VALID_DEEPGRAM}":
            return {"projects": [{"project_id": "mock"}]}
        return JSONResponse(status_code=401, content={"error": {"message": "unauthorized"}})

    @app.get("/openai/v1/models")
    async def groq_models(request: Request):
        if request.headers.get("Authorization") == f"Bearer {VALID_GROQ}":
            return {"object": "list", "data": [{"id": "whisper-large-v3-turbo"}]}
        return JSONResponse(status_code=401, content={"error": {"message": "unauthorized"}})

    return app


@pytest.fixture
async def provider_mock():
    """Starts the auth-aware mock; yields the three base URLs."""
    config = uvicorn.Config(_mock_app(), host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.02)
    assert server.started
    port = server.servers[0].sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    old = {v: os.environ.get(v) for v in _SHADOW_VARS}
    for v in _SHADOW_VARS:
        os.environ.pop(v, None)
    os.environ["PHAI_NVIDIA_BASE_URL"] = base + "/v1"
    os.environ["PHAI_DEEPGRAM_BASE_URL"] = base
    os.environ["PHAI_GROQ_BASE_URL"] = base + "/openai/v1"
    yield {"nvidia": base + "/v1", "deepgram": base, "groq": base + "/openai/v1"}
    server.should_exit = True
    try:
        await asyncio.wait_for(task, timeout=10)
    except Exception:  # noqa: BLE001
        task.cancel()
    for v, val in old.items():
        if val is None:
            os.environ.pop(v, None)
        else:
            os.environ[v] = val


@pytest.fixture
def workdir():
    d = tempfile.mkdtemp(prefix=".pcai-keys-", dir=os.path.expanduser("~"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _make_app(workdir: str) -> App:
    return App(db_path=os.path.join(workdir, "test.db"),
               data_dir=os.path.join(workdir, "data"),
               workspace_root=workdir)


async def _client(app: App) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                             base_url="http://test")


# ---------------------------------------------------------------------------
# persistence: save → verify on disk → restart app → still there
# ---------------------------------------------------------------------------


async def test_nvidia_key_persists_across_restart(provider_mock, workdir):
    inst1 = _make_app(workdir)
    await inst1.startup()
    try:
        async with await _client(inst1) as c:
            res = await c.put("/api/settings", json={
                "agent": "phantom", "key": "nvidia_api_key", "value": VALID_NVIDIA})
            assert res.status_code == 200
            data = res.json()
            assert data["persisted"] is True
            assert data["ok"] is True
            assert data["masked"].endswith(VALID_NVIDIA[-4:])
    finally:
        await inst1.shutdown()

    # verify the file actually contains the key (no env shadowing)
    secrets_file = os.path.join(workdir, "data", "secrets.json")
    assert os.path.exists(secrets_file)
    import json
    with open(secrets_file) as f:
        assert json.load(f)["PHANTOM_NVIDIA_API_KEY"] == VALID_NVIDIA
    # chmod 600
    assert (os.stat(secrets_file).st_mode & 0o777) == 0o600

    # "refresh": a brand-new app instance on the same data dir
    inst2 = _make_app(workdir)
    await inst2.startup()
    try:
        async with await _client(inst2) as c:
            res = await c.get("/api/settings")
            assert res.status_code == 200
            key_info = res.json()["keys"]["phantom"]
            assert key_info["configured"] is True
            assert key_info["masked"].endswith(VALID_NVIDIA[-4:])
    finally:
        await inst2.shutdown()


async def test_voice_keys_persist_across_restart(provider_mock, workdir):
    inst1 = _make_app(workdir)
    await inst1.startup()
    try:
        async with await _client(inst1) as c:
            res = await c.put("/api/voice/config", json={"deepgram_api_key": VALID_DEEPGRAM,
                                                         "groq_api_key": VALID_GROQ})
            assert res.status_code == 200
            data = res.json()
            assert data["persisted"] is True
            assert data["deepgram_configured"] is True
            assert data["groq_configured"] is True
            assert data["deepgram_masked"].endswith(VALID_DEEPGRAM[-4:])
            # the raw key must never be echoed back
            raw = json_dumps(res.json())
            assert VALID_DEEPGRAM not in raw
            assert VALID_GROQ not in raw
    finally:
        await inst1.shutdown()

    inst2 = _make_app(workdir)
    await inst2.startup()
    try:
        async with await _client(inst2) as c:
            res = await c.get("/api/voice/config")
            data = res.json()
            assert data["deepgram_configured"] is True
            assert data["groq_configured"] is True
    finally:
        await inst2.shutdown()


async def test_brain_key_persists_across_restart(provider_mock, workdir):
    inst1 = _make_app(workdir)
    await inst1.startup()
    try:
        async with await _client(inst1) as c:
            res = await c.put("/api/brains/research/config", json={"api_key": VALID_NVIDIA})
            assert res.status_code == 200
            assert res.json()["persisted"] is True
    finally:
        await inst1.shutdown()

    inst2 = _make_app(workdir)
    await inst2.startup()
    try:
        async with await _client(inst2) as c:
            res = await c.get("/api/brains/configs")
            brains = {b["brain_id"]: b for b in res.json()["brains"]}
            assert brains["research"]["key_configured"] is True
    finally:
        await inst2.shutdown()


async def test_delete_key_unconfigures(provider_mock, workdir):
    inst = _make_app(workdir)
    await inst.startup()
    try:
        async with await _client(inst) as c:
            await c.put("/api/settings", json={
                "agent": "phantom", "key": "nvidia_api_key", "value": VALID_NVIDIA})
            res = await c.put("/api/settings", json={
                "agent": "phantom", "key": "nvidia_api_key", "delete": True})
            assert res.status_code == 200
            assert res.json()["persisted"] is True
            res2 = await c.get("/api/settings")
            assert res2.json()["keys"]["phantom"]["configured"] is False
    finally:
        await inst.shutdown()


async def test_save_reports_disk_failure(provider_mock, workdir):
    inst = _make_app(workdir)
    await inst.startup()
    try:
        # replace the secrets file with a directory → atomic replace must fail
        # → the endpoint answers honestly instead of pretending success
        secrets_file = os.path.join(workdir, "data", "secrets.json")
        os.makedirs(secrets_file, exist_ok=True)
        async with await _client(inst) as c:
            res = await c.put("/api/settings", json={
                "agent": "phantom", "key": "nvidia_api_key", "value": VALID_NVIDIA})
            assert res.status_code == 200
            assert res.json()["persisted"] is False
    finally:
        await inst.shutdown()


# ---------------------------------------------------------------------------
# POST /api/keys/test — live key check, friendly results, no key material
# ---------------------------------------------------------------------------


async def test_keys_test_nvidia_ok(provider_mock, workdir):
    inst = _make_app(workdir)
    await inst.startup()
    try:
        async with await _client(inst) as c:
            await c.put("/api/settings", json={
                "agent": "phantom", "key": "nvidia_api_key", "value": VALID_NVIDIA})
            res = await c.post("/api/keys/test", json={"kind": "nvidia", "agent": "phantom"})
            assert res.status_code == 200
            data = res.json()
            assert data["ok"] is True
            assert "works" in data["message"]
            assert VALID_NVIDIA not in json_dumps(data)
    finally:
        await inst.shutdown()


async def test_keys_test_invalid_key_friendly(provider_mock, workdir):
    inst = _make_app(workdir)
    await inst.startup()
    try:
        async with await _client(inst) as c:
            await c.put("/api/settings", json={
                "agent": "phantom", "key": "nvidia_api_key", "value": "nvapi-bad-key-9999"})
            res = await c.post("/api/keys/test", json={"kind": "nvidia", "agent": "phantom"})
            assert res.status_code == 200
            data = res.json()
            assert data["ok"] is False
            assert "rejected" in data["message"]
    finally:
        await inst.shutdown()


async def test_keys_test_no_key_friendly(provider_mock, workdir):
    inst = _make_app(workdir)
    await inst.startup()
    try:
        async with await _client(inst) as c:
            res = await c.post("/api/keys/test", json={"kind": "deepgram"})
            assert res.status_code == 400
            assert "No Deepgram key" in res.json()["detail"]
            res2 = await c.post("/api/keys/test", json={"kind": "bogus"})
            assert res2.status_code == 400
    finally:
        await inst.shutdown()


async def test_keys_test_deepgram_and_groq(provider_mock, workdir):
    inst = _make_app(workdir)
    await inst.startup()
    try:
        async with await _client(inst) as c:
            await c.put("/api/voice/config", json={"deepgram_api_key": VALID_DEEPGRAM,
                                                   "groq_api_key": VALID_GROQ})
            res = await c.post("/api/keys/test", json={"kind": "deepgram"})
            assert res.json()["ok"] is True
            res = await c.post("/api/keys/test", json={"kind": "groq"})
            assert res.json()["ok"] is True
            # invalid groq → friendly rejection
            await c.put("/api/voice/config", json={"groq_api_key": "gsk_bad"})
            res = await c.post("/api/keys/test", json={"kind": "groq"})
            assert res.json()["ok"] is False
            assert "rejected" in res.json()["message"]
    finally:
        await inst.shutdown()


def json_dumps(obj) -> str:
    import json
    return json.dumps(obj)


async def test_keys_test_uses_brain_own_key(provider_mock, workdir):
    """A specialist brain's Test button must check ITS OWN stored key
    (brain.<id>.api_key), not fall back to Phantom's key."""
    inst = _make_app(workdir)
    await inst.startup()
    try:
        async with await _client(inst) as c:
            # phantom has an INVALID key; research has a VALID one
            await c.put("/api/settings", json={
                "agent": "phantom", "key": "nvidia_api_key", "value": "nvapi-bad-key-9999"})
            await c.put("/api/brains/research/config", json={"api_key": VALID_NVIDIA})
            # brain's own key works
            r = await c.post("/api/keys/test", json={"kind": "nvidia", "agent": "research"})
            assert r.status_code == 200
            assert r.json()["ok"] is True, r.json()
            # phantom (no own brain key) still fails with its bad key
            r2 = await c.post("/api/keys/test", json={"kind": "nvidia", "agent": "phantom"})
            assert r2.json()["ok"] is False
    finally:
        await inst.shutdown()
