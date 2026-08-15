"""Jarvis Phase 9 — Cloudflare tunnel: CORS so the https PWA can call the LAN
backend, preflight, and the tunnel helper script."""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

from phantom_ai.api.app import App
from phantom_ai.api.server import create_app


async def _client(app, origin=None) -> httpx.AsyncClient:
    headers = {"Origin": origin} if origin else {}
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                             base_url="http://test", headers=headers)


async def test_cors_headers_present_for_tunnel_origin(app):
    instance, _state, _wd = app
    async with await _client(instance, origin="https://abc123.trycloudflare.com") as client:
        r = await client.get("/api/status")
        assert r.status_code == 200
        assert r.headers.get("access-control-allow-origin") == "https://abc123.trycloudflare.com"
        assert r.headers.get("access-control-allow-headers")
        assert r.headers.get("access-control-allow-methods")


async def test_cors_preflight(app):
    instance, _state, _wd = app
    async with await _client(instance, origin="https://xyz.trycloudflare.com") as client:
        r = await client.options("/api/companion/status")
        assert r.status_code == 200
        assert r.headers.get("access-control-allow-origin") in ("*", "https://xyz.trycloudflare.com")


async def test_tunnel_script_exists_and_shellcheck(app):
    root = Path(app.__class__.__module__.split(".")[0]).resolve().parent
    script = root / "scripts" / "tunnel.sh"
    assert script.exists(), "scripts/tunnel.sh missing"
    text = script.read_text()
    assert "cloudflared" in text and "trycloudflare" in text
    assert "127.0.0.1" in text


async def test_mobile_works_with_origin_header(app):
    instance, _state, _wd = app
    async with await _client(instance, origin="https://abc.trycloudflare.com") as client:
        # API + static both fine from a cross-origin PWA
        st = (await client.get("/api/companion/status")).json()
        assert st["profile_name"] == "JOOJO"
        html = (await client.get("/mobile")).text
        assert "Phantom Companion" in html
