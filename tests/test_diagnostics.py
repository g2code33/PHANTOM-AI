"""In-app diagnostics: log capture + speaker install-hint + endpoint shape."""

from __future__ import annotations

import logging

from phantom_ai.api import server as srv


async def _client(app):
    import httpx
    from phantom_ai.api.server import create_app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                             base_url="http://test")


async def test_diagnostics_endpoint_shape(app):
    instance, _state, _wd = app
    async with await _client(instance) as c:
        r = await c.get("/api/diagnostics")
        assert r.status_code == 200
        d = r.json()
        assert "version" in d and "uptime_s" in d
        assert isinstance(d["logs"], list)
        assert "speaker" in d and "keys" in d
        assert d["keys"]["nvidia"] is True or d["keys"]["nvidia"] is False
        assert "wake" in d


async def test_diagnostics_captures_backend_logs(app):
    instance, _state, _wd = app
    logging.getLogger("phantom.api").warning("diagnostics-test-marker-xyz")
    async with await _client(instance) as c:
        d = (await c.get("/api/diagnostics")).json()
        assert any("diagnostics-test-marker-xyz" in line for line in d["logs"]), \
            "backend log line should appear in diagnostics"


async def test_speaker_install_hint_when_unavailable(app):
    """When resemblyzer isn't installed (test env), available() returns an
    actionable install_hint the UI shows as auto-instruct."""
    instance, _state, _wd = app
    async with await _client(instance) as c:
        d = (await c.get("/api/diagnostics")).json()
        sp = d["speaker"]
        assert sp is not None
        if not sp.get("available"):
            assert sp.get("reason"), "unavailable must explain why"
            assert "resemblyzer" in sp.get("install_hint", "").lower(), \
                "install_hint should mention resemblyzer"
        else:
            assert sp.get("threshold") is not None
