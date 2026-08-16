"""Jarvis Phase 8 — PWA: manifest, service worker, iOS icons, mobile shell
served for the iPhone companion (no App Store needed)."""

from __future__ import annotations

import httpx
import pytest

from phantom_ai.api.app import App
from phantom_ai.api.server import create_app


async def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                             base_url="http://test")


async def test_manifest_served_and_valid(app):
    instance, _state, _wd = app
    async with await _client(instance) as client:
        r = await client.get("/manifest.webmanifest")
        assert r.status_code == 200
        assert "application/manifest" in r.headers["content-type"]
        m = r.json()
        assert m["display"] == "standalone"
        assert m["start_url"] == "/mobile"
        assert m["name"] == "Phantom Companion"
        assert any(i["sizes"] == "512x512" for i in m["icons"])
        assert any(i.get("purpose") == "maskable" for i in m["icons"])


async def test_service_worker_served(app):
    instance, _state, _wd = app
    async with await _client(instance) as client:
        r = await client.get("/sw.js")
        assert r.status_code == 200
        assert "text/javascript" in r.headers["content-type"]
        assert "phantom-companion" in r.text
        # API calls are network-only in the SW (no stale cached API)
        assert '"/api/"' in r.text


async def test_ios_assets_served(app):
    instance, _state, _wd = app
    async with await _client(instance) as client:
        for path in ("/apple-touch-icon.png", "/icons/icon-192.png",
                     "/icons/icon-512.png", "/icons/icon-maskable-512.png"):
            r = await client.get(path)
            assert r.status_code == 200, path
            assert r.headers["content-type"].startswith("image/")


async def test_mobile_has_pwa_meta(app):
    instance, _state, _wd = app
    async with await _client(instance) as client:
        html = (await client.get("/mobile")).text
        assert 'rel="manifest"' in html
        assert 'rel="apple-touch-icon"' in html
        assert "apple-mobile-web-app-capable" in html
        assert "viewport-fit=cover" in html


async def test_ui_assets_all_served(app):
    """Every script/asset the UI references must be served with 200 — guards
    against the 405-regression (paths that only match the OPTIONS preflight
    route return 405 for GET, which silently breaks the page's scripts)."""
    instance, _state, _wd = app
    async with await _client(instance) as client:
        html = (await client.get("/")).text
        # collect the exact asset paths referenced by the page
        import re
        refs = set(re.findall(r'(?:src|href)="(/[^"#?]+)', html))
        refs |= {"/app.js", "/voice.js", "/hud.js", "/styles.css",
                 "/manifest.webmanifest", "/sw.js", "/apple-touch-icon.png"}
        assert refs, "no asset references found in index.html"
        for path in sorted(refs):
            r = await client.get(path)
            assert r.status_code == 200, f"{path} -> {r.status_code}"
            assert r.headers.get("content-type"), f"{path} missing content-type"
