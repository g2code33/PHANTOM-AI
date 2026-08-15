"""Daily briefing engine — Jarvis Phase 5.

Builds the "everything, prioritized" morning briefing: calendar/schedules,
tasks, study plan, projects, updates, wellness, goals and opportunities.
- Pre-generated and cached so asking for it returns INSTANTLY.
- Quiet by default: never dumps everything — top priorities first, then the
  rest as a compact summary.
- Two formats: `spoken` (concise, addressed to JOOJO) and `structured`
  (cards for the UI).
- Regenerated on demand or on a schedule; the heartbeat speaks the high-
  priority items only when proactive speech is enabled.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Optional

from ..config import now_iso


class BriefingEngine:
    def __init__(self, monitor: Any, profiles: Any, memories: Any,
                 settings: Any, health: Any, events: Any) -> None:
        self.monitor = monitor
        self.profiles = profiles
        self.memories = memories
        self.settings = settings
        self.health = health
        self.events = events
        self._cache: dict[str, Any] = {}
        self._cache_at = 0.0

    # ------------------------------------------------------------------
    async def get(self, force: bool = False) -> dict[str, Any]:
        """Return the briefing — cached for instant output."""
        cache_ttl = int(await self.settings.get("briefing.cache_ttl", "*", 600))
        now = time.monotonic()
        if not force and self._cache and (now - self._cache_at) < cache_ttl:
            return self._cache
        briefing = await self._build()
        self._cache = briefing
        self._cache_at = now
        return briefing

    async def refresh(self) -> dict[str, Any]:
        return await self.get(force=True)

    # ------------------------------------------------------------------
    async def _build(self) -> dict[str, Any]:
        snap = await self.monitor.snapshot(cached=False)
        profile = await self.profiles.default() if self.profiles else None
        fields = (profile or {}).get("fields") or {}
        display_name = (profile or {}).get("display_name") or "JOOJO"

        # ---- gather sections -------------------------------------------------
        tasks = snap["tasks"]
        signals = snap["signals"]
        upcoming = snap["upcoming"]
        wellness = snap["wellness"]
        opportunities = snap["opportunities"]

        priorities: list[dict[str, Any]] = []
        sections: dict[str, Any] = {}

        # 1) failures / things needing attention (highest priority)
        attention = []
        for f in tasks.get("recent_failures", []):
            attention.append({"type": "task_failed", "title": f["name"],
                              "detail": f.get("error") or "failed"})
        if signals["tool_failures_24h"] >= 3:
            attention.append({"type": "tool_failures",
                              "title": f"{signals['tool_failures_24h']} tool failures in 24h",
                              "detail": "I can review and fix recurring issues."})
        if attention:
            priorities.append({"level": "high", "items": attention})

        # 2) tasks in flight
        if tasks["active"]:
            priorities.append({
                "level": "in_progress",
                "items": [{"type": "task", "title": t["name"],
                           "detail": t["status"]} for t in tasks["active"][:4]],
            })

        # 3) upcoming schedules/reminders
        if upcoming:
            priorities.append({
                "level": "upcoming",
                "items": [{"type": "schedule", "title": u["name"],
                           "detail": f"in {u['in_hours']}h"} for u in upcoming[:4]],
            })

        # 4) wellness (today)
        if wellness.get("enabled"):
            wl = []
            if wellness.get("hydration_pct") is not None:
                wl.append(f"hydration {wellness['hydration_pct']}%")
            if wellness.get("meals_logged") is not None:
                wl.append(f"{wellness['meals_logged']} meals")
            if wellness.get("sleep_hours"):
                wl.append(f"sleep {wellness['sleep_hours']}h")
            if wellness.get("medications"):
                wl.append(f"{wellness['medications']} medication reminder(s)")
            if wellness.get("appointments"):
                wl.append(f"{wellness['appointments']} appointment(s)")
            sections["wellness"] = {"title": "Today's Health", "items": wl}

        # 5) projects & goals from the profile
        projects = fields.get("projects") or []
        if projects:
            sections["projects"] = {
                "title": "Projects",
                "items": [p.split("(")[0].strip() for p in projects[:5]],
            }
        goals = fields.get("goals") or []
        if goals:
            sections["goals"] = {"title": "Goals", "items": list(goals)[:4]}

        # 6) study plan from memory
        study = []
        if self.memories is not None:
            try:
                mems = await self.memories.search("phantom", "study", limit=3)
                for m in mems:
                    if m.get("agent") == "shared" or m.get("kind") in ("project", "decision"):
                        continue
                    study.append(m["content"][:160])
            except Exception:  # noqa: BLE001
                pass
        if study:
            sections["study"] = {"title": "Study", "items": study[:3]}

        # 7) opportunities
        if opportunities:
            sections["opportunities"] = {
                "title": "Opportunities",
                "items": [o["title"] for o in opportunities[:3]],
            }

        # ---- top priorities (max 2) for the spoken summary ------------------
        spoken = self._spoken(display_name, priorities, sections)
        return {
            "generated_at": now_iso(),
            "for_user": display_name,
            "top_priorities": priorities,
            "sections": sections,
            "system": {
                "cpu": snap["system"].get("cpu_percent"),
                "memory": snap["system"].get("memory_percent"),
                "disk": snap["system"].get("disk_percent"),
            },
            "spoken": spoken,
        }

    # ------------------------------------------------------------------
    def _spoken(self, name: str, priorities: list[dict], sections: dict) -> str:
        lines = [f"Good {self._greeting()}, {name}."]
        tops = [item for group in priorities[:2] for item in group.get("items", [])][:2]
        if tops:
            lines.append("Here's what matters today.")
            for t in tops:
                lines.append(f"- {t['title']}" + (f": {t['detail']}" if t.get("detail") else ""))
        else:
            lines.append("Nothing urgent today.")
        # one-line summaries of the rest
        summaries = []
        if sections.get("wellness"):
            summaries.append("Health: " + ", ".join(sections["wellness"]["items"][:3]))
        if sections.get("projects"):
            summaries.append("Projects: " + ", ".join(sections["projects"]["items"][:3]))
        if sections.get("opportunities"):
            summaries.append("Opportunities: " + ", ".join(sections["opportunities"]["items"][:2]))
        if summaries:
            lines.append("Also: " + " | ".join(summaries))
        lines.append("I'm here if you need me.")
        return " ".join(lines)

    @staticmethod
    def _greeting() -> str:
        hour = datetime.now(timezone.utc).hour + 1  # +1 for Ghana (GMT)
        hour = hour % 24
        if hour < 12:
            return "morning"
        if hour < 17:
            return "afternoon"
        return "evening"
