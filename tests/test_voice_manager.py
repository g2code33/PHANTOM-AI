"""Multi-provider voice engine tests (spec §15 matrix).

Covers: Deepgram success/timeout/429/invalid key, Groq fallback/failure,
Local Whisper fallback, TTS failure, provider recovery (backoff), usage
tracking, friendly errors, status endpoint, and the full mic->STT->AI->TTS
pipeline via /api/voice/test.
"""

from __future__ import annotations

import asyncio
import os
import struct

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

VALID_DG = "dg-voice-test-valid-000000"
VALID_GROQ = "gsk-voice-test-valid-000000"

_ENV_VARS = ("PHAI_DEEPGRAM_BASE_URL", "PHAI_GROQ_BASE_URL",
             "PHAI_NVIDIA_BASE_URL")


def make_voice_mock() -> FastAPI:
    """Mock Deepgram (listen/speak), Groq (transcriptions) and a cloud TTS."""
    state = {
        "dg_stt_status": 200, "dg_stt_text": "hello phantom, testing voice",
        "dg_tts_status": 200, "groq_status": 200, "groq_text": "groq heard you",
        "cloud_tts_status": 200,
        "hits": {"dg_stt": 0, "groq": 0, "dg_tts": 0, "cloud_tts": 0},
    }
    app = FastAPI()

    @app.post("/v1/listen")
    async def dg_listen(request: Request):
        state["hits"]["dg_stt"] += 1
        if request.headers.get("Authorization") != f"Token {VALID_DG}":
            return JSONResponse(status_code=401, content={"err": "unauthorized"})
        if state["dg_stt_status"] != 200:
            return JSONResponse(status_code=state["dg_stt_status"],
                                content={"err": "mock failure"})
        return {"results": {"channels": [
            {"alternatives": [{"transcript": state["dg_stt_text"]}]}]}}

    @app.post("/v1/speak")
    async def dg_speak(request: Request):
        state["hits"]["dg_tts"] += 1
        if request.headers.get("Authorization") != f"Token {VALID_DG}":
            return JSONResponse(status_code=401, content={"err": "unauthorized"})
        if state["dg_tts_status"] != 200:
            return JSONResponse(status_code=state["dg_tts_status"],
                                content={"err": "mock failure"})
        return Response(b"ID3DGMP3" + b"\x00" * 64, media_type="audio/mpeg")

    @app.post("/openai/v1/audio/transcriptions")
    async def groq_transcribe(request: Request):
        state["hits"]["groq"] += 1
        if request.headers.get("Authorization") != f"Bearer {VALID_GROQ}":
            return JSONResponse(status_code=401, content={"err": "unauthorized"})
        if state["groq_status"] != 200:
            return JSONResponse(status_code=state["groq_status"],
                                content={"err": "mock failure"})
        return {"text": state["groq_text"]}

    @app.post("/cloud/v1/audio/speech")
    async def cloud_speech(request: Request):
        state["hits"]["cloud_tts"] += 1
        if state["cloud_tts_status"] != 200:
            return JSONResponse(status_code=state["cloud_tts_status"],
                                content={"err": "mock failure"})
        return Response(b"ID3CLOUD" + b"\x00" * 64, media_type="audio/mpeg")

    return app, state


@pytest.fixture
async def voice_mock():
    app, state = make_voice_mock()
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.02)
    assert server.started
    port = server.servers[0].sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    old = {v: os.environ.get(v) for v in _ENV_VARS}
    os.environ["PHAI_DEEPGRAM_BASE_URL"] = base + "/v1"
    os.environ["PHAI_GROQ_BASE_URL"] = base + "/openai/v1"
    os.environ["PHAI_NVIDIA_BASE_URL"] = base + "/v1"  # unused here
    yield state
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


