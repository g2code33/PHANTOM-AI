"""Proactive heartbeat: scheduled agent checks and reminders.

Design (quiet by default):
- Schedules support: cron (5-field), "every 30m/2h", "hourly", "daily at HH:MM", "at HH:MM".
- Quiet hours suppress runs and defer them.
- Missed runs (e.g. the app was closed) are caught up on restart within a window.
- Results become dismissible notifications, never silent high-risk actions.
- The kill switch stops the scheduler immediately.
- No duplicate runs: next_run_at is advanced atomically per tick.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ..config import now_iso
from ..storage.ops import ScheduleStore

TICK_SECONDS = 10
MAX_CATCHUP_HOURS = 24


def parse_expression(expression: str) -> dict:
    """Parse a schedule expression. Returns {'kind':..., 'every_sec':...} or
    {'kind':'cron','minute':...,'hour':...,'dom':...,'month':...,'dow':...}."""
    expr = expression.strip().lower()
    m = re.match(r"^every\s+(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours)$", expr)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        mult = {"s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
                "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
                "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600}[unit]
        return {"kind": "interval", "every_sec": n * mult}
    if expr == "hourly":
        return {"kind": "interval", "every_sec": 3600}
    m = re.match(r"^(?:daily\s+at\s+|at\s+)(\d{1,2}):(\d{2})$", expr)
    if m:
        return {"kind": "daily", "hour": int(m.group(1)), "minute": int(m.group(2))}
    parts = expr.split()
    field_re = re.compile(r"^(\*|\d{1,2}|\*/\d{1,2})$")
    if len(parts) == 5 and all(field_re.fullmatch(p) for p in parts):
        return {"kind": "cron", "minute": parts[0], "hour": parts[1], "dom": parts[2],
                "month": parts[3], "dow": parts[4]}
    raise ValueError(
        "unsupported schedule expression — use 'every 30m', 'hourly', 'daily at 09:00', "
        "'at 18:30', or a 5-field cron like '*/10 * * * *'")


def next_run_at(expression: str, from_dt: Optional[datetime] = None) -> str:
    parsed = parse_expression(expression)
    base = from_dt or datetime.now(timezone.utc)
    if parsed["kind"] == "interval":
        return (base + timedelta(seconds=parsed["every_sec"])).isoformat(timespec="milliseconds")
    if parsed["kind"] == "daily":
        nxt = base.replace(hour=parsed["hour"], minute=parsed["minute"], second=0, microsecond=0)
        if nxt <= base:
            nxt += timedelta(days=1)
        return nxt.isoformat(timespec="milliseconds")
    # cron (limited to minute/hour granularity)
    minute, hour = parsed["minute"], parsed["hour"]
    nxt = base.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(60 * 24 * 2):
        if _matches(minute, nxt.minute) and _matches(hour, nxt.hour):
            return nxt.isoformat(timespec="milliseconds")
        nxt += timedelta(minutes=1)
    return (base + timedelta(days=1)).isoformat(timespec="milliseconds")


def _matches(field: str, value: int) -> bool:
    if field == "*":
        return True
    if "/" in field:
        base, step = field.split("/")
        if base == "*" and value % int(step) == 0:
            return True
        return value == int(base) and value % int(step) == 0
    return value == int(field)


class HeartbeatScheduler:
    def __init__(self, store: ScheduleStore, settings: Any, task_manager: Any,
                 agent_runner: Any, events: Any, killswitch: Any,
                 notification_store: Any) -> None:
        self.store = store
        self.settings = settings
        self.task_manager = task_manager
        self.agent_runner = agent_runner
        self.events = events
        self.killswitch = killswitch
        self.notifications = notification_store
        self._running = False
        self._wake = asyncio.Event()

    async def start(self) -> None:
        self._running = True
        asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        self._running = False
        self._wake.set()

    async def _loop(self) -> None:
        while self._running:
            try:
                if self.killswitch.is_engaged():
                    await asyncio.sleep(TICK_SECONDS)
                    continue
                await self._tick()
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001 — never let one bad tick kill the loop
                pass
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=TICK_SECONDS)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                return
            self._wake.clear()

    async def _tick(self) -> None:
        if self.killswitch.is_engaged():
            return  # kill switch stops the heartbeat immediately
        now = datetime.now(timezone.utc)
        schedules = await self.store.list()
        for sched in schedules:
            if not sched["enabled"]:
                continue
            nxt = sched["next_run_at"]
            if nxt is None:
                nxt = next_run_at(sched["expression"])
                await self.store.update(sched["id"], next_run_at=nxt)
                continue
            try:
                due_at = datetime.fromisoformat(nxt)
            except ValueError:
                due_at = now
            if due_at > now:
                continue
            # advance first (no duplicates even if the run takes long)
            nxt_new = next_run_at(sched["expression"], from_dt=now)
            await self.store.update(sched["id"], next_run_at=nxt_new)
            if (now - due_at).total_seconds() > MAX_CATCHUP_HOURS * 3600:
                await self.store.update(sched["id"], last_status="skipped",
                                        last_result="missed by > catch-up window")
                continue
            await self._run_schedule(sched)

    async def _run_schedule(self, sched: dict) -> None:
        if self._in_quiet_hours(sched):
            await self.store.update(sched["id"], last_status="deferred",
                                    last_result="quiet hours")
            return
        agent = sched["agent"]
        await self.store.update(sched["id"], last_status="running")
        await self.events.publish("heartbeat.run", {"schedule_id": sched["id"],
                                                    "name": sched["name"], "agent": agent})

        async def factory(task_id: str):
            from ..config import now_iso

            result = await self.agent_runner(
                agent_id=agent, conversation_id=_conv_id(sched), user_text=sched["prompt"],
                session_id=f"heartbeat:{sched['id']}", mode="proactive",
            )
            summary = (result.get("content") or result.get("error") or "no output")[:800]
            status = "ok" if result.get("status") == "ok" else "error"
            from ..config import now_iso

            await self.store.update(sched["id"], last_status=status, last_result=summary[:2000],
                                    last_run_at=now_iso())
            await self.notifications.create(
                agent, f"Heartbeat · {sched['name']}",
                summary if status == "ok" else f"Heartbeat failed: {summary}",
            )
            await self.events.publish("notification.new", {
                "agent": agent,
                "notification": {"title": f"Heartbeat · {sched['name']}", "body": summary[:300]}})
            return result

        await self.task_manager.launch(agent, f"Heartbeat: {sched['name']}", "heartbeat", factory)

    def _in_quiet_hours(self, sched: dict) -> bool:
        start, end = sched.get("quiet_start"), sched.get("quiet_end")
        if not start or not end:
            return False
        try:
            s, e = _to_minutes(start), _to_minutes(end)
        except (ValueError, TypeError):
            return False
        if s == e:
            return False
        now = datetime.now(timezone.utc)
        minutes = now.hour * 60 + now.minute
        if s < e:
            return s <= minutes < e
        return minutes >= s or minutes < e

    def wake(self) -> None:
        self._wake.set()


def _to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _conv_id(sched: dict) -> str:
    import uuid

    return f"heartbeat-{sched['id']}"
