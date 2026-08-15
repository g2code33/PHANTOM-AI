"""Companion service — the APK talks to the PC through this.

Jarvis Phase 7 (powerful + smart): the phone is a real remote for the PC:
  - /companion/status: one condensed payload (presence, agents, briefing,
    pending confirmations, active tasks, notifications, system) for a fast,
    low-bandwidth mobile screen.
  - confirmations: list + approve/deny from the phone (remote approval).
  - voice: phone sends a WAV, the PC transcribes server-side (Deepgram when
    configured), runs the requested agent, and streams the reply back over WS.
Everything is honest: if no STT key is configured, the phone gets a clear
message — no fake transcription.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import Any, Optional

from ..config import now_iso
from ..voice.stt import create_stt_provider


class CompanionService:
    def __init__(self, app: Any) -> None:
        self.app = app
        self._stt: Any = None
        self._stt_checked_at = 0.0

    # ------------------------------------------------------------------
    async def status(self) -> dict[str, Any]:
        app = self.app
        pending = []
        for agent_id in await app.brain_ids():
            pending += await app.confirmations.pending_for_agent(agent_id)
        tasks = await app.tasks.store.list(limit=50)
        active = [t for t in tasks if t.get("status") in ("running", "queued")]
        notifs = await app.notifications.list(limit=8)
        briefing = {}
        if app.briefing is not None:
            try:
                b = await app.briefing.get()
                briefing = {
                    "spoken": b.get("spoken", ""),
                    "priorities": [i["title"] for g in b.get("top_priorities", [])
                                   for i in g.get("items", [])][:5],
                }
            except Exception:  # noqa: BLE001
                briefing = {}
        presence = app.wake.state_dict() if app.wake else {}
        return {
            "version": "0.3.6",
            "generated_at": now_iso(),
            "presence": presence,
            "mode": (await app.settings.get("voice.mode", "*", "conversation")),
            "profile_name": (await app.profiles.default())["display_name"]
            if app.profiles else "User",
            "briefing": briefing,
            "pending_confirmations": [{
                "id": c["id"], "agent": c["agent"], "tool": c["tool_name"],
                "what": c.get("reason", "")[:200],
                "action": f"{c['tool_name']}({str(c.get('arguments'))[:120]})",
                "requested_at": c.get("requested_at", ""),
            } for c in pending[:10]],
            "active_tasks": [{"id": t["id"], "name": t.get("name"),
                              "status": t.get("status")} for t in active[:10]],
            "notifications": [{"id": n["id"], "title": n.get("title"),
                               "body": n.get("body", "")[:200],
                               "created_at": n.get("created_at", "")}
                              for n in notifs],
            "system": {
                "cpu": (await app.monitor.snapshot())["system"].get("cpu_percent"),
            } if app.monitor else {},
        }

    # ------------------------------------------------------------------
    async def decide_confirmation(self, confirmation_id: str, approve: bool) -> dict:
        row = await self.app.confirmations.decide(confirmation_id, approve,
                                                  decided_by="companion")
        return row

    async def pending_confirmations(self) -> list[dict]:
        out = []
        for agent_id in await self.app.brain_ids():
            for c in await self.app.confirmations.pending_for_agent(agent_id):
                out.append({"id": c["id"], "agent": c["agent"],
                            "tool": c["tool_name"],
                            "what": (c.get("reason") or "")[:200],
                            "action": f"{c['tool_name']}({str(c.get('arguments'))[:120]})",
                            "requested_at": c.get("requested_at")})
        return out

    # ------------------------------------------------------------------
    async def transcribe(self, wav_bytes: bytes) -> str:
        """Server-side STT (Deepgram when configured). Returns transcript or
        raises RuntimeError with a clear message."""
        key = self.app.secrets.get("DEEPGRAM_API_KEY")
        if not key:
            raise RuntimeError(
                "No STT configured on the PC — add a Deepgram API key in "
                "Settings → Voice, then voice forwarding will work.")
        provider = create_stt_provider({
            "provider": "deepgram", "api_key": key,
            "model": await self.app.settings.get("voice.stt.model", "*", "nova-2"),
        })
        tmp = Path(self.app.data_dir) / "companion-voice"
        tmp.mkdir(parents=True, exist_ok=True)
        path = tmp / f"{uuid.uuid4().hex}.wav"
        path.write_bytes(wav_bytes)
        try:
            return await provider.transcribe(str(path))
        finally:
            try:
                path.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass

    async def voice_forward(self, agent: str, wav_bytes: bytes) -> dict[str, Any]:
        """Phone voice → PC: transcribe → run the agent → return the reply.
        Events (chunks, tool activity, done) also stream over the shared WS."""
        if agent not in ("phantom", "coded"):
            raise ValueError("agent must be phantom or coded")
        text = await self.transcribe(wav_bytes)
        if not text.strip():
            raise ValueError("No speech detected in the audio.")
        conv = await self.app.conversations.create(agent)
        run_id = uuid.uuid4().hex
        task = asyncio.ensure_future(
            self.app.agents[agent].run(
                conversation_id=conv["id"], user_text=text,
                session_id=f"companion:{run_id}", mode="chat", run_id=run_id))
        try:
            result = await asyncio.wait_for(task, timeout=180)
        except asyncio.TimeoutError:
            task.cancel()
            raise RuntimeError("The agent took too long to respond — try again.") from None
        return {
            "run_id": run_id,
            "conversation_id": conv["id"],
            "text": text,
            "reply": result.content,
            "status": result.status,
            "tool_calls": result.tool_calls_made,
            "error": result.error,
        }