def make_wav_bytes(seconds: float = 0.5) -> bytes:
    rate, channels, bits = 16000, 1, 16
    n = int(rate * seconds)
    data = b"\x00\x00" * n
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + \
        struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * channels * bits // 8,
                    channels * bits // 8, bits) + b"data" + struct.pack("<I", len(data))
    return header + data


async def _client(app) -> httpx.AsyncClient:
    from phantom_ai.api.server import create_app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                             base_url="http://test")


async def _configure_keys(c, dg=VALID_DG, groq=VALID_GROQ):
    body = {}
    if dg:
        body["deepgram_api_key"] = dg
    if groq:
        body["groq_api_key"] = groq
    if body:
        res = await c.put("/api/voice/config", json=body)
        assert res.status_code == 200
        assert res.json()["persisted"] is True


# ---------------------------------------------------------------------------
# STT chain
# ---------------------------------------------------------------------------


async def test_stt_primary_deepgram(app, voice_mock):
    instance, _state, _wd = app
    async with await _client(instance) as c:
        await _configure_keys(c)
        res = await c.post("/api/voice/stt",
                           files={"audio": ("u.wav", make_wav_bytes(), "audio/wav")},
                           data={"language": "en"})
        assert res.status_code == 200
        data = res.json()
        assert data["provider"] == "deepgram"
        assert "hello phantom" in data["text"]
        assert data["fallbacks"] == []
        # usage ledger recorded the request
        us = (await c.get("/api/voice/usage")).json()
        assert us["totals"]["requests"] >= 1
        stt_slots = [p for p in us["by_provider"] if p["kind"] == "stt"]
        assert any(p["provider"] == "deepgram" and p["ok"] >= 1 for p in stt_slots)


async def test_stt_failover_429_deepgram_to_groq(app, voice_mock):
    instance, _state, _wd = app
    voice_mock["dg_stt_status"] = 429
    async with await _client(instance) as c:
        await _configure_keys(c)
        res = await c.post("/api/voice/stt",
                           files={"audio": ("u.wav", make_wav_bytes(), "audio/wav")})
        assert res.status_code == 200
        data = res.json()
        assert data["provider"] == "groq"
        assert data["fallbacks"] == ["deepgram"]
        assert "groq heard you" in data["text"]


async def test_stt_all_cloud_fail_friendly(app, voice_mock):
    instance, _state, _wd = app
    voice_mock["dg_stt_status"] = 500
    voice_mock["groq_status"] = 401
    async with await _client(instance) as c:
        await _configure_keys(c)  # local whisper not installed → chain exhausts
        res = await c.post("/api/voice/stt",
                           files={"audio": ("u.wav", make_wav_bytes(), "audio/wav")})
        assert res.status_code == 502
        msg = res.json()["detail"]
        assert "failed" in msg or "rejected" in msg or "unavailable" in msg
        # never leak the keys
        assert VALID_DG not in msg and VALID_GROQ not in msg


async def test_stt_groq_invalid_key_friendly(app, voice_mock):
    instance, _state, _wd = app
    voice_mock["dg_stt_status"] = 500
    voice_mock["groq_status"] = 401
    async with await _client(instance) as c:
        await _configure_keys(c)
        res = await c.post("/api/voice/stt",
                           files={"audio": ("u.wav", make_wav_bytes(), "audio/wav")})
        assert res.status_code == 502
        assert "rejected" in res.json()["detail"]


async def test_stt_local_whisper_fallback(app, voice_mock, monkeypatch):
    from phantom_ai.voice.stt import LocalWhisperSTTProvider

    async def fake_transcribe(self, audio_path, language="en"):
        return "local whisper heard it"

    monkeypatch.setattr(LocalWhisperSTTProvider, "installed", lambda self: True)
    monkeypatch.setattr(LocalWhisperSTTProvider, "transcribe", fake_transcribe)

    instance, _state, _wd = app
    voice_mock["dg_stt_status"] = 429
    voice_mock["groq_status"] = 429
    async with await _client(instance) as c:
        await _configure_keys(c)
        res = await c.post("/api/voice/stt",
                           files={"audio": ("u.wav", make_wav_bytes(), "audio/wav")})
        assert res.status_code == 200
        data = res.json()
        assert data["provider"] == "local_whisper"
        assert data["fallbacks"] == ["deepgram", "groq"]
        assert data["text"] == "local whisper heard it"


