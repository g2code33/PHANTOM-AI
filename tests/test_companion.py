"""Jarvis Phase 7 — APK companion: status payload, remote confirmations,
voice forwarding (server STT + agent run), mobile UI served."""

from __future__ import annotations

import asyncio
import base64
import io
import os
import wave

import httpx
import numpy as np
import pytest

from phantom_ai.api.app import App
from phantom_ai.api.server import create_app


async def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                             base_url="http://test")


def _wav(seconds=1.0, freq=440.0) -> bytes:
    t = np.linspace(0, seconds, int(16000 * seconds), endpoint=False)
    audio = (0.25 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    pcm = (audio * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


async def test_companion_status_payload(app):
    instance, _state, _wd = app
    async with await _client(instance) as client:
        st = (await client.get("/api/companion/status")).json()
        assert st["version"]
        assert "presence" in st and st["presence"]["state"] in ("sleeping", "listening")
        assert "briefing" in st
        assert "pending_confirmations" in st
        assert "active_tasks" in st
        assert "notifications" in st
        assert "system" in st
        assert st["profile_name"] == "JOOJO"


async def test_remote_confirmation_approve_and_deny(app):
    instance, _state, _wd = app
    # create a pending confirmation via the store
    conf = await instance.confirmation_store.create(
        agent="phantom", conversation_id="c1", tool_name="delete_file",
        arguments={"path": "/tmp/x"}, reason="test", impact="file",
        risk="high")
    async with await _client(instance) as client:
        pending = (await client.get("/api/companion/confirmations")).json()
        assert any(c["id"] == conf["id"] for c in pending["confirmations"])
        # approve
        r = await client.post(f"/api/companion/confirmations/{conf['id']}/approve")
        assert r.status_code == 200
        assert (await client.get("/api/companion/confirmations")).json()["confirmations"] == []
        # deny a second one
        conf2 = await instance.confirmation_store.create(
            agent="coded", conversation_id="c2", tool_name="write_file",
            arguments={"path": "/tmp/y"}, reason="t2", impact="file", risk="low")
        r2 = await client.post(f"/api/companion/confirmations/{conf2['id']}/deny")
        assert r2.status_code == 200
        row = await instance.confirmation_store.get(conf2["id"])
        assert row["status"] == "denied"


async def test_voice_forward_requires_stt(app):
    """Without a Deepgram key the phone gets a clear 503, never a fake reply."""
    instance, _state, _wd = app
    async with await _client(instance) as client:
        r = await client.post("/api/companion/voice?agent=phantom", content=_wav())
        assert r.status_code == 503
        assert "Deepgram" in r.json()["detail"]


async def test_voice_forward_with_mock_stt_runs_agent(app, monkeypatch):
    """With STT available, phone voice → transcript → agent runs → reply."""
    instance, state, _wd = app
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test-key")
    # seed an enrollment-free wake not needed; just run the pipeline
    # mock the transcription + let the mock agent reply
    from phantom_ai.voice.stt import DeepgramSTTProvider

    async def fake_transcribe(self, audio_path, language="en"):
        return "hello phantom, how is my day"

    monkeypatch.setattr(DeepgramSTTProvider, "transcribe", fake_transcribe)

    async def handler(body):
        return {"content": "Your day looks good, JOOJO.", "tool_calls": []}

    state.handler = handler
    async with await _client(instance) as client:
        r = await client.post("/api/companion/voice?agent=phantom", content=_wav())
        assert r.status_code == 200
        j = r.json()
        assert "hello phantom" in j["text"]
        assert j["reply"]
        assert j["run_id"] and j["conversation_id"]
        assert j["status"] == "ok"


async def test_mobile_ui_served(app):
    instance, _state, _wd = app
    async with await _client(instance) as client:
        for path in ("/mobile", "/mobile.js", "/mobile.css"):
            r = await client.get(path)
            assert r.status_code == 200, path
        html = (await client.get("/mobile")).text
        assert "Phantom Companion" in html
        assert "mVoiceBtn" in html
