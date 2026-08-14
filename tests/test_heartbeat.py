"""TIER 9 — heartbeat: expression parsing, scheduling, quiet hours,
notifications, catch-up, kill-switch integration."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from phantom_ai.heartbeat.scheduler import HeartbeatScheduler, next_run_at, parse_expression


def test_parse_expressions():
    assert parse_expression("every 30m")["every_sec"] == 1800
    assert parse_expression("every 2h")["every_sec"] == 7200
    assert parse_expression("hourly")["every_sec"] == 3600
    assert parse_expression("daily at 09:30")["kind"] == "daily"
    assert parse_expression("at 18:05")["kind"] == "daily"
    cron = parse_expression("*/10 * * * *")
    assert cron["kind"] == "cron" and cron["minute"] == "*/10"
    with pytest.raises(ValueError):
        parse_expression("random nonsense")


def test_next_run_at_interval():
    base = datetime(2026, 8, 14, 10, 0, 0, tzinfo=timezone.utc)
    nxt = next_run_at("every 30m", base)
    assert datetime.fromisoformat(nxt) == base + timedelta(minutes=30)


def test_next_run_at_daily_rolls_over():
    base = datetime(2026, 8, 14, 23, 0, 0, tzinfo=timezone.utc)
    nxt = next_run_at("daily at 09:00", base)
    assert datetime.fromisoformat(nxt).hour == 9
    assert datetime.fromisoformat(nxt).day == 15


async def test_tick_runs_due_schedule_and_creates_notification(app):
    instance, _state, _wd = app
    runner_calls = []

    async def fake_runner(**kwargs):
        runner_calls.append(kwargs)
        return {"status": "ok", "content": "system healthy"}

    sched = await instance.scheduler.store.create(
        "phantom", "Health check", "every 5m", "check disk space",
        quiet_start=None, quiet_end=None)
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    sched = await instance.scheduler.store.update(sched["id"], next_run_at=past)

    scheduler = HeartbeatScheduler(
        store=instance.scheduler.store, settings=instance.settings,
        task_manager=instance.tasks, agent_runner=fake_runner,
        events=instance.events, killswitch=instance.killswitch,
        notification_store=instance.notifications,
    )
    await scheduler._tick()
    await asyncio.sleep(0.2)  # let the background task finish

    notifs = await instance.notifications.list()
    assert any("Health check" in n["title"] for n in notifs)
    updated = await instance.scheduler.store.get(sched["id"])
    assert updated["last_status"] == "ok"
    assert updated["next_run_at"] is not None
    # the runner was invoked with mode=proactive
    assert runner_calls and runner_calls[0]["mode"] == "proactive"


async def test_quiet_hours_defer(app):
    instance, _state, _wd = app
    now = datetime.now(timezone.utc)
    # quiet hours covering the current minute
    start = f"{now.hour:02d}:{now.minute:02d}"
    end = f"{(now + timedelta(minutes=1)).hour:02d}:{(now + timedelta(minutes=1)).minute:02d}"
    sched = {"quiet_start": start, "quiet_end": end}
    scheduler = HeartbeatScheduler(
        store=instance.scheduler.store, settings=instance.settings,
        task_manager=instance.tasks, agent_runner=lambda **kw: {},
        events=instance.events, killswitch=instance.killswitch,
        notification_store=instance.notifications)
    assert scheduler._in_quiet_hours(sched) is True
    # non-overlapping quiet hours → False
    sched2 = {"quiet_start": "00:00", "quiet_end": "00:01"}
    assert scheduler._in_quiet_hours(sched2) is False


async def test_killswitch_stops_heartbeat(app):
    instance, _state, _wd = app
    await instance.killswitch.engage("test")
    sched = await instance.scheduler.store.create("phantom", "X", "every 5m", "do stuff")
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    await instance.scheduler.store.update(sched["id"], next_run_at=past)
    scheduler = HeartbeatScheduler(
        store=instance.scheduler.store, settings=instance.settings,
        task_manager=instance.tasks, agent_runner=lambda **kw: {"status": "ok"},
        events=instance.events, killswitch=instance.killswitch,
        notification_store=instance.notifications)
    await scheduler._tick()
    # nothing ran: schedule untouched
    updated = await instance.scheduler.store.get(sched["id"])
    assert updated["last_status"] is None
    await instance.killswitch.disengage()
