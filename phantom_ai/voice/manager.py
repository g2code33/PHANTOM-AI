"""Central voice engine manager — multi-provider STT/TTS with automatic
failover, per-provider health tracking, exponential backoff, usage/cost
tracking and friendly errors.

STT chain (priority): Deepgram -> Groq Whisper -> Local Whisper (offline)
TTS chain (priority): Deepgram Aura -> configured cloud fallback -> Local
                      (Piper / espeak-ng)

No provider is retried forever: each is tried once per request, failures push
it into a backoff window (short for transient errors, long for invalid keys),
and the chain always ends in a friendly, actionable message instead of a raw
stack trace.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from ..config import DEEPGRAM_BASE_URL, GROQ_BASE_URL, now_iso
from .errors import VoiceProviderError
from .stt import (DeepgramSTTProvider, GroqWhisperSTTProvider,
                  LocalWhisperSTTProvider)
from .tts import (DeepgramAuraTTSProvider, LocalTTSProvider,
                  OpenAICompatTTSProvider)

log = logging.getLogger("phantom.voice")

DEFAULT_STT_PRIORITY = ["deepgram", "groq", "local_whisper"]
DEFAULT_TTS_PRIORITY = ["deepgram", "cloud", "local"]

_BACKOFF_BY_KIND = {
    "invalid_key": 600.0,
    "no_credits": 600.0,
    "rate_limited": 120.0,
    "timeout": 30.0,
    "network": 30.0,
    "provider_error": 20.0,
    "no_speech": 5.0,
    "not_installed": 5.0,
}


class ProviderHealth:
    """Per-provider health + backoff state for the status dashboard."""

    def __init__(self, name: str, role: str) -> None:
        self.name = name
        self.role = role  # "stt" | "tts"
        self.consecutive_failures = 0
        self.disabled_until = 0.0
        self.last_error = ""
        self.last_error_kind = ""
        self.last_used_at: Optional[str] = None
        self.last_ok_at: Optional[str] = None
        self.total_ok = 0
        self.total_errors = 0

    def available(self) -> bool:
        return time.time() >= self.disabled_until

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.total_ok += 1
        self.last_error = ""
        self.last_error_kind = ""
        self.last_ok_at = now_iso()
        self.last_used_at = now_iso()
        self.disabled_until = 0.0

    def record_failure(self, kind: str, friendly: str) -> None:
        self.consecutive_failures += 1
        self.total_errors += 1
        self.last_error = friendly
        self.last_error_kind = kind
        base = _BACKOFF_BY_KIND.get(kind, 30.0)
        backoff = min(base * (2 ** min(self.consecutive_failures - 1, 4)), 1800.0)
        self.disabled_until = time.time() + backoff
        self.last_used_at = now_iso()

    def state(self) -> str:
        if self.last_error_kind == "not_installed":
            return "unavailable"
        if not self.available():
            return "disabled"
        if self.total_errors == 0 and self.total_ok == 0:
            return "untested"
        if self.consecutive_failures == 0 and self.total_ok > 0:
            return "healthy"
        if self.consecutive_failures > 0:
            return "degraded"
        return "untested"

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "state": self.state(),
            "consecutive_failures": self.consecutive_failures,
            "disabled_for": max(0, self.disabled_until - time.time()),
            "last_error": self.last_error,
            "last_error_kind": self.last_error_kind,
            "last_used_at": self.last_used_at,
            "last_ok_at": self.last_ok_at,
            "total_ok": self.total_ok,
            "total_errors": self.total_errors,
        }


class VoiceUsageStore:
    """Persistent usage/cost ledger (voice_usage table in the main DB)."""

    def __init__(self, db: Any) -> None:
        self.db = db

    async def _ensure_table(self) -> None:
        await self.db.execute(
            "CREATE TABLE IF NOT EXISTS voice_usage ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " ts TEXT NOT NULL,"
            " kind TEXT NOT NULL,"          # stt | tts
            " provider TEXT NOT NULL,"
            " status TEXT NOT NULL,"        # ok | error | rate_limited | ...
            " audio_seconds REAL DEFAULT 0,"
            " chars INTEGER DEFAULT 0,"
            " detail TEXT DEFAULT '')")

    async def record(self, kind: str, provider: str, status: str,
                     audio_seconds: float = 0.0, chars: int = 0,
                     detail: str = "") -> None:
        try:
            await self._ensure_table()
            await self.db.execute(
                "INSERT INTO voice_usage (ts, kind, provider, status,"
                " audio_seconds, chars, detail) VALUES (?,?,?,?,?,?,?)",
                (now_iso(), kind, provider, status, float(audio_seconds),
                 int(chars), str(detail)[:200]))
        except Exception as exc:  # noqa: BLE001
            log.warning("voice usage record failed: %s", exc)

    async def summary(self, window_h: int = 24) -> dict[str, Any]:
        """Aggregate usage + estimated cost (rough public list prices)."""
        await self._ensure_table()
        since = time.time() - window_h * 3600
        # compute ISO timestamp of the cutoff
        from datetime import datetime, timezone
        since_iso = datetime.fromtimestamp(
            since, tz=timezone.utc).isoformat(timespec="milliseconds")

        rows = await self.db.fetchall(
            "SELECT kind, provider, status, audio_seconds, chars FROM voice_usage"
            " WHERE ts >= ?", (since_iso,))
        by_provider: dict[str, dict[str, Any]] = {}
        totals = {"requests": len(rows), "audio_seconds": 0.0, "chars": 0,
                  "est_cost_usd": 0.0, "errors": 0}
        recent_errors: list[dict[str, str]] = []
        for r in rows:
            key = f"{r['kind']}:{r['provider']}"
            slot = by_provider.setdefault(key, {
                "kind": r["kind"], "provider": r["provider"], "ok": 0,
                "errors": 0, "rate_limited": 0, "audio_seconds": 0.0,
                "chars": 0, "est_cost_usd": 0.0})
            totals["audio_seconds"] += r["audio_seconds"] or 0
            totals["chars"] += r["chars"] or 0
            slot["audio_seconds"] += r["audio_seconds"] or 0
            slot["chars"] += r["chars"] or 0
            if r["status"] == "ok":
                slot["ok"] += 1
            elif r["status"] == "rate_limited":
                slot["rate_limited"] += 1
                totals["errors"] += 1
            else:
                slot["errors"] += 1
                totals["errors"] += 1
            if r["status"] != "ok" and len(recent_errors) < 10:
                recent_errors.append({
                    "kind": r["kind"], "provider": r["provider"],
                    "status": r["status"], "detail": r["detail"] or ""})
            cost = _estimate_cost(r["kind"], r["provider"], r["audio_seconds"],
                                  r["chars"])
            slot["est_cost_usd"] = round(slot["est_cost_usd"] + cost, 6)
            totals["est_cost_usd"] = round(totals["est_cost_usd"] + cost, 6)
        return {"window_h": window_h, "totals": totals,
                "by_provider": list(by_provider.values()),
                "recent_errors": recent_errors}


_STT_RATE_PER_SEC = {"deepgram": 0.0043 / 60.0}   # nova-2 ~$0.0043/min
_TTS_RATE_PER_CHAR = {"deepgram": 0.015 / 1000.0,  # aura ~$0.015/1k chars
                      "cloud": 0.015 / 1000.0}


def _estimate_cost(kind: str, provider: str, seconds: float, chars: int) -> float:
    if kind == "stt":
        return (seconds or 0) * _STT_RATE_PER_SEC.get(provider, 0.0)
    return (chars or 0) * _TTS_RATE_PER_CHAR.get(provider, 0.0)


def wav_duration(path: str) -> float:
    """Duration in seconds from a RIFF/WAV header (no full parse needed)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(44)
        if len(head) < 44 or head[:4] != b"RIFF":
            return 0.0
        sample_rate = int.from_bytes(head[24:28], "little")
        channels = int.from_bytes(head[22:24], "little")
        bits = int.from_bytes(head[34:36], "little")
        data_size = int.from_bytes(head[40:44], "little")
        if sample_rate <= 0:
            return 0.0
        return data_size / (sample_rate * max(channels, 1) * max(bits // 8, 1))
    except Exception:  # noqa: BLE001
        return 0.0


class VoiceManager:
    """Owns the provider chains, failover, health, usage and status."""

    def __init__(self, secrets: Any, settings: Any, db: Any,
                 data_dir: str = "") -> None:
        self.secrets = secrets
        self.settings = settings
        self.data_dir = Path(data_dir)
        self.usage = VoiceUsageStore(db)
        self.health_map: dict[str, ProviderHealth] = {}
        self._whisper: Optional[LocalWhisperSTTProvider] = None
        self._whisper_task: Optional[asyncio.Task] = None
        self._last_active: dict[str, str] = {}
        self._stt_cache: dict[str, Any] = {}
        self._tts_cache: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _health(self, name: str, role: str) -> ProviderHealth:
        key = (name, role)
        h = self.health_map.get(key)
        if h is None:
            h = ProviderHealth(name, role)
            self.health_map[key] = h
        return h

    async def _priority(self, setting: str, default: list[str]) -> list[str]:
        try:
            prio = await self.settings.get(setting, "*", None)
        except Exception:  # noqa: BLE001
            prio = None
        if not prio:
            return list(default)
        if isinstance(prio, str):
            prio = [p.strip() for p in prio.split(",") if p.strip()]
        known = {name: i for i, name in enumerate(default)}
        return [p for p in prio if p in known]

    async def stt_priority(self) -> list[str]:
        return await self._priority("voice.stt_priority", DEFAULT_STT_PRIORITY)

    async def tts_priority(self) -> list[str]:
        return await self._priority("voice.tts_priority", DEFAULT_TTS_PRIORITY)

    # ------------------------------------------------------------------
    # availability checks (config presence / install detection)
    # ------------------------------------------------------------------
    async def whisper_provider(self) -> LocalWhisperSTTProvider:
        if self._whisper is None:
            model = "base"
            try:
                model = (await self.settings.get("voice.local_model", "*", "base")) or "base"
            except Exception:  # noqa: BLE001
                pass
            self._whisper = LocalWhisperSTTProvider(
                data_dir=str(self.data_dir), model=str(model))
            self._whisper_task = asyncio.get_event_loop().create_task(
                self._whisper.idle_release_loop())
        return self._whisper

    async def _stt_ready(self, name: str) -> tuple[bool, str]:
        if name == "deepgram":
            ok = bool(self.secrets.get("DEEPGRAM_API_KEY"))
            return ok, "" if ok else "No Deepgram key saved — add one in Settings"
        if name == "groq":
            ok = bool(self.secrets.get("GROQ_API_KEY"))
            return ok, "" if ok else "No Groq key saved — add one in Settings (free at groq.com)"
        if name == "local_whisper":
            wp = await self.whisper_provider()
            if wp.installed():
                return True, ""
            return False, wp.missing_reason()
        return False, f"Unknown STT provider '{name}'"

    async def _tts_ready(self, name: str) -> tuple[bool, str]:
        if name == "deepgram":
            ok = bool(self.secrets.get("DEEPGRAM_API_KEY"))
            return ok, "" if ok else "No Deepgram key saved — add one in Settings"
        if name == "cloud":
            cfg = await self._cloud_tts_config()
            return bool(cfg.get("base_url")), \
                ("Configure a cloud TTS endpoint in Settings → Voice → TTS fallback"
                 if not cfg.get("base_url") else "")
        if name == "local":
            lp = LocalTTSProvider(data_dir=str(self.data_dir))
            if lp.available():
                return True, ""
            return False, lp.missing_reason()
        return False, f"Unknown TTS provider '{name}'"

    async def _cloud_tts_config(self) -> dict[str, Any]:
        """OpenAI-compatible TTS fallback: base_url + key + model + voice."""
        try:
            cfg = await self.settings.get("voice.tts_cloud", "*", {}) or {}
        except Exception:  # noqa: BLE001
            cfg = {}
        key = self.secrets.get("VOICE_TTS_CLOUD_API_KEY") or \
            self.secrets.get("PHANTOM_NVIDIA_API_KEY") or \
            self.secrets.get("NVIDIA_API_KEY") or ""
        return {"base_url": (cfg.get("base_url") or "").strip(),
                "api_key": (cfg.get("api_key") or key or "").strip(),
                "model": cfg.get("model") or "tts-1",
                "voice": cfg.get("voice") or "alloy"}

    # ------------------------------------------------------------------
    # provider instances (cached, lazy)
    # ------------------------------------------------------------------
    async def _stt_provider(self, name: str):
        if name in self._stt_cache:
            return self._stt_cache[name]
        if name == "deepgram":
            prov = DeepgramSTTProvider(
                api_key=self.secrets.get("DEEPGRAM_API_KEY") or "",
                base_url=os.environ.get("PHAI_DEEPGRAM_BASE_URL", DEEPGRAM_BASE_URL))
        elif name == "groq":
            prov = GroqWhisperSTTProvider(
                api_key=self.secrets.get("GROQ_API_KEY") or "",
                base_url=os.environ.get("PHAI_GROQ_BASE_URL", GROQ_BASE_URL))
        elif name == "local_whisper":
            prov = await self.whisper_provider()
        else:
            raise ValueError(f"unknown STT provider {name}")
        self._stt_cache[name] = prov
        return prov

    async def _tts_provider(self, name: str):
        if name in self._tts_cache:
            return self._tts_cache[name]
        if name == "deepgram":
            prov = DeepgramAuraTTSProvider(
                api_key=self.secrets.get("DEEPGRAM_API_KEY") or "",
                base_url=os.environ.get("PHAI_DEEPGRAM_BASE_URL", DEEPGRAM_BASE_URL))
        elif name == "cloud":
            cfg = await self._cloud_tts_config()
            prov = OpenAICompatTTSProvider(
                base_url=cfg["base_url"], api_key=cfg["api_key"],
                model=cfg["model"], default_voice=cfg["voice"])
        elif name == "local":
            prov = LocalTTSProvider(data_dir=str(self.data_dir))
        else:
            raise ValueError(f"unknown TTS provider {name}")
        self._tts_cache[name] = prov
        return prov

    # ------------------------------------------------------------------
    # core pipelines with failover
    # ------------------------------------------------------------------
    async def transcribe(self, audio_path: str, language: str = "en") -> dict:
        """Run the STT chain. Returns {provider, text, fallbacks, audio_seconds}
        or raises VoiceProviderError with a friendly aggregate message."""
        seconds = wav_duration(audio_path)
        chain = await self.stt_priority()
        fallbacks: list[str] = []
        errors: list[str] = []
        for name in chain:
            health = self._health(name, "stt")
            ready, why = await self._stt_ready(name)
            if not ready:
                errors.append(why)
                continue
            if not health.available():
                errors.append(f"{_label(name)} is recovering — skipped")
                continue
            try:
                provider = await self._stt_provider(name)
                text = await provider.transcribe(audio_path, language=language)
                health.record_success()
                self._last_active["stt"] = name
                await self.usage.record("stt", name, "ok", seconds,
                                        chars=len(text))
                log.info("voice STT via %s (%ss)", name, round(seconds, 1))
                return {"provider": name, "text": text,
                        "fallbacks": fallbacks, "audio_seconds": seconds}
            except VoiceProviderError as exc:
                health.record_failure(exc.kind, exc.friendly)
                fallbacks.append(name)
                await self.usage.record(
                    "stt", name, exc.kind, seconds, detail=exc.friendly)
                errors.append(exc.friendly)
                log.warning("voice STT %s failed (%s): %s", name, exc.kind,
                            exc.friendly)
            except Exception as exc:  # noqa: BLE001
                health.record_failure("provider_error", str(exc)[:200])
                fallbacks.append(name)
                errors.append(f"{_label(name)} failed unexpectedly")
                log.exception("voice STT %s crashed", name)
        if not fallbacks:
            raise VoiceProviderError(
                "provider_error",
                "🎙️ Speech recognition isn't ready — " +
                ("; ".join(dict.fromkeys(errors))[:280] or "no providers configured"),
                "voice")
        raise VoiceProviderError(
            "provider_error",
            "🎙️ All speech providers failed — " +
            ("; ".join(dict.fromkeys(errors))[:280]), "voice")

    async def synthesize(self, text: str, voice: str = "",
                         output_path: str = "") -> dict:
        """Run the TTS chain. Returns {provider, audio_path, format, fallbacks}
        or raises VoiceProviderError with a friendly aggregate message."""
        text = (text or "").strip()
        if not text:
            raise VoiceProviderError("provider_error", "Nothing to say", "voice")
        out_path = output_path or str(self.data_dir / "tts_out.mp3")
        chain = await self.tts_priority()
        fallbacks: list[str] = []
        errors: list[str] = []
        for name in chain:
            health = self._health(name, "tts")
            ready, why = await self._tts_ready(name)
            if not ready:
                errors.append(why)
                continue
            if not health.available():
                errors.append(f"{_label(name)} is recovering — skipped")
                continue
            try:
                provider = await self._tts_provider(name)
                if name == "local":
                    out = out_path.rsplit(".", 1)[0] + ".wav"
                else:
                    out = out_path.rsplit(".", 1)[0] + ".mp3"
                path = await provider.synthesize(text, voice=voice, output_path=out)
                health.record_success()
                self._last_active["tts"] = name
                fmt = Path(path).suffix.lstrip(".")
                await self.usage.record("tts", name, "ok", chars=len(text))
                log.info("voice TTS via %s (%d chars)", name, len(text))
                return {"provider": name, "audio_path": path, "format": fmt,
                        "fallbacks": fallbacks}
            except VoiceProviderError as exc:
                health.record_failure(exc.kind, exc.friendly)
                fallbacks.append(name)
                await self.usage.record("tts", name, exc.kind,
                                        chars=len(text), detail=exc.friendly)
                errors.append(exc.friendly)
                log.warning("voice TTS %s failed (%s): %s", name, exc.kind,
                            exc.friendly)
            except Exception as exc:  # noqa: BLE001
                health.record_failure("provider_error", str(exc)[:200])
                fallbacks.append(name)
                errors.append(f"{_label(name)} failed unexpectedly")
                log.exception("voice TTS %s crashed", name)
        if not fallbacks:
            raise VoiceProviderError(
                "provider_error",
                "🔊 Text-to-speech isn't ready — " +
                ("; ".join(dict.fromkeys(errors))[:280] or "no providers configured"),
                "voice")
        raise VoiceProviderError(
            "provider_error",
            "🔊 All speech providers failed — " +
            ("; ".join(dict.fromkeys(errors))[:280]), "voice")

    # ------------------------------------------------------------------
    # status / usage / pipeline test
    # ------------------------------------------------------------------
    async def status(self) -> dict[str, Any]:
        stt_prio = await self.stt_priority()
        tts_prio = await self.tts_priority()
        providers: list[dict[str, Any]] = []
        seen: set[str] = set()
        for name in DEFAULT_STT_PRIORITY:
            ready, why = await self._stt_ready(name)
            h = self._health(name, "stt")
            snap = h.snapshot()
            if not ready:
                snap["state"] = "unavailable"
                snap["reason"] = why
            elif not h.available():
                snap["state"] = "disabled"
                snap["reason"] = snap["last_error"]
            providers.append({
                **snap,
                "label": _label(name), "role_label": "STT",
                "priority": stt_prio.index(name) + 1 if name in stt_prio else None,
                "configured": ready,
                "active": self._last_active.get("stt") == name,
            })
            seen.add(name)
        for name in DEFAULT_TTS_PRIORITY:
            ready, why = await self._tts_ready(name)
            h = self._health(name, "tts")
            snap = h.snapshot()
            if not ready:
                snap["state"] = "unavailable"
                snap["reason"] = why
            elif not h.available():
                snap["state"] = "disabled"
                snap["reason"] = snap["last_error"]
            providers.append({
                **snap,
                "label": _label(name), "role_label": "TTS",
                "priority": tts_prio.index(name) + 1 if name in tts_prio else None,
                "configured": ready,
                "active": self._last_active.get("tts") == name,
            })
            seen.add(name)
        wp = await self.whisper_provider()
        return {
            "providers": providers,
            "stt_priority": stt_prio,
            "tts_priority": tts_prio,
            "active_stt": self._last_active.get("stt", ""),
            "active_tts": self._last_active.get("tts", ""),
            "local_whisper": {
                "model": wp.model_size,
                "installed": wp.installed(),
                "loaded": wp.loaded(),
                "reason": wp.missing_reason() if not wp.installed() else "",
            },
        }

    async def usage_summary(self, window_h: int = 24) -> dict[str, Any]:
        return await self.usage.summary(window_h)

    def close(self) -> None:
        """Cancel background tasks (e.g. the whisper idle-release loop)."""
        if self._whisper_task is not None:
            self._whisper_task.cancel()
            self._whisper_task = None
        if self._whisper is not None:
            self._whisper.release()

    async def test_pipeline(
        self, audio_path: str, reply_fn: Optional[Callable[[str], Awaitable[str]]] = None,
        language: str = "en", output_path: str = "",
    ) -> dict[str, Any]:
        """Full pipeline: mic audio -> STT chain -> AI reply -> TTS chain ->
        audio. Used by the 'Test Voice System' button and tests."""
        stt = await self.transcribe(audio_path, language=language)
        transcript = stt["text"]
        if reply_fn is not None:
            reply = await reply_fn(transcript)
        else:
            reply = (f"Got it — you said: {transcript}")
        tts = await self.synthesize(reply, voice="", output_path=output_path)
        return {
            "ok": True,
            "transcript": transcript,
            "stt_provider": stt["provider"],
            "stt_fallbacks": stt["fallbacks"],
            "reply": reply,
            "tts_provider": tts["provider"],
            "tts_fallbacks": tts["fallbacks"],
            "audio_path": tts["audio_path"],
            "format": tts["format"],
        }


def _label(name: str) -> str:
    return {
        "deepgram": "Deepgram",
        "groq": "Groq Whisper",
        "local_whisper": "Local Whisper",
        "cloud": "Cloud TTS",
        "local": "Local TTS",
    }.get(name, name.title())
