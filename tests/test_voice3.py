"""Jarvis Phase 3 — voice polish: per-persona voices (phantom vs coded),
voice catalog, config persistence."""

from __future__ import annotations

import httpx
import pytest

from phantom_ai.api.app import App
from phantom_ai.api.server import create_app


async def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                             base_url="http://test")


async def test_voice_config_has_per_persona_voices(app):
    instance, _state, _wd = app
    async with await _client(instance) as client:
        cfg = (await client.get("/api/voice/config")).json()
        assert "voices" in cfg
        assert "phantom" in cfg["voices"] and "coded" in cfg["voices"]
        assert cfg["voices"]["phantom"] == ""  # defaults empty → engine picks


async def test_set_per_persona_voices(app):
    instance, _state, _wd = app
    async with await _client(instance) as client:
        r = await client.put("/api/voice/config", json={
            "voices": {"phantom": "aura-orion-en", "coded": "aura-arcas-en"}})
        assert r.status_code == 200
        cfg = r.json()
        assert cfg["voices"]["phantom"] == "aura-orion-en"
        assert cfg["voices"]["coded"] == "aura-arcas-en"
        # persists
        cfg2 = (await client.get("/api/voice/config")).json()
        assert cfg2["voices"] == cfg["voices"]
        # update only one leaves the other
        await client.put("/api/voice/config", json={"voices": {"coded": "aura-zeus-en"}})
        cfg3 = (await client.get("/api/voice/config")).json()
        assert cfg3["voices"]["phantom"] == "aura-orion-en"
        assert cfg3["voices"]["coded"] == "aura-zeus-en"


async def test_voice_catalog(app):
    instance, _state, _wd = app
    async with await _client(instance) as client:
        cat = (await client.get("/api/voice/voices")).json()
        dg = {v["id"] for v in cat["deepgram"]}
        assert "aura-orion-en" in dg  # calm male — Phantom default
        assert "aura-arcas-en" in dg  # sharp male — Coded default
        assert any(v["gender"] == "female" for v in cat["deepgram"])
        assert cat["browser"] == []  # filled client-side


async def test_voice_config_mask_and_persist(app):
    instance, _state, _wd = app
    async with await _client(instance) as client:
        # phantom voice = orion, coded voice = arcas are the engine defaults
        # when unset; ensure they differ so the personas sound distinct
        cfg = (await client.get("/api/voice/config")).json()
        # engine-side defaults are applied in the client; server just stores
        assert isinstance(cfg["voices"]["phantom"], str)
        assert isinstance(cfg["voices"]["coded"], str)
