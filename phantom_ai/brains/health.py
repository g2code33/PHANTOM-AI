"""Brain health computation: derives live health/performance statistics from
the audit log, task records and delegations."""

from __future__ import annotations

from typing import Any

from ..config import now_iso


class BrainHealth:
    def __init__(self, audit: Any, tasks: Any, delegations: Any) -> None:
        self.audit = audit
        self.tasks = tasks
        self.delegations = delegations

    async def compute(self, brain_id: str, window_hours: int = 24) -> dict[str, Any]:
        from datetime import datetime, timedelta, timezone

        since = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()

        runs = await self.audit.query(agent=brain_id, event="agent.run_completed", limit=500)
        runs = [r for r in runs if (r.get("ts") or "") >= since]
        ok = sum(1 for r in runs if r.get("detail", {}).get("status") == "ok")
        failed = len(runs) - ok

        tool_events = await self.audit.query(agent=brain_id, event="tool.executed", limit=500)
        tool_events = [t for t in tool_events if (t.get("ts") or "") >= since]
        tool_errors = await self.audit.query(agent=brain_id, event="tool.error", limit=500)
        tool_errors = [t for t in tool_errors if (t.get("ts") or "") >= since]
        latencies = [t.get("latency_ms") for t in tool_events + runs if t.get("latency_ms")]
        avg_latency = round(sum(latencies) / len(latencies), 1) if latencies else None

        handoffs = await self.delegations.list(origin=brain_id, limit=200) \
            if self.delegations else []
        handoffs = [d for d in handoffs if (d.get("created_at") or "") >= since]
        handoff_failures = sum(1 for d in handoffs
                               if d.get("status") in ("failed", "timed_out", "blocked"))

        success_rate = round(ok / len(runs), 3) if runs else None
        error_rate = round(len(tool_errors) / max(len(tool_events), 1), 3)

        state = "online"
        if runs and success_rate is not None and success_rate <= 0.5:
            state = "degraded"
        if not runs and not tool_events:
            state = "idle"

        return {
            "state": state,
            "window_hours": window_hours,
            "tasks": len(runs), "succeeded": ok, "failed": failed,
            "success_rate": success_rate, "avg_latency_ms": avg_latency,
            "error_rate": error_rate, "handoff_failures": handoff_failures,
            "last_active": runs[0]["ts"] if runs else None,
            "computed_at": now_iso(),
        }

    async def update_all(self, registry: Any, window_hours: int = 24) -> None:
        for brain in await registry.list():
            if brain["status"] != "active":
                continue
            try:
                health = await self.compute(brain["id"], window_hours)
                stats = {
                    "success_rate": health["success_rate"],
                    "avg_latency_ms": health["avg_latency_ms"],
                    "error_rate": health["error_rate"],
                    "tasks": health["tasks"],
                    "last_active": health["last_active"],
                }
                await registry.update_health(brain["id"], stats, health)
            except Exception:  # noqa: BLE001 — health must never break the loop
                continue