async def test_stt_health_backoff_skips_deepgram(app, voice_mock):
    instance, _state, _wd = app
    voice_mock["dg_stt_status"] = 429
    async with await _client(instance) as c:
        await _configure_keys(c)
        # first: deepgram 429 → groq handles
        r1 = await c.post("/api/voice/stt",
                          files={"audio": ("u.wav", make_wav_bytes(), "audio/wav")})
        assert r1.json()["provider"] == "groq"
        # second: deepgram still failing and now backed off → groq again, and
        # deepgram is NOT retried inside this request
        r2 = await c.post("/api/voice/stt",
                          files={"audio": ("u.wav", make_wav_bytes(), "audio/wav")})
        assert r2.json()["provider"] == "groq"
        st = (await c.get("/api/voice/status")).json()
        dg = next(p for p in st["providers"] if p["name"] == "deepgram" and p["role"] == "stt")
        assert dg["state"] == "disabled"
        assert dg["last_error_kind"] == "rate_limited"


async def test_stt_provider_recovers(app, voice_mock):
    """Once the backoff window passes and the provider is healthy again,
    the chain returns to the preferred provider automatically."""
    instance, _state, _wd = app
    voice_mock["dg_stt_status"] = 429
    async with await _client(instance) as c:
        await _configure_keys(c)
        await c.post("/api/voice/stt", files={"audio": ("u.wav", make_wav_bytes())})
        # deepgram is now in backoff — groq handles the next request
        res = await c.post("/api/voice/stt", files={"audio": ("u.wav", make_wav_bytes())})
        assert res.json()["provider"] == "groq"
        # provider recovers + backoff expires → preferred provider returns
        voice_mock["dg_stt_status"] = 200
        instance.voice_mgr.health_map[("deepgram", "stt")].disabled_until = 0
        res = await c.post("/api/voice/stt", files={"audio": ("u.wav", make_wav_bytes())})
        assert res.json()["provider"] == "deepgram"
        st = (await c.get("/api/voice/status")).json()
        dg = next(p for p in st["providers"] if p["name"] == "deepgram" and p["role"] == "stt")
        assert dg["state"] == "healthy"


# ---------------------------------------------------------------------------
# TTS chain
# ---------------------------------------------------------------------------


async def test_tts_primary_deepgram(app, voice_mock):
    instance, _state, _wd = app
    async with await _client(instance) as c:
        await _configure_keys(c)
        res = await c.post("/api/voice/tts", json={"text": "Hello JOOJO"})
        assert res.status_code == 200
        assert res.headers.get("X-TTS-Provider") == "deepgram"
        body = await res.aread()
        assert len(body) > 0


async def test_tts_failover_to_local(app, voice_mock, monkeypatch):
    from phantom_ai.voice.tts import LocalTTSProvider

    async def fake_synth(self, text, voice="", output_path=""):
        import os
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "wb") as f:
            f.write(b"RIFFLOCAL")
        return output_path

    monkeypatch.setattr(LocalTTSProvider, "available", lambda self: True)
    monkeypatch.setattr(LocalTTSProvider, "synthesize", fake_synth)

    instance, _state, _wd = app
    voice_mock["dg_tts_status"] = 500
    async with await _client(instance) as c:
        await _configure_keys(c)
        res = await c.post("/api/voice/tts", json={"text": "Hello JOOJO"})
        assert res.status_code == 200
        assert res.headers.get("X-TTS-Provider") == "local"


