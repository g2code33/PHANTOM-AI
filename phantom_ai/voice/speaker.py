"""Speaker enrollment + verification (speaker lock) — Jarvis Phase 2.

Only JOOJO's voice should wake Phantom/Coded. This uses a REAL local speaker
embedding model (resemblyzer — 256-dim voice embeddings, cosine similarity),
running fully offline on the machine.

- Enrollment: capture 3-5 utterances (16 kHz mono PCM16 WAV), embed each, store
  the mean embedding (base64) encrypted in the secrets store.
- Verification: embed a wake utterance, cosine-similarity vs the enrollment;
  pass if similarity >= threshold (default 0.72).
- Honest degradation: if resemblyzer (or its weights) are unavailable, the
  verifier reports `available: false` and the system runs WITHOUT speaker lock
  (wake stays open) and clearly says so — no fake verification.
- The embed() method is a thin seam so tests can inject deterministic vectors
  without downloading model weights.
"""

from __future__ import annotations

import base64
import io
import math
import wave
from typing import Any, Optional

from ..config import SecretsStore

ENROLL_SAMPLES_NEEDED = 3
THRESHOLD_DEFAULT = 0.72


class SpeakerUnavailable(Exception):
    pass


def parse_wav16k_mono(data: bytes):
    """Decode a 16 kHz mono PCM16 WAV into a float32 array (-1..1)."""
    import numpy as np

    with wave.open(io.BytesIO(data), "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError("expected 16-bit PCM")
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def _encode_wav16k_mono(audio) -> bytes:
    import numpy as np

    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


class SpeakerVerifier:
    def __init__(self, secrets: SecretsStore, audit: Any,
                 threshold: float = THRESHOLD_DEFAULT) -> None:
        self.secrets = secrets
        self.audit = audit
        self.threshold = threshold
        self._encoder: Any = None
        self._unavailable_reason: str = ""

    # ---- embed seam (real: resemblyzer; tests: monkeypatch) --------------
    def _embed(self, audio) -> "np.ndarray":
        if self._encoder is None:
            self._load_encoder()
        return self._encoder.embed_utterance(audio)

    def _load_encoder(self) -> None:
        try:
            from resemblyzer import VoiceEncoder

            self._encoder = VoiceEncoder()
        except Exception as exc:  # noqa: BLE001 — missing pkg or blocked weights
            self._unavailable_reason = str(exc)[:200]
            self._encoder = None
            raise SpeakerUnavailable(str(exc)) from None

    # ------------------------------------------------------------------
    async def available(self) -> dict[str, Any]:
        try:
            if self._encoder is None:
                self._load_encoder()
            return {"available": True, "threshold": self.threshold,
                    "samples_needed": ENROLL_SAMPLES_NEEDED}
        except SpeakerUnavailable as exc:
            return {"available": False, "reason": str(exc)[:200],
                    "threshold": self.threshold}

    async def enrolled(self, agent: str) -> bool:
        return bool(self.secrets.get(f"voice.enroll.{agent}"))

    # ------------------------------------------------------------------
    async def enroll_sample(self, agent: str, wav_bytes: bytes) -> dict[str, Any]:
        """Add one enrollment utterance; once >= 3 samples are stored, compute
        and persist the mean embedding."""
        import numpy as np

        audio = parse_wav16k_mono(wav_bytes)
        if len(audio) < 16000 * 0.5:
            raise ValueError("utterance too short (need >= 0.5s)")
        embedding = self._embed(audio)
        existing = self.secrets.get(f"voice.enroll.{agent}.samples") or ""
        samples = [s for s in existing.split("|") if s] if existing else []
        samples.append(base64.b64encode(embedding.astype(np.float32).tobytes()).decode())
        self.secrets.set(f"voice.enroll.{agent}.samples", "|".join(samples))
        if len(samples) >= ENROLL_SAMPLES_NEEDED:
            vecs = np.stack([
                np.frombuffer(base64.b64decode(s), dtype=np.float32)
                for s in samples[: ENROLL_SAMPLES_NEEDED]
            ])
            mean = vecs.mean(axis=0)
            mean = mean / (np.linalg.norm(mean) + 1e-9)
            self.secrets.set(
                f"voice.enroll.{agent}",
                base64.b64encode(mean.astype(np.float32).tobytes()).decode())
            self.secrets.delete(f"voice.enroll.{agent}.samples")
            await self.audit.record(agent, "voice.enrolled", {})
            return {"enrolled": True, "samples": ENROLL_SAMPLES_NEEDED,
                    "agent": agent}
        return {"enrolled": False, "samples": len(samples),
                "samples_needed": ENROLL_SAMPLES_NEEDED, "agent": agent}

    async def verify(self, agent: str, wav_bytes: bytes) -> dict[str, Any]:
        """Verify a wake utterance against the enrolled voiceprint."""
        import numpy as np

        enrolled_b64 = self.secrets.get(f"voice.enroll.{agent}")
        if not enrolled_b64:
            return {"verified": True, "reason": "no enrollment (speaker lock off)",
                    "score": None, "threshold": self.threshold}
        ref = np.frombuffer(base64.b64decode(enrolled_b64), dtype=np.float32)
        audio = parse_wav16k_mono(wav_bytes)
        if len(audio) < 16000 * 0.4:
            return {"verified": False, "reason": "utterance too short",
                    "score": 0.0, "threshold": self.threshold}
        emb = self._embed(audio)
        sim = float(np.dot(ref, emb) / (np.linalg.norm(ref) * np.linalg.norm(emb) + 1e-9))
        passed = sim >= self.threshold
        await self.audit.record(agent, "voice.verify",
                                {"passed": passed, "score": round(sim, 3)})
        return {"verified": bool(passed), "score": round(sim, 3),
                "threshold": self.threshold,
                "reason": "match" if passed else "voice does not match the enrolled user"}

    async def clear(self, agent: str) -> None:
        self.secrets.delete(f"voice.enroll.{agent}")
        self.secrets.delete(f"voice.enroll.{agent}.samples")
        await self.audit.record(agent, "voice.enrollment_cleared", {})

    async def set_threshold(self, value: float) -> None:
        self.threshold = max(0.5, min(0.95, float(value)))
