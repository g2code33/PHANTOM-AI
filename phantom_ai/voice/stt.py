"""Speech-to-text abstraction. The agent core is text-based; voice is an
interface layer only. The shipped default is browser-side STT (Web Speech API,
push-to-talk); server-side providers plug in behind the same interface."""

from __future__ import annotations

import abc
import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("phantom.voice.stt")


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

        from .errors import VoiceProviderError, classify_http_error

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                with open(audio_path, "rb") as fh:
                    resp = await client.post(
                        f"{self.base_url}/audio/transcriptions",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        data={"model": self.model, "language": language},
                        files={"file": (audio_path.split("/")[-1], fh)},
                    )
        except httpx.TimeoutException:
            raise VoiceProviderError("timeout", "STT timed out", "STT") from None
        except httpx.HTTPError:
            raise VoiceProviderError("network", "Could not reach the STT service", "STT") from None
        if resp.status_code != 200:
            raise classify_http_error("STT", resp.status_code, resp.text)
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

        from .errors import VoiceProviderError, classify_http_error

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                with open(audio_path, "rb") as fh:
                    resp = await client.post(
                        f"{self.base_url}/listen",
                        headers={"Authorization": f"Token {self.api_key}"},
                        params={"model": self.model, "punctuate": "true",
                                "language": language,
                                # explicit format so Deepgram never has to
                                # sniff ("corrupt or unsupported data" was
                                # the symptom when autodetect failed)
                                "container": "wav", "encoding": "linear16",
                                "sample_rate": "16000"},
                        files={"audio": (audio_path.split("/")[-1], fh,
                                        "audio/wav")},
                    )
        except httpx.TimeoutException:
            raise VoiceProviderError("timeout", "Deepgram STT timed out", "Deepgram") from None
        except httpx.HTTPError:
            raise VoiceProviderError("network", "Could not reach Deepgram — check internet", "Deepgram") from None
        if resp.status_code != 200:
            raise classify_http_error("Deepgram", resp.status_code, resp.text)
        data = resp.json()
        transcript = (data.get("results") or {}).get("channels", [{}])[0] \
            .get("alternatives", [{}])[0].get("transcript", "")
        if not transcript.strip():
            raise VoiceProviderError("no_speech", "No speech heard (Deepgram)", "Deepgram")
        return transcript.strip()


class GroqWhisperSTTProvider(STTProvider):
    """Server-side Groq Whisper transcription (whisper-large-v3-turbo).
    Free tier; used as the first cloud fallback when Deepgram is down."""

    name = "groq"

    def __init__(self, api_key: str, model: str = "whisper-large-v3-turbo",
                 base_url: str = "https://api.groq.com/openai/v1") -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")

    async def transcribe(self, audio_path: str, language: str = "en") -> str:
        import httpx

        from .errors import VoiceProviderError, classify_http_error

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                with open(audio_path, "rb") as fh:
                    resp = await client.post(
                        f"{self.base_url}/audio/transcriptions",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        data={"model": self.model, "language": language},
                        files={"file": (audio_path.split("/")[-1], fh,
                                        "audio/wav")},
                    )
        except httpx.TimeoutException:
            raise VoiceProviderError("timeout", "Groq Whisper timed out", "Groq") from None
        except httpx.HTTPError:
            raise VoiceProviderError("network", "Could not reach Groq — check internet", "Groq") from None
        if resp.status_code != 200:
            raise classify_http_error("Groq Whisper", resp.status_code, resp.text)
        text = ((resp.json() or {}).get("text") or "").strip()
        if not text:
            raise VoiceProviderError("no_speech", "No speech heard (Groq)", "Groq")
        return text


