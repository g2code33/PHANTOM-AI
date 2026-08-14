"""API smoke tests: status, chat, settings/keys masking, permissions, kill
switch integration, offline mode (no keys → honest offline provider)."""

from __future__ import annotations

import asyncio
import os

import httpx

from phantom_ai.api.app import App
from phantom_ai.api.server import create_app


async def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                             base_url="http://test")


async def test_status_and_agents(app):
    instance, _state, _wd = app
    async with await _client(instance) as client:
        res = await client.get("/api/status")
        assert res.status_code == 200
        data = res.json()
        assert len(data["agents"]) == 2
        assert data["killswitch"]["engaged"] is False
        ids = {a["id"] for a in data["agents"]}
        assert ids == {"phantom", "coded"}
        assert data["providers"]["phantom"]["ok"] is True

        res2 = await client.get("/api/agents")
        agents = res2.json()["agents"]
        assert agents[0]["display_name"] == "Phantom"
        assert agents[1]["display_name"] == "Coded"


async def test_chat_over_http(app):
    instance, state, _wd = app

    async def handler(body):
        return {"content": "reply from the api test", "tool_calls": []}

    state.handler = handler
    async with await _client(instance) as client:
        res = await client.post("/api/agents/phantom/chat",
                                json={"text": "hello api"})
        assert res.status_code == 200
        data = res.json()
        assert data["run_id"]
        cid = data["conversation_id"]

        for _ in range(50):
            conv = await client.get(f"/api/conversations/{cid}")
            msgs = conv.json()["messages"]
            if len(msgs) >= 2:
                break
            await asyncio.sleep(0.05)
        assert msgs[-1]["role"] == "assistant"
        assert "reply from the api test" in msgs[-1]["content"]


async def test_settings_key_masking(app):
    instance, _state, _wd = app
    async with await _client(instance) as client:
        res = await client.get("/api/settings")
        keys = res.json()["keys"]
        assert keys["phantom"]["configured"] is True
        assert "nvapi" not in keys["phantom"]["masked"]

        # saving a new key works and rebuilds the provider
        # (env var takes priority, so drop it to prove the file store works)
        os.environ.pop("PHANTOM_NVIDIA_API_KEY", None)
        res = await client.put("/api/settings", json={
            "agent": "phantom", "key": "nvidia_api_key",
            "value": "nvapi-brand-new-secret-key-00000000"})
        assert res.status_code == 200
        assert instance.secrets.get("PHANTOM_NVIDIA_API_KEY") == "nvapi-brand-new-secret-key-00000000"
        assert instance.providers["phantom"].has_key
        os.environ["PHANTOM_NVIDIA_API_KEY"] = "nvapi-test-phantom-key-0000000000"


async def test_permission_override_via_api(app):
    instance, _state, _wd = app
    async with await _client(instance) as client:
        res = await client.put("/api/permissions", json={
            "agent": "phantom", "tool": "write_file", "level": "safe_action"})
        assert res.status_code == 200
        level, _, _ = await instance.permissions.effective_level("phantom", "write_file", {})
        assert level.value == "safe_action"
        perms = await client.get("/api/permissions")
        rows = {r["tool"]: r for r in perms.json()["agents"]["phantom"]}
        assert rows["write_file"]["override"] == "safe_action"


async def test_killswitch_via_api(app):
    instance, _state, _wd = app
    async with await _client(instance) as client:
        res = await client.post("/api/killswitch/engage", json={"reason": "api test"})
        assert res.json()["ok"] is True
        chat = await client.post("/api/agents/phantom/chat", json={"text": "hi"})
        assert chat.status_code == 409
        tool = await client.post("/api/tools/system_info/run", json={"agent": "phantom", "arguments": {}})
        assert tool.status_code == 409
        dis = await client.post("/api/killswitch/disengage")
        assert dis.json()["ok"] is True


async def test_manual_tool_run_endpoint(app, workdir):
    instance, _state, _wd = app
    async with await _client(instance) as client:
        res = await client.post("/api/tools/system_info/run",
                                json={"agent": "phantom", "arguments": {}})
        assert res.status_code == 200
        assert res.json()["success"] is True

        # blocked manual run
        res2 = await client.post("/api/tools/run_command/run",
                                 json={"agent": "phantom",
                                       "arguments": {"command": "rm -rf /"}})
        assert res2.status_code in (403, 422)


async def test_offline_mode_is_honest(tmp_path):
    """Without API keys the app runs in clearly-labeled offline mode."""
    os.environ.pop("PHANTOM_NVIDIA_API_KEY", None)
    os.environ.pop("CODED_NVIDIA_API_KEY", None)
    instance = App(db_path=str(tmp_path / "offline.db"), data_dir=str(tmp_path / "data"))
    await instance.startup()
    try:
        assert instance.providers["phantom"].name == "offline"
        conv = await instance.conversations.create("phantom")
        result = await instance.agents["phantom"].run(conv["id"], "help")
        assert "offline" in result.content.lower()
        assert "PHANTOM_NVIDIA_API_KEY" in result.content
    finally:
        await instance.shutdown()
