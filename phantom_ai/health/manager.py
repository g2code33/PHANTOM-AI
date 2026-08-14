"""Health Manager — orchestrates the Health brain's features:

- Today overview (morning / during-day / evening) built locally from vault
  records and user settings (deterministic, privacy-friendly — no LLM needed).
- Measurement trends (display trends, never diagnoses).
- Personalized routines (morning/afternoon/evening) integrated with the
  scheduler (routines become heartbeat schedules + notifications).
- Daily briefing section (non-sensitive summaries by default).
- Consent/privacy settings and enable/disable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from ..config import now_iso
from .redflags import check_measurement, check_symptom
from .vault import CATEGORY_LABELS, HealthVault

TRACKABLE = ("hydration", "activity", "sleep", "nutrition", "medication",
             "appointments", "metrics", "goals")

DEFAULT_ROUTINE = {
    "morning": ["Hydration", "Medication reminder", "Breakfast", "Exercise"],
    "afternoon": ["Hydration", "Movement break", "Meal reminder"],
    "evening": ["Activity review", "Medication reminder", "Sleep preparation"],
}

DEFAULT_GOALS = {
    "water_liters": 2.0,
    "steps": 8000,
    "sleep_hours": 7.5,
    "meals": 3,
}


class HealthManager:
    def __init__(self, vault: HealthVault, settings: Any, schedules: Any,
                 notifications: Any, audit: Any, events: Any,
                 killswitch: Any) -> None:
        self.vault = vault
        self.settings = settings
        self.schedules = schedules          # ScheduleStore
        self.notifications = notifications  # NotificationStore
        self.audit = audit
        self.events = events
        self.killswitch = killswitch

    # ---- enable / disable ---------------------------------------------
    async def enabled(self) -> bool:
        return bool(await self.settings.get("health.enabled", "health", True))

    async def set_enabled(self, value: bool, by: str = "user") -> dict:
        await self.settings.set("health.enabled", bool(value), "health")
        await self.audit.record("health", "health.brain_" + ("enabled" if value else "disabled"),
                                {"by": by})
        return {"enabled": bool(value)}

    async def status(self) -> dict[str, Any]:
        enabled = await self.enabled()
        categories = await self.settings.get("health.tracked_categories", "health",
                                             list(TRACKABLE))
        return {
            "enabled": enabled,
            "vault_records": await self.vault.total(),
            "counts": await self.vault.counts(),
            "tracked_categories": [c for c in TRACKABLE if c in categories],
            "privacy": await self.privacy(),
            "categories": list(CATEGORY_LABELS.values()),
        }

    # ---- privacy / consent ---------------------------------------------
    async def privacy(self) -> dict[str, Any]:
        return {
            "encryption": "Fernet (AES-128-CBC + HMAC) at rest; key in chmod-600 secrets store",
            "share_with_other_brains": bool(await self.settings.get(
                "health.privacy.share_with_other_brains", "health", False)),
            "include_in_daily_briefing": bool(await self.settings.get(
                "health.privacy.include_in_daily_briefing", "health", True)),
            "briefing_sensitive": bool(await self.settings.get(
                "health.privacy.briefing_sensitive", "health", False)),
            "auto_inject_in_prompts": False,
        }

    async def set_privacy(self, **kwargs: Any) -> dict:
        allowed = {"share_with_other_brains", "include_in_daily_briefing",
                   "briefing_sensitive"}
        for key, value in kwargs.items():
            if key in allowed:
                await self.settings.set(f"health.privacy.{key}", bool(value), "health")
        await self.audit.record("health", "health.privacy_changed", {"keys": list(kwargs)})
        return await self.privacy()

    # ---- today overview -------------------------------------------------
    async def today(self) -> dict[str, Any]:
        today = datetime.now(timezone.utc).date().isoformat()
        habits = await self.vault.since("habit", cutoff_iso=today + "T00:00:00")
        measurements = await self.vault.since("measurement", cutoff_iso=today + "T00:00:00")
        meds = await self.vault.since("medication", cutoff_iso=today + "T00:00:00")
        appointments = await self.vault.since("appointment", cutoff_iso=today + "T00:00:00")
        goals_cfg = await self.settings.get("health.goals", "health", DEFAULT_GOALS)

        def _today_habits(kind: str) -> list[dict]:
            return [h["data"] for h in habits if (h["data"].get("kind") or "") == kind]

        water = _today_habits("water")
        meals = _today_habits("meal")
        exercise = _today_habits("exercise")
        water_liters = sum(float(w.get("amount", 0)) for w in water)

        sleep = next((m["data"] for m in measurements
                      if (m["data"].get("metric") or "") == "sleep"), None)
        steps = next((m["data"] for m in measurements
                      if (m["data"].get("metric") or "") == "steps"), None)

        goals = {
            "water_liters": float(goals_cfg.get("water_liters", 2.0)),
            "steps": int(goals_cfg.get("steps", 8000)),
            "sleep_hours": float(goals_cfg.get("sleep_hours", 7.5)),
            "meals": int(goals_cfg.get("meals", 3)),
        }
        water_pct = min(100, round(water_liters / goals["water_liters"] * 100)) \
            if goals["water_liters"] else 0
        meals_pct = min(100, round(len(meals) / goals["meals"] * 100)) if goals["meals"] else 0

        return {
            "date": today,
            "hydration": {"glasses": len(water), "liters": round(water_liters, 2),
                          "goal_liters": goals["water_liters"], "pct": water_pct},
            "nutrition": {"meals_logged": len(meals), "goal_meals": goals["meals"],
                          "pct": meals_pct},
            "activity": {"sessions": len(exercise),
                         "steps": int((steps or {}).get("value") or 0),
                         "goal_steps": goals["steps"]},
            "sleep": {"value": sleep.get("value") if sleep else None,
                      "unit": "h", "goal_hours": goals["sleep_hours"]},
            "medications": [{"title": m.get("title"), **m["data"]} for m in meds],
            "appointments": [{"title": a.get("title"), **a["data"]} for a in appointments],
            "routines": await self.routines(),
        }

    async def morning(self) -> str:
        t = await self.today()
        lines = [f"🌅 Morning health overview — {t['date']}"]
        if t["sleep"]["value"] is not None:
            lines.append(f"😴 Sleep: {t['sleep']['value']}h (goal {t['sleep']['goal_hours']}h)")
        else:
            lines.append("😴 Sleep: not recorded yet (optional)")
        lines.append(f"💧 Hydration goal: {t['hydration']['liters']}/{t['hydration']['goal_liters']}L "
                     f"({t['hydration']['pct']}%)")
        lines.append(f"🏃 Planned activity: {t['activity']['sessions']} session(s) logged")
        if t["medications"]:
            lines.append("💊 Medications scheduled: " +
                         ", ".join(m.get("title") or m.get("name") or "?" for m in t["medications"]))
        if t["appointments"]:
            lines.append("📅 Appointments: " +
                         ", ".join(a.get("title") or a.get("name") or "?" for a in t["appointments"]))
        return "\n".join(lines)

    async def evening(self) -> str:
        t = await self.today()
        lines = [f"🌙 Evening health summary — {t['date']}"]
        lines.append(f"💧 Hydration: {t['hydration']['liters']}L of {t['hydration']['goal_liters']}L "
                     f"goal ({t['hydration']['pct']}%)")
        lines.append(f"🍎 Meals logged: {t['nutrition']['meals_logged']} of "
                     f"{t['nutrition']['goal_meals']}")
        lines.append(f"🏃 Activity sessions: {t['activity']['sessions']} · "
                     f"steps: {t['activity']['steps']}/{t['activity']['goal_steps']}")
        missed = []
        if t["hydration"]["pct"] < 60:
            missed.append("hydration behind goal")
        if t["nutrition"]["meals_logged"] < t["nutrition"]["goal_meals"]:
            missed.append(f"{t['nutrition']['goal_meals'] - t['nutrition']['meals_logged']} meal(s) "
                          "not logged")
        if t["sleep"]["value"] is None:
            missed.append("sleep not recorded")
        lines.append("✅ Missed/behind: " + ("; ".join(missed) if missed else "nothing — well done"))
        lines.append("😴 Sleep preparation: wind down, limit screens, keep a consistent bedtime.")
        lines.append("📝 Tip: record tomorrow's plan in your routine to keep momentum.")
        return "\n".join(lines)

    # ---- trends -----------------------------------------------------------
    async def trends(self, metric: str, limit: int = 14) -> dict[str, Any]:
        rows = await self.vault.since("measurement", cutoff_iso="2000-01-01")
        points = []
        for r in rows:
            data = r["data"]
            if (data.get("metric") or "") != metric:
                continue
            try:
                points.append({
                    "value": float(data.get("value")),
                    "unit": data.get("unit", ""),
                    "when": r["recorded_at"],
                })
            except (TypeError, ValueError):
                continue
        points = points[-min(max(limit, 1), 90):]
        trend = None
        if len(points) >= 2:
            avg_old = sum(p["value"] for p in points[:-1]) / (len(points) - 1)
            trend = round(points[-1]["value"] - avg_old, 2)
        return {"metric": metric, "points": points,
                "count": len(points),
                "trend_vs_previous": trend,
                "latest": points[-1] if points else None}

    async def measurement_flags(self, metric: str, value: float, unit: str = "") -> list[dict]:
        flag = check_measurement(metric, value, unit)
        return [flag.to_dict()] if flag else []

    # ---- routines ---------------------------------------------------------
    async def routines(self) -> dict[str, list[str]]:
        return await self.settings.get("health.routine", "health", DEFAULT_ROUTINE)

    async def set_routine(self, block: str, items: list[str]) -> dict:
        routine = await self.routines()
        routine[block] = [str(i)[:120] for i in items]
        await self.settings.set("health.routine", routine, "health")
        await self.audit.record("health", "health.routine_updated", {"block": block})
        return routine

    async def schedule_routine(self, block: str, hour: int, minute: int = 0) -> dict:
        """Turn a routine block into a real heartbeat schedule for the Health
        brain (integration with Schedule + Notification brains)."""
        if block not in DEFAULT_ROUTINE:
            raise ValueError(f"unknown routine block: {block} (morning/afternoon/evening)")
        items = (await self.routines()).get(block, [])
        prompt = (f"Run health_daily_overview (morning/evening as appropriate). Then, for the "
                  f"{block} routine, remind the user about: {', '.join(items) or '(no items)'}. "
                  f"Use send_notification with a short, non-sensitive message. Follow all health "
                  f"safety rules.")
        name = f"Health routine · {block.capitalize()}"
        # remove any existing schedule with the same name to avoid duplicates
        for sched in await self.schedules.list("health"):
            if sched["name"] == name:
                await self.schedules.delete(sched["id"])
        sched = await self.schedules.create(
            "health", name, f"daily at {hour:02d}:{minute:02d}", prompt,
            quiet_start="22:00", quiet_end="07:00")
        from ..heartbeat.scheduler import next_run_at

        await self.schedules.update(sched["id"], next_run_at=next_run_at(f"daily at {hour:02d}:{minute:02d}"))
        return sched

    # ---- daily briefing contribution ---------------------------------------
    async def briefing_section(self) -> str:
        """Non-sensitive summary for the Daily Briefing (respects privacy
        settings: sensitive details are omitted unless opted in)."""
        t = await self.today()
        sensitive = await self.settings.get("health.privacy.briefing_sensitive", "health", False)
        lines = ["**Today's Health**"]
        lines.append(f"💧 Hydration goal: {t['hydration']['pct']}% complete "
                     f"({t['hydration']['liters']}L)")
        lines.append(f"🏃 Activity: {t['activity']['steps']} steps · "
                     f"{t['activity']['sessions']} session(s)")
        if t["sleep"]["value"] is not None:
            lines.append(f"😴 Sleep: {t['sleep']['value']}h")
        if sensitive:
            if t["medications"]:
                lines.append("💊 Medication reminders: " +
                             ", ".join(m.get("title") or "?" for m in t["medications"]))
            if t["appointments"]:
                lines.append("📅 Health appointments: " +
                             ", ".join(a.get("title") or "?" for a in t["appointments"]))
        return "\n".join(lines)