class LocalWhisperSTTProvider(STTProvider):
    """Truly offline STT via faster-whisper (tiny/base/small/medium/large).

    The model is loaded lazily ONLY when transcribing and released again after
    an idle timeout — it never runs in the background (RAM-friendly on
    JOOJO's 8 GB laptop). Model auto-detected in the standard cache dirs or
    data_dir/models/whisper-{size}."""

    name = "local_whisper"

    def __init__(self, data_dir: str = "", model: str = "base",
                 idle_ttl: float = 120.0, device: str = "auto") -> None:
        self.data_dir = data_dir
        self.model_size = model  # tiny|base|small|medium|large
        self.idle_ttl = idle_ttl
        self.device = device  # auto|cpu|cuda
        self._model = None  # loaded faster_whisper model (lazy)
        self._loaded_at = 0.0
        self._loaded_model_key = ""

    # -- detection ------------------------------------------------------
    @property
    def _cache_dirs(self) -> list[str]:
        dirs = []
        if self.data_dir:
            dirs.append(str(Path(self.data_dir) / "models" /
                           f"whisper-{self.model_size}"))
        home_cache = Path.home() / ".cache" / "huggingface" / "hub"
        repo = f"models--Systran--faster-whisper-{self.model_size}"
        dirs.append(str(home_cache / repo))
        return dirs

    def installed(self) -> bool:
        """True when faster_whisper is importable AND a model is present."""
        if self._model is not None:
            return True
        if not _faster_whisper_available():
            return False
        for d in self._cache_dirs:
            if (Path(d) / "model.bin").exists():
                return True
        return False

    def missing_reason(self) -> str:
        if not _faster_whisper_available():
            return ("Local Whisper needs the 'faster-whisper' package — run "
                    "`pip install faster-whisper` (or the installer will do it)")
        return (f"Whisper '{self.model_size}' model not found. Run "
                f"`python -m phantom_ai.voice.install_whisper {self.model_size}` "
                "to download it (needs internet once).")

    def _find_model_dir(self) -> str | None:
        for d in self._cache_dirs:
            if Path(d).exists():
                return d
        return None

    # -- lazy load / release ---------------------------------------------
    async def _load(self) -> None:
        from faster_whisper import WhisperModel  # type: ignore

        model_dir = self._find_model_dir()
        if model_dir is None:
            raise VoiceProviderError(
                "not_installed", self.missing_reason(), "Local Whisper")
        device = self.device
        if device == "auto":
            try:
                import torch  # type: ignore
                device = "cuda" if torch.cuda.is_available() else "cpu"
            except Exception:  # noqa: BLE001
                device = "cpu"  # torch not needed — ctranslate2 runs on CPU
        compute = "float16" if device == "cuda" else "int8"
        self._model = WhisperModel(model_dir, device=device, compute_type=compute)
        self._loaded_at = time.time()
        self._loaded_model_key = model_dir

    def release(self) -> None:
        """Free the loaded model (called after idle TTL)."""
        self._model = None
        self._loaded_model_key = ""

    def loaded(self) -> bool:
        return self._model is not None

    async def transcribe(self, audio_path: str, language: str = "en") -> str:
        from .errors import VoiceProviderError

        if not _faster_whisper_available():
            raise VoiceProviderError("not_installed", self.missing_reason(), "Local Whisper")
        if self._model is None:
            await self._load()
        self._loaded_at = time.time()
        # run the (blocking) transcription off the event loop
        segments, _info = await asyncio.get_event_loop().run_in_executor(
            None, lambda: self._model.transcribe(audio_path, language=language))  # type: ignore
        text = " ".join(s.text.strip() for s in segments).strip()
        if not text:
            raise VoiceProviderError("no_speech", "No speech heard (Local Whisper)", "Local Whisper")
        return text

    async def idle_release_loop(self) -> None:
        """Background task: unload the model after idle_ttl of inactivity."""
        try:
            while True:
                await asyncio.sleep(15)
                if self._model is not None and \
                        time.time() - self._loaded_at > self.idle_ttl:
                    log.info("local whisper idle — releasing model (RAM free)")
                    self.release()
        except asyncio.CancelledError:
            self.release()
            raise


def _faster_whisper_available() -> bool:
    # user-installed packages (frozen app: `pip install --user faster-whisper`)
    try:
        import site as _site

        for _p in (_site.getusersitepackages(),):
            if _p and _p not in sys.path:
                sys.path.insert(0, _p)
    except Exception:  # noqa: BLE001
        pass
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def create_stt_provider(config: dict[str, Any]) -> STTProvider:
    provider = config.get("provider", "browser")
    if provider == "browser":
        return BrowserSTTProvider()
    if provider == "deepgram":
        return DeepgramSTTProvider(
            api_key=config.get("api_key", ""),
            model=config.get("model", "nova-2"),
            base_url=config.get("base_url", "https://api.deepgram.com/v1"))
    if provider == "groq":
        return GroqWhisperSTTProvider(
            api_key=config.get("api_key", ""),
            model=config.get("model", "whisper-large-v3-turbo"),
            base_url=config.get("base_url", "https://api.groq.com/openai/v1"))
    if provider == "openai-compatible":
        return OpenAICompatSTTProvider(
            base_url=config.get("base_url", ""),
            api_key=config.get("api_key", ""),
            model=config.get("model", "whisper-1"),
        )
    raise ValueError(f"unknown STT provider: {provider}")
