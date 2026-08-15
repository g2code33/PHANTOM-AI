"""Monitoring engine — Jarvis Phase 5.

Watches everything it can watch locally and honestly:
  - system health (CPU, memory, disk, battery)
  - tasks (running / failed / queued)
  - audit signals (tool/API/delegation failures, denials in the last 24h)
  - schedules (upcoming)
  - health vault (today's wellness)
  - project/graph activity
  - opportunities (derived from the profile + failure patterns)

Quiet by default: it observes and reports on request; proactive pushes only
happen through the heartbeat + notification layer with cooldowns. Nothing here
is simulated — every field comes from a real store or OS read.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import psutil


class MonitorEngine:
    def __init__(self, settings: Any, audit: Any, tasks: Any, schedules: Any,
                 health: Any, graph: Any, profiles: Any, killswitch: Any,
                 events: Any) -> None:
        self.settings = settings
        self.audit = audit
        self.tasks = tasks
        self.schedules = schedules
        self.health = health
        self.graph = graph
        self.profiles = profiles
        self.killswitch = killswitch
        self.events = events
        self._last_snapshot: dict[str, Any] = {}
        self._last_at = 0.0

    # ------------------------------------------------------------------
    async def snapshot(self, cached: bool = True) -> dict[str, Any]:
        """Live monitor snapshot (cached 30s so the UI can poll freely)."""
        now = time.monotonic()
        if cached and self._last_snapshot and (now - self._last_at) < 30:
            return self._last_snapshot
        out: dict[str, Any] = {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "system": await self._system(),
            "tasks": await self._tasks(),
            "signals": await self._signals(),
            "upcoming": await self._upcoming(),
            "wellness": await self._wellness(),
            "opportunities": await self._opportunities(),
        }
        self._last_snapshot = out
        self._last_at = now
        return out

    # ------------------------------------------------------------------
    async def _system(self) -> dict[str, Any]:
        try:
            vm = psutil.virtual_memory()
            battery = None
            try:
                if psutil.sensors_battery() is not None:
                    b = psutil.sensors_battery()
                    battery = {"percent": b.percent, "plugged": b.power_plugged}
            except Exception:  # noqa: BLE001
                battery = None
            return {
                "cpu_percent": psutil.cpu_percent(interval=0.1),
                "memory_percent": vm.percent,
                "disk_percent": psutil.disk_usage("/").percent,
                "load_avg": [round(x, 2) for x in __import__("os").getloadavg()],
                "battery": battery,
            }
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)[:120]}

    async def _tasks(self) -> dict[str, Any]:
        rows = await self.tasks.list(limit=200)
        counts = {"running": 0, "queued": 0, "failed": 0, "completed": 0, "cancelled": 0}
        for t in rows:
            s = t.get("status")
            if s in counts:
                counts[s] += 1
        recent_failed = [t for t in rows
                         if t.get("status") == "failed"
                         and (t.get("ended_at") or "") >=
                         (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()]
        return {
            "counts": counts,
            "active": [{"id": t["id"], "name": t.get("name"),
                        "status": t.get("status"), "agent": t.get("agent")}
                       for t in rows if t.get("status") in ("running", "queued")][:10],
            "recent_failures": [{"id": t["id"], "name": t.get("name"),
                                 "error": (t.get("error") or "")[:160]}
                                for t in recent_failed][:5],
        }

    async def _signals(self) -> dict[str, Any]:
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        tool_err = await self.audit.query(event="tool.error", limit=500)
        tool_err = [e for e in tool_err if (e.get("ts") or "") >= since]
        api_err = await self.audit.query(event="api.error", limit=200)
        api_err = [e for e in api_err if (e.get("ts") or "") >= since]
        denied = await self.audit.query(event="confirmation.denied", limit=200)
        denied = [e for e in denied if (e.get("ts") or "") >= since]
        return {
            "tool_failures_24h": len(tool_err),
            "api_failures_24h": len(api_err),
            "denials_24h": len(denied),
            "top_tool_failures": [
                {"tool": (e.get("detail") or {}).get("tool"),
                 "kind": (e.get("detail") or {}).get("kind")}
                for e in tool_err[:5]],
        }

    async def _upcoming(self) -> list[dict[str, Any]]:
        schedules = await self.schedules.list()
        out = []
        now = datetime.now(timezone.utc)
        for s in schedules:
            if not s.get("enabled"):
                continue
            nxt = s.get("next_run_at")
            if not nxt:
                continue
            try:
                due = datetime.fromisoformat(nxt)
            except ValueError:
                continue
            if due < now:
                continue
            hours = (due - now).total_seconds() / 3600
            if hours < 72:  # next 3 days
                out.append({"name": s.get("name"), "agent": s.get("agent"),
                            "at": nxt, "in_hours": round(hours, 1)})
        out.sort(key=lambda x: x["in_hours"])
        return out[:8]

    async def _wellness(self) -> dict[str, Any]:
        try:
            if self.health is None or not await self.health.enabled():
                return {"enabled": False}
            today = await self.health.today()
            return {
                "enabled": True,
                "hydration_pct": today["hydration"]["pct"],
                "meals_logged": today["nutrition"]["meals_logged"],
                "sleep_hours": today["sleep"]["value"],
                "medications": len(today["medications"]),
                "appointments": len(today["appointments"]),
            }
        except Exception as exc:  # noqa: BLE001
            return {"enabled": False, "error": str(exc)[:120]}

    async def _opportunities(self) -> list[dict[str, Any]]:
        """Derived, honest suggestions based on real signals + the profile."""
        out: list[dict[str, Any]] = []
        signals = await self._signals()
        tasks = await self._tasks()
        profile = await self.profiles.default() if self.profiles else None
        fields = (profile or {}).get("fields") or {}
        projects = fields.get("projects") or []
        try:
            if tasks["recent_failures"]:
                out.append({
                    "priority": "high",
                    "title": f"{len(tasks['recent_failures'])} task(s) failed recently",
                    "detail": "Check the Task Manager — I can help debug or retry.",
                })
            if signals["tool_failures_24h"] >= 3:
                out.append({
                    "priority": "medium",
                    "title": f"{signals['tool_failures_24h']} tool failures in 24h",
                    "detail": "I can review the audit log and fix recurring tool issues.",
                })
            if projects and len(projects) >= 3:
                out.append({
                    "priority": "info",
                    "title": "Active project ecosystem",
                    "detail": f"{len(projects)} projects on your plate (incl. {projects[0].split('(')[0].strip()}). "
                              "Say 'brief me on my projects' for a full run-down.",
                })
            if signals["denials_24h"] >= 3:
                out.append({
                    "priority": "low",
                    "title": "Permission friction",
                    "detail": "Some actions were denied — review permission settings if I'm asking too often.",
                })
        except Exception:  # noqa: BLE001
            pass
        return out

    async def watch_list(self) -> dict[str, Any]:
        return {
            "watchers": [
                "system health (cpu/memory/disk/battery)",
                "tasks (running/failed/queued)",
                "audit signals (tool/api/delegation failures, denials)",
                "schedules & upcoming reminders",
                "health vault (today's wellness)",
                "projects & opportunities (from your profile)",
            ],
            "quiet_by_default": True,
        }
