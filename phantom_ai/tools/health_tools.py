"""Health Brain tools — real, private, safety-first.

Every tool operates on the encrypted Health Memory Vault. Health data is never
written to general memory and never auto-injected into prompts; it is only
returned to the model on explicit user request (privacy by design).
"""

from __future__ import annotations

from typing import Any

from .base import PermissionLevel, ToolContext, ToolError, ToolResult, ToolSpec

MEASUREMENT_METRICS = ["weight", "blood_pressure", "heart_rate", "temperature",
                       "blood_glucose", "spo2", "sleep", "steps", "activity"]
HABIT_KINDS = ["water", "meal", "exercise", "break", "other"]


def _health(ctx: ToolContext):
    if ctx.health is None:
        raise ToolError("Health brain is unavailable in this context", kind="unavailable")
    return ctx.health


def _guard_enabled(manager) -> None:
    # enabled-check done by manager callers where relevant; tools remain usable
    # for records even when scheduled routines are off.
    return None


async def _log_measurement(ctx: ToolContext, metric: str, value: float,
                           unit: str = "", note: str = "") -> ToolResult:
    manager = _health(ctx)
    if metric not in MEASUREMENT_METRICS:
        raise ToolError(f"unknown metric: {metric} (use one of {MEASUREMENT_METRICS})",
                        kind="invalid")
    record = await manager.vault.add(
        "measurement", {"metric": metric, "value": value, "unit": unit,
                        "note": note[:500]},
        title=f"{metric} {value}{unit}")
    flags = await manager.measurement_flags(metric, value, unit)
    out = f"Recorded {metric}: {value}{unit} — saved to your encrypted Health Memory."
    if flags:
        from ..health.redflags import explain_red_flag, RedFlag
        out += "\n\n" + explain_red_flag(RedFlag(**flags[0]))
        out += "\n(General guide only — a professional must interpret your reading.)"
    return ToolResult.ok(out, data={"record": record, "red_flags": flags})


async def _log_symptom(ctx: ToolContext, symptom: str, severity: int = 5,
                       duration: str = "", possible_triggers: str = "",
                       observations: str = "") -> ToolResult:
    manager = _health(ctx)
    data = {"symptom": symptom[:300], "severity": max(0, min(10, severity)),
            "duration": duration[:200], "possible_triggers": possible_triggers[:500],
            "observations": observations[:500]}
    record = await manager.vault.add("symptom", data, title=symptom[:120])
    from ..health.redflags import check_symptom

    flag = check_symptom(symptom, data["severity"])
    out = (f"Symptom recorded: {symptom} (severity {data['severity']}/10) — saved privately.\n\n"
           "I can organize and summarize your records and help you prepare questions for a "
           "healthcare professional. I do not diagnose.")
    if flag is not None:
        from ..health.redflags import explain_red_flag
        out += "\n\n" + explain_red_flag(flag)
    return ToolResult.ok(out, data={"record": record, "red_flag": flag.to_dict() if flag else None})


async def _log_habit(ctx: ToolContext, kind: str, amount: float = 0,
                     unit: str = "", note: str = "") -> ToolResult:
    manager = _health(ctx)
    if kind not in HABIT_KINDS:
        raise ToolError(f"unknown habit kind: {kind}", kind="invalid")
    record = await manager.vault.add(
        "habit", {"kind": kind, "amount": amount, "unit": unit[:50], "note": note[:300]},
        title=f"{kind} {amount}{unit}".strip())
    return ToolResult.ok(f"Logged habit: {kind} {amount}{unit}".strip(),
                         data={"record": record})


async def _log_medication(ctx: ToolContext, name: str, dose: str = "",
                          schedule: str = "", refill_date: str = "",
                          notes: str = "") -> ToolResult:
    manager = _health(ctx)
    data = {"name": name[:200], "dose": dose[:200], "schedule": schedule[:300],
            "refill_date": refill_date[:50], "notes": notes[:500]}
    record = await manager.vault.add("medication", data, title=name[:120])
    out = (f"Medication recorded: {name} ({dose or 'dose not specified'}). "
           "I can remind you per the schedule you configured. I never change doses — "
           "discuss any dose questions with your prescriber or pharmacist.")
    return ToolResult.ok(out, data={"record": record})


