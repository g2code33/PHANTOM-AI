"""Jarvis Phase 2 — per-brain API keys/model, speaker enrollment + speaker-lock
wake gating. The speaker embed() seam is mocked (deterministic vectors) so the
pipeline is tested without downloading model weights."""

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


def _sine_wav(seconds=1.0, freq=440.0, rate=16000) -> bytes:
    t = np.linspace(0, seconds, int(rate * seconds), endpoint=False)
    audio = (0.25 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    pcm = (audio * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _embed_fake_vector(seed: float) -> np.ndarray:
    rng = np.random.default_rng(int(seed * 1000))
    v = rng.normal(size=256).astype(np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


@pytest.fixture
async def app_with_mock_verifier(mock_server, workdir):
    """App whose SpeakerVerifier.verify uses deterministic fake embeddings."""
    state, base_url = mock_server
    os.environ["PHANTOM_NVIDIA_API_KEY"] = "nvapi-test-phantom-key-0000000000"
    os.environ["CODED_NVIDIA_API_KEY"] = "nvapi-test-coded-key-0000000000"
    os.environ["EVOLUTION_NVIDIA_API_KEY"] = "nvapi-test-evolution-key-0000000000"
    os.environ["HEALTH_NVIDIA_API_KEY"] = "nvapi-test-health-key-0000000000"
    for b in ("PHANTOM", "CODED", "EVOLUTION", "HEALTH", "PLANNER", "RESEARCH"):
        os.environ[f"BRAIN_{b}_NVIDIA_BASE_URL"] = base_url

    from phantom_ai.api.app import App

    instance = App(db_path=os.path.join(workdir, "test.db"),
                   data_dir=os.path.join(workdir, "data"), workspace_root=workdir)
    await instance.startup()

    # mock the embed seam with deterministic vectors per "speaker"
    enroll_key = {"seed": 1.0}
    other_key = {"seed": 9.0}

    def fake_embed(self, audio):
        # Deterministic per-audio vector: dominant frequency (zero-crossings).
        # freq >= 400 Hz -> "JOOJO" seed 1.0; freq < 400 Hz -> "other" seed 9.0
        if len(audio) < 64:
            return _embed_fake_vector(1.0)
        signs = np.sign(audio)
        crossings = int(np.sum(np.abs(np.diff(signs)) > 0.5))
        seconds = len(audio) / 16000.0
        freq = (crossings / 2) / max(seconds, 1e-6)
        return _embed_fake_vector(1.0 if freq >= 400 else 9.0)

    instance.speaker._embed = fake_embed.__get__(instance.speaker)
    instance.speaker._encoder = object()  # skip weight load
    yield instance, state, workdir
    await instance.shutdown()
    for var in list(os.environ):
        if var.startswith(("PHANTOM_NVIDIA", "CODED_NVIDIA", "EVOLUTION_NVIDIA",
                           "HEALTH_NVIDIA", "BRAIN_")):
            os.environ.pop(var, None)


# ---------------------------------------------------------------------------
# per-brain keys
# ---------------------------------------------------------------------------

async def test_brain_config_defaults(app):
    instance, _state, _wd = app
    async with await _client(instance) as client:
        cfg = (await client.get("/api/brains/planner/config")).json()
        assert cfg["brain_id"] == "planner"
        assert cfg["model"]
        assert cfg["provider"] in ("nvidia", "offline")


async def test_brain_own_key_model_and_rebuild(app):
    instance, _state, _wd = app
    async with await _client(instance) as client:
        res = await client.put("/api/brains/research/config", json={
            "api_key": "nvapi-own-key-for-research-000000",
            "model": "nvidia/deepseek-r1",
        })
        assert res.status_code == 200
        cfg = res.json()
        assert cfg["key_configured"] is True
        assert "nvapi-own-key-for-research" not in res.text  # masked
        assert "deepseek" in cfg["model"]
        # the research agent's provider now uses the own key
        assert instance.providers["research"].api_key == "nvapi-own-key-for-research-000000"
        assert instance.providers["research"].model == "nvidia/deepseek-r1"
        # other brains unaffected (still phantom fallback)
        assert instance.providers["planner"].api_key == "nvapi-test-phantom-key-0000000000"


async def test_brain_env_key_precedence(app, monkeypatch):
    instance, _state, _wd = app
    monkeypatch.setenv("BRAIN_TUTOR_NVIDIA_API_KEY", "nvapi-env-tutor-key-000000")
    brain = await instance.brains.get("tutor")
    from phantom_ai.brains.config import resolve_brain_config

    cfg = await resolve_brain_config("tutor", dict(brain), instance.secrets,
                                     instance.settings)
    assert cfg["api_key"] == "nvapi-env-tutor-key-000000"


async def test_brain_config_delete_key(app):
    instance, _state, _wd = app
    async with await _client(instance) as client:
        await client.put("/api/brains/planner/config", json={
            "api_key": "nvapi-temp-planner-key-000000"})
        assert instance.secrets.get("brain.planner.api_key")
        await client.put("/api/brains/planner/config", json={"delete_key": True})
        assert not instance.secrets.get("brain.planner.api_key")
        assert instance.providers["planner"].api_key == "nvapi-test-phantom-key-0000000000"


# ---------------------------------------------------------------------------
# speaker enrollment + speaker lock
# ---------------------------------------------------------------------------

async def test_enroll_flow_speaker_lock_gate(app_with_mock_verifier):
    instance, _state, _wd = app_with_mock_verifier
    async with await _client(instance) as client:
        # not enrolled yet → status
        st = (await client.get("/api/voice/enroll/status?agent=phantom")).json()
        assert st["enrolled"] is False

        # enroll 3 samples
        for i in range(3):
            r = await client.post("/api/voice/enroll/sample?agent=phantom",
                                  content=_sine_wav(freq=440 + i))
            assert r.status_code == 200
        st2 = (await client.get("/api/voice/enroll/status?agent=phantom")).json()
        assert st2["enrolled"] is True

        # enable speaker lock
        await client.put("/api/voice/enroll/speaker-lock", json={"enabled": True})

        # wake without sample → need_verification
        r = await client.post("/api/presence/wake", json={"agent": "phantom"})
        body = r.json()
        assert body["woken"] is False
        assert body["need_verification"] is True

        # wake with a MATCHING sample → woken
        # (fake_embed maps even-hash audio to seed 1.0 = enrollment seed)
        r2 = await client.post("/api/presence/wake",
                               json={"agent": "phantom",
                                     "sample_wav": base64.b64encode(
                                         _sine_wav(freq=440)).decode()})
        body2 = r2.json()
        assert body2["woken"] is True
        assert body2["active_agent"] == "phantom"


async def test_speaker_lock_denies_mismatched_voice(app_with_mock_verifier):
    instance, _state, _wd = app_with_mock_verifier
    async with await _client(instance) as client:
        for i in range(3):
            await client.post("/api/voice/enroll/sample?agent=phantom",
                              content=_sine_wav(freq=500 + i))
        await client.put("/api/voice/enroll/speaker-lock", json={"enabled": True})
        # 300 Hz → "other" voice → must be refused
        import base64 as _b64

        r = await client.post("/api/presence/wake",
                              json={"agent": "phantom",
                                    "sample_wav": _b64.b64encode(_sine_wav(freq=300)).decode()})
        body = r.json()
        assert body["woken"] is False
        assert body.get("need_verification") is not True
        assert "does not match" in body.get("reason", "") or body.get("score") is not None


async def test_speaker_lock_off_still_open(app_with_mock_verifier):
    instance, _state, _wd = app_with_mock_verifier
    async with await _client(instance) as client:
        r = await client.post("/api/presence/wake", json={"agent": "coded"})
        assert r.json()["woken"] is True


async def test_enroll_clear(app_with_mock_verifier):
    instance, _state, _wd = app_with_mock_verifier
    async with await _client(instance) as client:
        for _ in range(3):
            await client.post("/api/voice/enroll/sample?agent=phantom",
                              content=_sine_wav())
        assert (await client.get("/api/voice/enroll/status?agent=phantom")).json()["enrolled"]
        await client.post("/api/voice/enroll/clear", json={"agent": "phantom"})
        assert not (await client.get("/api/voice/enroll/status?agent=phantom")).json()["enrolled"]