async def test_tts_all_fail_friendly(app, voice_mock):
    instance, _state, _wd = app
    voice_mock["dg_tts_status"] = 500
    async with await _client(instance) as c:
        await _configure_keys(c)  # local TTS not installed in sandbox
        res = await c.post("/api/voice/tts", json={"text": "Hello JOOJO"})
        assert res.status_code == 502
        msg = res.json()["detail"]
        assert "failed" in msg or "unavailable" in msg or "recovering" in msg
        assert VALID_DG not in msg


# ---------------------------------------------------------------------------
# status / usage / full pipeline
# ---------------------------------------------------------------------------


async def test_status_endpoint(app, voice_mock):
    instance, _state, _wd = app
    async with await _client(instance) as c:
        await _configure_keys(c)
        st = (await c.get("/api/voice/status")).json()
        assert st["stt_priority"] == ["deepgram", "groq", "local_whisper"]
        assert st["tts_priority"] == ["deepgram", "cloud", "local"]
        names = {(p["name"], p["role"]) for p in st["providers"]}
        assert ("deepgram", "stt") in names and ("groq", "stt") in names
        assert ("local_whisper", "stt") in names
        assert ("deepgram", "tts") in names and ("local", "tts") in names
        dg = next(p for p in st["providers"] if p["name"] == "deepgram" and p["role"] == "stt")
        assert dg["configured"] is True
        assert dg["label"] == "Deepgram"
        assert isinstance(st["local_whisper"]["installed"], bool)
        assert st["local_whisper"]["model"] in ("tiny", "base", "small", "medium", "large")
        if not st["local_whisper"]["installed"]:
            assert st["local_whisper"]["reason"]  # honest explanation shown


async def test_usage_endpoint(app, voice_mock):
    instance, _state, _wd = app
    async with await _client(instance) as c:
        await _configure_keys(c)
        await c.post("/api/voice/stt", files={"audio": ("u.wav", make_wav_bytes(), "audio/wav")})
        await c.post("/api/voice/tts", json={"text": "hi"})
        us = (await c.get("/api/voice/usage")).json()
        assert us["totals"]["requests"] >= 2
        kinds = {p["kind"] for p in us["by_provider"]}
        assert {"stt", "tts"} <= kinds
        assert us["totals"]["audio_seconds"] >= 0.4  # ~0.5s wav


async def test_full_pipeline_test_endpoint(app, voice_mock):
    """mic -> STT -> Phantom AI -> TTS -> audio (the Test Voice System button)."""
    instance, state, _wd = app

    async def handler(body):
        return {"content": "I heard you loud and clear", "tool_calls": []}

    state.handler = handler
    async with await _client(instance) as c:
        await _configure_keys(c)
        res = await c.post("/api/voice/test",
                           files={"audio": ("u.wav", make_wav_bytes(0.5), "audio/wav")})
        assert res.status_code == 200
        assert res.headers.get("X-STT-Provider") == "deepgram"
        assert res.headers.get("X-TTS-Provider") == "deepgram"
        assert "hello phantom" in res.headers.get("X-Transcript", "")
        assert "loud and clear" in res.headers.get("X-Reply", "")
        body = await res.aread()
        assert len(body) > 0


async def test_wav_duration():
    from phantom_ai.voice.manager import wav_duration

    path = "/tmp/pcai_test_dur.wav"
    with open(path, "wb") as f:
        f.write(make_wav_bytes(1.0))
    assert abs(wav_duration(path) - 1.0) < 0.05
    os.unlink(path)
    assert wav_duration("/nonexistent.wav") == 0.0


def test_local_whisper_detection_honest():
    """When faster-whisper is missing OR the model is missing, the provider
    reports installed()=False with an actionable reason (never crashes)."""
    from phantom_ai.voice.stt import LocalWhisperSTTProvider

    p = LocalWhisperSTTProvider(data_dir="/nonexistent-phantom-models", model="base")
    assert p.installed() is False
    assert "whisper" in p.missing_reason().lower()
    assert p.loaded() is False
    p.release()  # safe no-op
