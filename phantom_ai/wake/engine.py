"""Wake engine + presence state machine — Jarvis Phase 1.

Presence states (Siri-style):
  sleeping  — the engine is quiet; only the wake-word detector listens (battery-light)
  waking    — wake word detected; ready-cue fired; about to accept speech
  listening — conversation active (the woken agent can hear/respond)
  silenced  — user said "stay silent"; the wake word can re-activate
  killed    — kill switch engaged (nothing listens)

Per-agent wake routing: "phantom" wakes Phantom, "coded" wakes Coded — either
at any time, even while the other is awake. They are addressed separately by
their wake word.

Rules enforced here (all configurable, all persisted):
  - idle timeout: back to sleeping after N minutes of no interaction (default 60)
  - ready cue: a distinct audio+visual cue is emitted on every wake
  - "stay silent" silences until the next wake word
  - kill switch stops everything immediately

The actual audio wake-word detection runs on the client (offline engine, e.g.
Picovoice/openWakeWord, or the browser) which reports wake events here; this
engine is the source of truth for presence state. Honest split: no fake audio.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from ..config import now_iso

AGENTS = ("phantom", "coded")

STATES = ("sleeping", "waking", "listening", "silenced", "killed")


@dataclass
class PresenceState:
    state: str = "sleeping"          # shared presence state
    active_agent: str = ""           # which agent is awake (phantom|coded|"")
    woke_at: str = ""
    last_activity: float = 0.0
    idle_minutes: int = 60
    ready_cue_sent_at: str = ""
    silenced_until: str = ""
    kill_engaged: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "active_agent": self.active_agent,
            "woke_at": self.woke_at,
            "last_activity": self.last_activity,
            "idle_minutes": self.idle_minutes,
            "ready_cue_sent_at": self.ready_cue_sent_at,
            "silenced_until": self.silenced_until,
            "kill_engaged": self.kill_engaged,
            "agents": {
                "phantom": {"wake_word": "phantom", "addressable": True},
                "coded": {"wake_word": "coded", "addressable": True},
            },
        }


class WakeEngine:
    def __init__(self, settings: Any, audit: Any, events: Any,
                 killswitch: Any, verifier: Any = None) -> None:
        self.settings = settings
        self.audit = audit
        self.events = events
        self.killswitch = killswitch
        self.verifier = verifier
        self._state = PresenceState()
        self._idle_task: Optional[asyncio.Task] = None
        self._enabled = True
        self._speaker_lock = False

    # ------------------------------------------------------------------
    async def start(self) -> None:
        self._enabled = bool(await self.settings.get("wake.enabled", "*", True))
        self._speaker_lock = bool(await self.settings.get(
            "wake.speaker_lock", "*", False))
        self._state.idle_minutes = int(await self.settings.get(
            "wake.idle_minutes", "*", 60))
        # on restart, presence starts sleeping (no fake "always awake")
        await self.publish()

    async def publish(self) -> None:
        await self.events.publish("presence.state", self._state.to_dict())

    # ------------------------------------------------------------------
    async def wake(self, agent: str, source: str = "wakeword",
                   wav_bytes: Optional[bytes] = None) -> dict:
        """Activate an agent by wake word.

        Speaker lock: when enabled AND an enrollment exists, the wake must be
        accompanied by a voice sample that matches JOOJO's voiceprint. If the
        sample is missing, returns need_verification=True so the client sends
        one; a mismatched voice is refused (no fake wake).
        """
        if not self._enabled or self.killswitch.is_engaged():
            return {"woken": False, "reason": "disabled or killed",
                    **self._state.to_dict()}
        if agent not in AGENTS:
            agent = "phantom"

        if self._speaker_lock and self.verifier is not None:
            try:
                enrolled = await self.verifier.enrolled(agent)
            except Exception:  # noqa: BLE001
                enrolled = False
            if enrolled:
                if not wav_bytes:
                    await self.events.publish("wake.need_verification",
                                              {"agent": agent})
                    return {"woken": False, "need_verification": True,
                            "reason": "speaker lock — voice sample required",
                            **self._state.to_dict()}
                try:
                    check = await self.verifier.verify(agent, wav_bytes)
                except Exception as exc:  # noqa: BLE001
                    return {"woken": False, "reason": f"verifier error: {exc}",
                            **self._state.to_dict()}
                if not check.get("verified"):
                    await self.events.publish("wake.denied", {
                        "agent": agent, "reason": check.get("reason")})
                    return {"woken": False, "reason": check.get("reason"),
                            "score": check.get("score"), **self._state.to_dict()}

        was_sleeping = self._state.state in ("sleeping", "silenced")
        self._state.state = "waking"
        self._state.active_agent = agent
        self._state.woke_at = now_iso()
        self._state.last_activity = time.time()
        self._state.silenced_until = ""
        await self.audit.record(agent, "presence.wake", {"source": source})
        await self.events.publish("wake.detected", {"agent": agent, "source": source})

        # ready cue (Siri-style) then enter listening
        self._state.ready_cue_sent_at = now_iso()
        await self.events.publish("ready_cue", {"agent": agent})
        await asyncio.sleep(0.05)  # allow the cue to flush before state lands
        self._state.state = "listening"
        await self.publish()
        self._schedule_idle()
        return {"woken": True, **self._state.to_dict()}

    async def touch(self) -> None:
        """Reset the idle timer on any interaction (chat, voice, etc.)."""
        self._state.last_activity = time.time()
        self._schedule_idle()

    async def sleep(self, reason: str = "idle") -> dict:
        self._state.state = "sleeping"
        self._state.active_agent = ""
        await self.audit.record("system", "presence.sleep", {"reason": reason})
        await self.publish()
        if self._idle_task:
            self._idle_task.cancel()
            self._idle_task = None
        return self._state.to_dict()

    async def stay_silent(self) -> dict:
        """User command: quiet until the next wake word."""
        self._state.state = "silenced"
        self._state.silenced_until = "until-wake"
        await self.audit.record("system", "presence.silenced", {})
        await self.publish()
        if self._idle_task:
            self._idle_task.cancel()
            self._idle_task = None
        return self._state.to_dict()

    async def on_killswitch(self) -> None:
        self._state.state = "killed"
        self._state.kill_engaged = True
        self._state.active_agent = ""
        await self.publish()

    async def on_killswitch_release(self) -> None:
        self._state.kill_engaged = False
        self._state.state = "sleeping"
        await self.publish()

    # ------------------------------------------------------------------
    def _schedule_idle(self) -> None:
        if self._idle_task:
            self._idle_task.cancel()
        self._idle_task = asyncio.ensure_future(self._idle_loop())

    async def _idle_loop(self) -> None:
        minutes = max(1, self._state.idle_minutes)
        await asyncio.sleep(minutes * 60)
        if self._state.state == "listening":
            await self.sleep(reason="idle-timeout")

    async def set_enabled(self, value: bool) -> dict:
        self._enabled = bool(value)
        await self.settings.set("wake.enabled", self._enabled, "*")
        if not self._enabled:
            await self.sleep(reason="disabled")
        return self._state.to_dict()

    async def set_speaker_lock(self, value: bool) -> dict:
        self._speaker_lock = bool(value)
        await self.settings.set("wake.speaker_lock", self._speaker_lock, "*")
        return self._state.to_dict()

    async def set_idle_minutes(self, minutes: int) -> dict:
        self._state.idle_minutes = max(1, min(int(minutes), 1440))
        await self.settings.set("wake.idle_minutes", self._state.idle_minutes, "*")
        return self._state.to_dict()

    def state_dict(self) -> dict[str, Any]:
        return self._state.to_dict()

    def is_listening(self) -> bool:
        return self._state.state == "listening"
