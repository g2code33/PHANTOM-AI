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
import os
import sys
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


def find_resemblyzer_weights() -> str | None:
    """Locate resemblyzer's pretrained.pt for the (possibly frozen) app.

    Order: RESEMBLYZER_WEIGHTS env → bundled (_MEIPASS/resemblyzer/
    pretrained.pt, which PyInstaller data collection places there) → the
    package directory next to the installed resemblyzer. Returns None when
    not found (caller reports honestly)."""
    try:
        import resemblyzer
        pkg_dir = os.path.dirname(resemblyzer.__file__)
    except Exception:  # noqa: BLE001
        pkg_dir = ""
    meipass = getattr(sys, "_MEIPASS", "")
    candidates = [
        os.environ.get("RESEMBLYZER_WEIGHTS", ""),
        os.path.join(meipass, "resemblyzer", "pretrained.pt") if meipass else "",
        os.path.join(pkg_dir, "pretrained.pt") if pkg_dir else "",
    ]
    for cand in candidates:
        if cand and os.path.isfile(cand):
            return cand
    return None


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

    INSTALL_HINT = (
        "pip install --user resemblyzer torch\n"
        "# smaller CPU-only torch (optional, keeps it light):\n"
        "pip install --user torch --index-url https://download.pytorch.org/whl/cpu"
    )

    def _load_encoder(self) -> None:
        try:
            from resemblyzer import VoiceEncoder
        except Exception as exc:  # noqa: BLE001 — missing pkg
            self._unavailable_reason = (
                "resemblyzer is not installed — speaker lock needs it. "
                "Install once with: " + self.INSTALL_HINT.replace("\n", " / "))
            self._encoder = None
            raise SpeakerUnavailable(str(exc)) from None

        weights_fpath = find_resemblyzer_weights()
        if weights_fpath is None:
            self._unavailable_reason = (
                "resemblyzer pretrained weights not found — reinstall resemblyzer "
                "or set RESEMBLYZER_WEIGHTS to pretrained.pt")
            self._encoder = None
            raise SpeakerUnavailable(self._unavailable_reason) from None
        try:
            self._encoder = VoiceEncoder(weights_fpath=weights_fpath)
        except Exception as exc:  # noqa: BLE001 — torch issue
            self._unavailable_reason = (
                "speaker engine failed to load (torch problem): " + str(exc)[:120] +
                " — try: " + self.INSTALL_HINT.replace("\n", " / "))
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
                    "install_hint": self.INSTALL_HINT,
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
