"""Text-to-speech abstraction. Default: browser speech synthesis (real,
client-side). Server providers (OpenAI/NIM-compatible /audio/speech) plug in
behind the same interface. Speech is interruptible in the UI."""

from __future__ import annotations

import abc
from typing import Any


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
        import os

        out = output_path or "/tmp/phantom_tts.mp3"
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{self.base_url}/audio/speech",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.model, "voice": voice or self.default_voice,
                      "input": text[:4000]},
            )
            if resp.status_code != 200:
                raise RuntimeError(f"TTS failed: HTTP {resp.status_code}: {resp.text[:200]}")
            os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
            with open(out, "wb") as fh:
                fh.write(resp.content)
        return out


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
    raise ValueError(f"unknown TTS provider: {provider}")