async def _log_goal(ctx: ToolContext, goal: str, target: str = "",
                    deadline: str = "", category: str = "general") -> ToolResult:
    manager = _health(ctx)
    data = {"goal": goal[:300], "target": target[:200], "deadline": deadline[:50],
            "category": category[:100]}
    record = await manager.vault.add("goal", data, title=goal[:120])
    return ToolResult.ok(f"Health goal saved: {goal} ({target or 'no target'})",
                         data={"record": record})


async def _log_appointment(ctx: ToolContext, title: str, when: str,
                           provider: str = "", notes: str = "") -> ToolResult:
    manager = _health(ctx)
    data = {"when": when[:100], "provider": provider[:200], "notes": notes[:500]}
    record = await manager.vault.add("appointment", data, title=title[:120])
    return ToolResult.ok(f"Appointment saved: {title} on {when}",
                         data={"record": record})


async def _daily_overview(ctx: ToolContext, part: str = "morning") -> ToolResult:
    manager = _health(ctx)
    if part == "morning":
        text = await manager.morning()
    elif part == "evening":
        text = await manager.evening()
    elif part == "day":
        t = await manager.today()
        text = (f"☀️ During the day — {t['date']}\n"
                f"💧 Water: {t['hydration']['liters']}L ({t['hydration']['pct']}% of goal)\n"
                f"🍎 Meals logged: {t['nutrition']['meals_logged']}\n"
                f"🏃 Activity sessions: {len(t['activity']['sessions'])} · "
                f"steps {t['activity']['steps']}\n"
                f"💊 Medications: {len(t['medications'])} scheduled\n"
                f"📅 Appointments: {len(t['appointments'])}")
    else:
        raise ToolError("part must be morning|day|evening", kind="invalid")
    return ToolResult.ok(text, data={"part": part, "today": await manager.today()})


async def _trends(ctx: ToolContext, metric: str, limit: int = 14) -> ToolResult:
    manager = _health(ctx)
    trend = await manager.trends(metric, limit)
    if not trend["points"]:
        return ToolResult.ok(f"No {metric} records yet.", data=trend)
    lines = [f"{metric.upper()} — last {trend['count']} readings"]
    for p in trend["points"][-10:]:
        lines.append(f"  {p['value']} {p['unit']}  ({p['when'][:16]})")
    if trend["trend_vs_previous"] is not None:
        direction = "up" if trend["trend_vs_previous"] > 0 else "down"
        lines.append(f"Trend vs previous readings: {direction} by "
                     f"{abs(trend['trend_vs_previous'])} {trend['points'][-1]['unit']}")
        lines.append("(This is a trend display, not a diagnosis.)")
    return ToolResult.ok("\n".join(lines), data=trend)


async def _search(ctx: ToolContext, query: str = "", category: str = "",
                  limit: int = 25) -> ToolResult:
    manager = _health(ctx)
    if query:
        rows = await manager.vault.search(query, limit=min(limit, 50))
    else:
        rows = await manager.vault.list(category or None, limit=min(limit, 100))
    if not rows:
        return ToolResult.ok("No health records match.", data={"records": []})
    lines = []
    for r in rows:
        snippet = str(r.get("data", {}))[:220]
        lines.append(f"[{r['category']}] {r.get('title') or r['id'][:8]} · {r['recorded_at'][:16]}\n  {snippet}")
    return ToolResult.ok("\n".join(lines), data={"records": rows})


async def _redflag(ctx: ToolContext, symptom: str = "", severity: int = 0,
                   metric: str = "", value: float = 0, unit: str = "") -> ToolResult:
    from ..health.redflags import RedFlag, check_measurement, check_symptom, explain_red_flag

    flag = None
    if symptom:
        flag = check_symptom(symptom, severity)
    elif metric:
        flag = check_measurement(metric, value, unit)
    if flag is None:
        return ToolResult.ok("No red flags detected for this entry (this is not medical advice).",
                             data={"red_flag": None})
    text = explain_red_flag(flag)
    if flag.level == "emergency":
        text += ("\n\nDo not wait for further analysis. Seek emergency care now. "
                 "I cannot assess urgency from text.")
    return ToolResult.ok(text, data={"red_flag": flag.to_dict()})


