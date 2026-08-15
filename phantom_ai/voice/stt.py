"""Speech-to-text abstraction. The agent core is text-based; voice is an
interface layer only. The shipped default is browser-side STT (Web Speech API,
push-to-talk); server-side providers plug in behind the same interface."""

from __future__ import annotations

import abc
from typing import Any


class STTProvider(abc.ABC):
    name = "abstract"

    @abc.abstractmethod
    async def transcribe(self, audio_path: str, language: str = "en") -> str:
        raise NotImplementedError


class BrowserSTTProvider(STTProvider):
    """Client-side speech recognition (Web Speech API in the browser UI).
    No server component needed; the UI sends the transcript as a normal chat
    message through the exact same agent core as typed input."""

    name = "browser"

    async def transcribe(self, audio_path: str, language: str = "en") -> str:
        raise RuntimeError("browser STT runs in the UI, not on the server")


class OpenAICompatSTTProvider(STTProvider):
    """Server-side STT against any OpenAI-compatible /audio/transcriptions
    endpoint (e.g. a self-hosted Whisper/NIM ASR). Configure in Settings."""

    name = "openai-compatible"

    def __init__(self, base_url: str, api_key: str, model: str = "whisper-1") -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    async def transcribe(self, audio_path: str, language: str = "en") -> str:
        import httpx

        async with httpx.AsyncClient(timeout=60.0) as client:
            with open(audio_path, "rb") as fh:
                resp = await client.post(
                    f"{self.base_url}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    data={"model": self.model, "language": language},
                    files={"file": (audio_path.split("/")[-1], fh)},
                )
            if resp.status_code != 200:
                raise RuntimeError(f"STT failed: HTTP {resp.status_code}: {resp.text[:200]}")
            return (resp.json() or {}).get("text", "")


class DeepgramSTTProvider(STTProvider):
    """Server-side Deepgram transcription (REST upload). Used by the companion
    voice-forwarding: the phone sends a WAV, the PC transcribes via Deepgram."""

    name = "deepgram"

    def __init__(self, api_key: str, model: str = "nova-2",
                 base_url: str = "https://api.deepgram.com/v1") -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")

    async def transcribe(self, audio_path: str, language: str = "en") -> str:
        import httpx

        async with httpx.AsyncClient(timeout=60.0) as client:
            with open(audio_path, "rb") as fh:
                resp = await client.post(
                    f"{self.base_url}/listen",
                    headers={"Authorization": f"Token {self.api_key}"},
                    params={"model": self.model, "punctuate": "true",
                            "language": language},
                    files={"audio": (audio_path.split("/")[-1], fh,
                                    "audio/wav")},
                )
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Deepgram STT failed: HTTP {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            transcript = (data.get("results") or {}).get("channels", [{}])[0] \
                .get("alternatives", [{}])[0].get("transcript", "")
            if not transcript.strip():
                raise RuntimeError("Deepgram returned empty transcript (no speech detected?)")
            return transcript.strip()


def create_stt_provider(config: dict[str, Any]) -> STTProvider:
    provider = config.get("provider", "browser")
    if provider == "browser":
        return BrowserSTTProvider()
    if provider == "deepgram":
        return DeepgramSTTProvider(
            api_key=config.get("api_key", ""),
            model=config.get("model", "nova-2"),
            base_url=config.get("base_url", "https://api.deepgram.com/v1"))
    if provider == "openai-compatible":
        return OpenAICompatSTTProvider(
            base_url=config.get("base_url", ""),
            api_key=config.get("api_key", ""),
            model=config.get("model", "whisper-1"),
        )
    raise ValueError(f"unknown STT provider: {provider}")
