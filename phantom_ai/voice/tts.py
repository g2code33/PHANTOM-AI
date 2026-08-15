"""Text-to-speech abstraction. Default: browser speech synthesis (real,
client-side). Server providers (OpenAI/NIM-compatible /audio/speech) plug in
behind the same interface. Speech is interruptible in the UI."""

from __future__ import annotations

import abc
import asyncio
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

log = logging.getLogger("phantom.voice.tts")


class TTSProvider(abc.ABC):
    name = "abstract"

    @abc.abstractmethod
    async def synthesize(self, text: str, voice: str = "", output_path: str = "") -> str:
        """Return an audio file path (or a URL/trigger description)."""
        raise NotImplementedError


class BrowserTTSProvider(TTSProvider):
    name = "browser"

    async def synthesize(self, text: str, voice: str = "", output_path: str = "") -> str:
        raise RuntimeError("browser TTS runs in the UI (speechSynthesis), not on the server")


class OpenAICompatTTSProvider(TTSProvider):
    name = "openai-compatible"

    def __init__(self, base_url: str, api_key: str, model: str = "tts-1",
                 default_voice: str = "alloy") -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.default_voice = default_voice

    async def synthesize(self, text: str, voice: str = "", output_path: str = "") -> str:
        import httpx

        from .errors import VoiceProviderError, classify_http_error

        out = output_path or "/tmp/phantom_tts.mp3"
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    f"{self.base_url}/audio/speech",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={"model": self.model, "voice": voice or self.default_voice,
                          "input": text[:4000]},
                )
        except httpx.TimeoutException:
            raise VoiceProviderError("timeout", "Cloud TTS timed out", "Cloud TTS") from None
        except httpx.HTTPError:
            raise VoiceProviderError("network", "Could not reach the TTS service", "Cloud TTS") from None
        if resp.status_code != 200:
            raise classify_http_error("Cloud TTS", resp.status_code, resp.text)
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        with open(out, "wb") as fh:
            fh.write(resp.content)
        return out


class DeepgramAuraTTSProvider(TTSProvider):
    """Natural cloud TTS via Deepgram Aura (REST). Voices: aura-* (see the
    /api/voice/voices catalog)."""

    name = "deepgram"

    def __init__(self, api_key: str, default_voice: str = "aura-orion-en",
                 base_url: str = "https://api.deepgram.com/v1") -> None:
        self.api_key = api_key
        self.default_voice = default_voice
        self.base_url = base_url.rstrip("/")

    async def synthesize(self, text: str, voice: str = "", output_path: str = "") -> str:
        import httpx

        from .errors import VoiceProviderError, classify_http_error

        out = output_path or "/tmp/phantom_tts.mp3"
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    f"{self.base_url}/speak",
                    headers={"Authorization": f"Token {self.api_key}"},
                    params={"model": "aura-2-english", "encoding": "mp3",
                            "sample_rate": "24000"},
                    json={"text": text[:4000],
                          "voice": voice or self.default_voice},
                )
        except httpx.TimeoutException:
            raise VoiceProviderError("timeout", "Deepgram Aura TTS timed out", "Deepgram") from None
        except httpx.HTTPError:
            raise VoiceProviderError("network", "Could not reach Deepgram — check internet", "Deepgram") from None
        if resp.status_code != 200:
            raise classify_http_error("Deepgram Aura", resp.status_code, resp.text)
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        with open(out, "wb") as fh:
            fh.write(resp.content)
        return out


class LocalTTSProvider(TTSProvider):
    """Offline TTS. Prefers Piper (natural) if installed + model present;
    falls back to espeak-ng (robust, always available on most Linux boxes).
    No cloud needed — voice keeps working offline."""

    name = "local"

    def __init__(self, data_dir: str = "", default_voice: str = "") -> None:
        self.data_dir = data_dir
        self.default_voice = default_voice

    # -- detection ------------------------------------------------------
    def piper_binary(self) -> str | None:
        return shutil.which("piper")

    def piper_model(self) -> str | None:
        if not self.data_dir:
            return None
        base = Path(self.data_dir) / "models" / "piper"
        if not base.exists():
            return None
        onnx = sorted(base.glob("*.onnx"))
        return str(onnx[0]) if onnx else None

    def espeak_binary(self) -> str | None:
        return shutil.which("espeak-ng") or shutil.which("espeak")

    def available(self) -> bool:
        return bool(self.piper_binary() and self.piper_model()) or \
            bool(self.espeak_binary())

    def missing_reason(self) -> str:
        if not self.piper_binary() and not self.espeak_binary():
            return ("No local TTS engine found — install espeak-ng "
                    "(`sudo apt install espeak-ng`) or Piper for nicer voices")
        if self.piper_binary() and not self.piper_model():
            return "Piper is installed but no voice model found"
        return "Local TTS not available"

    async def synthesize(self, text: str, voice: str = "", output_path: str = "") -> str:
        from .errors import VoiceProviderError

        out = output_path or "/tmp/phantom_tts.wav"
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        # Piper first (nicer), espeak-ng as the safe fallback
        if self.piper_binary() and self.piper_model():
            cmd = [self.piper_binary(), "--model", self.piper_model(),
                   "--output_file", out]
            model_json = Path(self.piper_model()).with_suffix(".onnx.json")
            if model_json.exists():
                cmd += ["--config", str(model_json)]
            proc = await asyncio.get_event_loop().run_in_executor(
                None, lambda: subprocess.run(
                    cmd, input=text.encode("utf-8"), capture_output=True, timeout=60))
            if proc.returncode == 0 and Path(out).exists():
                return out
            log.warning("piper failed (%s), falling back to espeak-ng",
                        proc.stderr.decode(errors="replace")[:120])
        espeak = self.espeak_binary()
        if espeak:
            voice_flag = self.default_voice or "en-us"
            if voice:
                voice_flag = voice if "-" in voice else f"{voice}-us"
            proc = await asyncio.get_event_loop().run_in_executor(
                None, lambda: subprocess.run(
                    [espeak, "-v", voice_flag, "-w", out, text[:2000]],
                    capture_output=True, timeout=60))
            if proc.returncode == 0 and Path(out).exists():
                return out
            raise VoiceProviderError("provider_error",
                                     "Local TTS failed to speak", "Local TTS")
        raise VoiceProviderError("not_installed", self.missing_reason(), "Local TTS")


def create_tts_provider(config: dict[str, Any]) -> TTSProvider:
    provider = config.get("provider", "browser")
    if provider == "browser":
        return BrowserTTSProvider()
    if provider == "openai-compatible":
        return OpenAICompatTTSProvider(
            base_url=config.get("base_url", ""),
            api_key=config.get("api_key", ""),
            model=config.get("model", "tts-1"),
            default_voice=config.get("voice", "alloy"),
        )
    if provider == "deepgram":
        return DeepgramAuraTTSProvider(
            api_key=config.get("api_key", ""),
            default_voice=config.get("voice", "aura-orion-en"),
            base_url=config.get("base_url", "https://api.deepgram.com/v1"))
    if provider == "local":
        return LocalTTSProvider(
            data_dir=config.get("data_dir", ""),
            default_voice=config.get("voice", "en-us"))
    raise ValueError(f"unknown TTS provider: {provider}")