async def _briefing_section(ctx: ToolContext) -> ToolResult:
    manager = _health(ctx)
    section = await manager.briefing_section()
    return ToolResult.ok(section, data={"section": section})


async def _consult_prep(ctx: ToolContext, topic: str = "") -> ToolResult:
    manager = _health(ctx)
    rows = await manager.vault.search(topic, limit=8) if topic else \
        await manager.vault.list(limit=8)
    lines = [
        "Preparing questions for a healthcare professional — take these to your appointment:",
        "1. What are my main concerns based on what I've recorded?",
    ]
    if rows:
        lines.append("2. I have been tracking: " +
                     ", ".join(f"{r['category']} ({r.get('title')})" for r in rows[:5]) +
                     ". How should I interpret these?")
    lines += [
        "3. Are there any changes to my routine/medication I should discuss?",
        "4. What should I watch for between now and my next visit?",
        "",
        "(I prepare questions only — diagnosis and treatment decisions belong to your "
        "healthcare team.)",
    ]
    return ToolResult.ok("\n".join(lines), data={"topic": topic})


async def _study_advice(ctx: ToolContext, duration_minutes: int = 120) -> ToolResult:
    if duration_minutes < 15:
        raise ToolError("study session too short for advice", kind="invalid")
    text = (f"Study session plan (~{duration_minutes} min):\n"
            f"- Work in blocks of ~45–50 min, then a 5–10 min break.\n"
            f"- 💧 Drink water at each break.\n"
            f"- 🚶 Stand, stretch, or walk briefly between blocks.\n"
            f"- 🍎 Have a light meal/snack if the session crosses a meal time.\n"
            f"- 😴 Protect your sleep: stop intense study 30–60 min before bedtime.\n"
            "These are general wellness suggestions — adjust to what works for you.")
    return ToolResult.ok(text, data={"duration_minutes": duration_minutes})


async def _delete_record(ctx: ToolContext, record_id: str) -> ToolResult:
    manager = _health(ctx)
    ok = await manager.vault.delete(record_id)
    if not ok:
        raise ToolError(f"no health record with id {record_id}", kind="not_found")
    return ToolResult.ok(f"Deleted health record {record_id[:8]}", data={"deleted": record_id})


async def _clear_records(ctx: ToolContext, category: str = "") -> ToolResult:
    manager = _health(ctx)
    count = await manager.vault.clear(category or "")
    target = category or "ALL health records"
    return ToolResult.ok(f"Cleared {count} record(s) from {target}",
                         data={"cleared": count, "category": category})


async def _export(ctx: ToolContext) -> ToolResult:
    manager = _health(ctx)
    data = await manager.vault.export()
    return ToolResult.ok(f"Exported {data['count']} health records (JSON ready).",
                         data=data)


async def _privacy_get(ctx: ToolContext) -> ToolResult:
    manager = _health(ctx)
    privacy = await manager.privacy()
    return ToolResult.ok(
        "Health privacy settings:\n" +
        "\n".join(f"- {k}: {v}" for k, v in privacy.items()),
        data={"privacy": privacy})


async def _privacy_set(ctx: ToolContext,
                       share_with_other_brains: bool | None = None,
                       include_in_daily_briefing: bool | None = None,
                       briefing_sensitive: bool | None = None) -> ToolResult:
    manager = _health(ctx)
    kwargs = {}
    if share_with_other_brains is not None:
        kwargs["share_with_other_brains"] = share_with_other_brains
    if include_in_daily_briefing is not None:
        kwargs["include_in_daily_briefing"] = include_in_daily_briefing
    if briefing_sensitive is not None:
        kwargs["briefing_sensitive"] = briefing_sensitive
    privacy = await manager.set_privacy(**kwargs)
    return ToolResult.ok("Privacy settings updated: " + str(privacy), data={"privacy": privacy})


async def _set_routine(ctx: ToolContext, block: str,
                       items: list[str] | None = None) -> ToolResult:
    manager = _health(ctx)
    if block not in ("morning", "afternoon", "evening"):
        raise ToolError("block must be morning|afternoon|evening", kind="invalid")
    routine = await manager.set_routine(block, items or [])
    return ToolResult.ok(f"{block.capitalize()} routine updated: {', '.join(routine.get(block, []))}",
                         data={"routine": routine})


async def _schedule_routine(ctx: ToolContext, block: str, hour: int,
                            minute: int = 0) -> ToolResult:
    manager = _health(ctx)
    try:
        sched = await manager.schedule_routine(block, max(0, min(23, hour)), max(0, min(59, minute)))
    except ValueError as exc:
        raise ToolError(str(exc), kind="invalid") from exc
    return ToolResult.ok(
        f"Scheduled the {block} routine daily at {hour:02d}:{minute:02d} — "
        "notifications will come through the heartbeat (respects quiet hours).",
        data={"schedule": sched})


async def _disable(ctx: ToolContext) -> ToolResult:
    manager = _health(ctx)
    await manager.set_enabled(False, by=ctx.agent_id)
    return ToolResult.ok("Health Brain disabled. Health records stay encrypted and untouched.",
                         data={"enabled": False})


HEALTH_TOOLS = [
    "health_log_measurement", "health_log_symptom", "health_log_habit",
    "health_log_medication", "health_log_goal", "health_log_appointment",
    "health_daily_overview", "health_trends", "health_search",
    "health_redflag", "health_briefing_section", "health_consult_prep",
    "health_study_advice", "health_delete", "health_clear", "health_export",
    "health_privacy_get", "health_privacy_set", "health_set_routine",
    "health_schedule_routine", "health_disable",
    "send_notification", "delegate_to_phantom",
]


def register_health_tools(registry) -> None:
    agents = ("health",)
    registry.register(ToolSpec(
        name="health_log_measurement",
        description="Record a health measurement (weight, blood pressure, heart rate, "
                    "temperature, blood glucose, SpO2, sleep, steps). Stored encrypted; "
                    "red-flag thresholds checked automatically.",
        purpose="Track health measurements", category="health",
        parameters={"metric": {"type": "string", "enum": MEASUREMENT_METRICS, "required": True},
                    "value": {"type": "number", "required": True},
                    "unit": {"type": "string", "default": ""},
                    "note": {"type": "string", "default": ""}},
        handler=_log_measurement, permission=PermissionLevel.SAFE_ACTION, timeout=10,
        agents=agents,
    ))
    registry.register(ToolSpec(
        name="health_log_symptom",
        description="Record a symptom the user voluntarily reports (severity 0-10, duration, "
                    "triggers, observations). Safety layer checks for red flags.",
        purpose="Symptom tracking", category="health",
        parameters={"symptom": {"type": "string", "required": True},
                    "severity": {"type": "integer", "minimum": 0, "maximum": 10, "default": 5},
                    "duration": {"type": "string", "default": ""},
                    "possible_triggers": {"type": "string", "default": ""},
                    "observations": {"type": "string", "default": ""}},
        handler=_log_symptom, permission=PermissionLevel.SAFE_ACTION, timeout=10,
        agents=agents,
    ))
    registry.register(ToolSpec(
        name="health_log_habit",
        description="Log a wellness habit (water, meal, exercise, break).",
        purpose="Track daily habits", category="health",
        parameters={"kind": {"type": "string", "enum": HABIT_KINDS, "required": True},
                    "amount": {"type": "number", "default": 0},
                    "unit": {"type": "string", "default": ""},
                    "note": {"type": "string", "default": ""}},
        handler=_log_habit, permission=PermissionLevel.SAFE_ACTION, timeout=10,
        agents=agents,
    ))
    registry.register(ToolSpec(
        name="health_log_medication",
        description="Record a medication with its schedule (reminders only — never change doses).",
        purpose="Medication support", category="health",
        parameters={"name": {"type": "string", "required": True},
                    "dose": {"type": "string", "default": ""},
                    "schedule": {"type": "string", "default": ""},
                    "refill_date": {"type": "string", "default": ""},
                    "notes": {"type": "string", "default": ""}},
        handler=_log_medication, permission=PermissionLevel.SAFE_ACTION, timeout=10,
        agents=agents,
    ))
    registry.register(ToolSpec(
        name="health_log_goal",
        description="Save a user-defined health goal (e.g. 'walk 8000 steps daily').",
        purpose="Health goals", category="health",
        parameters={"goal": {"type": "string", "required": True},
                    "target": {"type": "string", "default": ""},
                    "deadline": {"type": "string", "default": ""},
                    "category": {"type": "string", "default": "general"}},
        handler=_log_goal, permission=PermissionLevel.SAFE_ACTION, timeout=10,
        agents=agents,
    ))
    registry.register(ToolSpec(
        name="health_log_appointment",
        description="Record a health-related appointment.",
        purpose="Appointments", category="health",
        parameters={"title": {"type": "string", "required": True},
                    "when": {"type": "string", "required": True},
                    "provider": {"type": "string", "default": ""},
                    "notes": {"type": "string", "default": ""}},
        handler=_log_appointment, permission=PermissionLevel.SAFE_ACTION, timeout=10,
        agents=agents,
    ))
    registry.register(ToolSpec(
        name="health_daily_overview",
        description="Generate the personalized daily health overview: morning, during-day, or "
                    "evening summary (local, private, no external model needed).",
        purpose="Daily health management", category="health",
        parameters={"part": {"type": "string", "enum": ["morning", "day", "evening"], "default": "morning"}},
        handler=_daily_overview, permission=PermissionLevel.READ_ONLY, timeout=10,
        agents=agents,
    ))
    registry.register(ToolSpec(
        name="health_trends",
        description="Show measurement trends (e.g. weight, blood pressure) — trends only, never "
                    "diagnoses.",
        purpose="Trends, not diagnoses", category="health",
        parameters={"metric": {"type": "string", "enum": MEASUREMENT_METRICS, "required": True},
                    "limit": {"type": "integer", "minimum": 2, "maximum": 90, "default": 14}},
        handler=_trends, permission=PermissionLevel.READ_ONLY, timeout=10,
        agents=agents,
    ))
    registry.register(ToolSpec(
        name="health_search",
        description="Search or list the user's health records (encrypted vault) by query or "
                    "category.",
        purpose="Health memory", category="health",
        parameters={"query": {"type": "string", "default": ""},
                    "category": {"type": "string", "enum": ["measurement", "symptom", "medication",
                                                            "appointment", "goal", "habit",
                                                            "routine", "note", ""], "default": ""},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 25}},
        handler=_search, permission=PermissionLevel.READ_ONLY, timeout=10,
        agents=agents,
    ))
    registry.register(ToolSpec(
        name="health_redflag",
        description="Safety check for a symptom or measurement. Returns a clear red-flag level "
                    "and advice; emergency findings tell the user to seek urgent care.",
        purpose="Safety / red-flag system", category="health",
        parameters={"symptom": {"type": "string", "default": ""},
                    "severity": {"type": "integer", "minimum": 0, "maximum": 10, "default": 0},
                    "metric": {"type": "string", "default": ""},
                    "value": {"type": "number", "default": 0},
                    "unit": {"type": "string", "default": ""}},
        handler=_redflag, permission=PermissionLevel.READ_ONLY, timeout=10,
        agents=agents,
    ))
    registry.register(ToolSpec(
        name="health_briefing_section",
        description="Produce the non-sensitive Health section for the Daily Briefing "
                    "(medications/appointments only if the user opted in).",
        purpose="Daily briefing integration", category="health",
        parameters={}, handler=_briefing_section, permission=PermissionLevel.READ_ONLY,
        timeout=10, agents=agents,
    ))
    registry.register(ToolSpec(
        name="health_consult_prep",
        description="Prepare a list of questions the user can take to a doctor/pharmacist "
                    "(never a diagnosis).",
        purpose="Professional consultation prep", category="health",
        parameters={"topic": {"type": "string", "default": ""}},
        handler=_consult_prep, permission=PermissionLevel.READ_ONLY, timeout=10,
        agents=agents,
    ))
    registry.register(ToolSpec(
        name="health_study_advice",
        description="Give general wellness advice for a study session (breaks, hydration, "
                    "movement, sleep).",
        purpose="Health + Study cooperation", category="health",
        parameters={"duration_minutes": {"type": "integer", "minimum": 15, "maximum": 600, "default": 120}},
        handler=_study_advice, permission=PermissionLevel.READ_ONLY, timeout=10,
        agents=agents,
    ))
    registry.register(ToolSpec(
        name="health_delete",
        description="Delete a single health record (permanent).",
        purpose="Health memory management", category="health",
        parameters={"record_id": {"type": "string", "required": True}},
        handler=_delete_record, permission=PermissionLevel.CONFIRM_REQUIRED, timeout=10,
        agents=agents,
        required_permission_notes="Deleting health records is permanent; confirmation required.",
    ))
    registry.register(ToolSpec(
        name="health_clear",
        description="Clear health records (a category or ALL). Permanent.",
        purpose="Health memory management", category="health",
        parameters={"category": {"type": "string", "enum": ["measurement", "symptom", "medication",
                                                            "appointment", "goal", "habit",
                                                            "routine", "note", ""], "default": ""}},
        handler=_clear_records, permission=PermissionLevel.HIGH_RISK, timeout=15,
        agents=agents,
        required_permission_notes="Clearing health records is irreversible — high risk.",
    ))
    registry.register(ToolSpec(
        name="health_export",
        description="Export all health records as JSON (user-initiated data export).",
        purpose="Data export", category="health",
        parameters={}, handler=_export, permission=PermissionLevel.SAFE_ACTION, timeout=15,
        agents=agents,
    ))
    registry.register(ToolSpec(
        name="health_privacy_get",
        description="Show current health privacy settings (encryption, sharing, briefing).",
        purpose="Privacy controls", category="health",
        parameters={}, handler=_privacy_get, permission=PermissionLevel.READ_ONLY, timeout=10,
        agents=agents,
    ))
    registry.register(ToolSpec(
        name="health_privacy_set",
        description="Change health privacy settings: share with other brains, include in daily "
                    "briefing, include sensitive details in the briefing.",
        purpose="Privacy controls", category="health",
        parameters={"share_with_other_brains": {"type": "boolean"},
                    "include_in_daily_briefing": {"type": "boolean"},
                    "briefing_sensitive": {"type": "boolean"}},
        handler=_privacy_set, permission=PermissionLevel.CONFIRM_REQUIRED, timeout=10,
        agents=agents,
        required_permission_notes="Changing privacy/sharing settings requires confirmation.",
    ))
    registry.register(ToolSpec(
        name="health_set_routine",
        description="Set the user's personalized wellness routine for a block "
                    "(morning/afternoon/evening).",
        purpose="Personalized routines", category="health",
        parameters={"block": {"type": "string", "enum": ["morning", "afternoon", "evening"], "required": True},
                    "items": {"type": "array", "items": {"type": "string"}}},
        handler=_set_routine, permission=PermissionLevel.SAFE_ACTION, timeout=10,
        agents=agents,
    ))
    registry.register(ToolSpec(
        name="health_schedule_routine",
        description="Turn a routine block into a daily heartbeat schedule for the Health brain "
                    "(notifications, quiet-hours aware).",
        purpose="Schedule integration", category="health",
        parameters={"block": {"type": "string", "enum": ["morning", "afternoon", "evening"], "required": True},
                    "hour": {"type": "integer", "minimum": 0, "maximum": 23, "required": True},
                    "minute": {"type": "integer", "minimum": 0, "maximum": 59, "default": 0}},
        handler=_schedule_routine, permission=PermissionLevel.SAFE_ACTION, timeout=10,
        agents=agents,
    ))
    registry.register(ToolSpec(
        name="health_disable",
        description="Disable the Health Brain. Records remain encrypted and untouched; it can "
                    "be re-enabled from Settings.",
        purpose="User control", category="health",
        parameters={}, handler=_disable, permission=PermissionLevel.CONFIRM_REQUIRED, timeout=10,
        agents=agents,
        required_permission_notes="Disabling the Health Brain requires confirmation.",
    ))
