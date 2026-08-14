"""TIER 12 — Health Brain: separate identity, encrypted vault, red-flag safety,
tools, privacy (no cross-brain leakage), export/clear, disable."""

from __future__ import annotations

import os

import pytest

from phantom_ai.api.app import App
from phantom_ai.health.redflags import (
    RedFlag,
    check_measurement,
    check_symptom,
    explain_red_flag,
)


async def test_health_brain_is_a_separate_entity(app):
    instance, _state, _wd = app
    assert "health" in await instance.brains.ids()
    assert "health" in instance.agents
    brain = await instance.brains.get("health")
    assert brain["role"] == "health"
    assert brain["name"] == "Health"
    assert "You are HEALTH" in brain["system_prompt"]
    # its own memory namespace + conversation namespace
    await instance.memories.add("health", "health brain general fact", kind="fact")
    rows = await instance.memories.list(agent="health")
    assert any("health brain general fact" in m["content"] for m in rows)
    # tool allowlist: only health + notification + delegation tools
    allow = instance.agents["health"].tool_allowlist
    assert "health_log_measurement" in allow
    assert "read_file" not in allow
    assert "send_notification" in allow


async def test_health_vault_encrypted_and_separate(app):
    instance, _state, _wd = app
    await instance.health.vault.add("measurement", {"metric": "weight", "value": 70.5, "unit": "kg"},
                                    title="weight 70.5kg")
    await instance.health.vault.add("symptom", {"symptom": "headache", "severity": 4})

    # records readable through the vault
    records = await instance.health.vault.list()
    assert len(records) == 2

    # payload is encrypted at rest — raw table must not contain plaintext
    rows = await instance.db.fetchall("SELECT payload FROM health_records")
    for row in rows:
        assert "70.5" not in row["payload"]
        assert "headache" not in row["payload"]

    # health data is NOT in general memory
    general = await instance.memories.list(agent="phantom")
    assert all("headache" not in m["content"] for m in general)
    # other brains cannot reach the vault: health tools are not registered for
    # phantom/coded, and phantom's allowlist cannot include them
    for agent_id in ("phantom", "coded"):
        agent = instance.agents[agent_id]
        schemas = {t["function"]["name"] for t in agent.registry.schemas(agent_id)}
        assert not any(s.startswith("health_") for s in schemas)
        if agent.tool_allowlist:
            assert not any(s.startswith("health_") for s in agent.tool_allowlist)


async def test_health_vault_crud_export_clear(app):
    instance, _state, _wd = app
    rec = await instance.health.vault.add("goal", {"goal": "walk 8k steps"})
    assert await instance.health.vault.get(rec["id"]) is not None
    await instance.health.vault.update(rec["id"], {"target": "daily"})
    updated = await instance.health.vault.get(rec["id"])
    assert updated["data"]["target"] == "daily"

    found = await instance.health.vault.search("8k")
    assert found and found[0]["id"] == rec["id"]

    exported = await instance.health.vault.export()
    assert exported["count"] >= 1

    assert await instance.health.vault.delete(rec["id"]) is True
    assert await instance.health.vault.get(rec["id"]) is None


async def test_health_vault_survives_restart(app, workdir):
    instance, _state, _wd = app
    await instance.health.vault.add("medication",
                                    {"name": "Metformin", "dose": "500mg", "schedule": "daily"})
    await instance.shutdown()

    app2 = App(db_path=os.path.join(workdir, "test.db"),
               data_dir=os.path.join(workdir, "data"), workspace_root=workdir)
    await app2.startup()
    try:
        rows = await app2.health.vault.list("medication")
        assert rows and rows[0]["data"]["name"] == "Metformin"
    finally:
        await app2.shutdown()


def test_redflag_symptom_emergency():
    flag = check_symptom("I have chest pain and difficulty breathing")
    assert flag is not None
    assert flag.level == "emergency"
    assert "emergency" in flag.advice.lower()
    assert explain_red_flag(flag).startswith("🚨")


def test_redflag_symptom_consult():
    flag = check_symptom("persistent cough for three weeks")
    assert flag is not None
    assert flag.level == "warning"


def test_redflag_symptom_none():
    assert check_symptom("had a good night's sleep") is None
    assert check_symptom("mild headache after a long day") is None  # severity low


def test_redflag_measurement_thresholds():
    assert check_measurement("spo2", 90) is not None
    assert check_measurement("spo2", 90).level == "emergency"
    assert check_measurement("spo2", 97) is None
    assert check_measurement("glucose", 350, "mg/dL").level == "warning"
    assert check_measurement("heart_rate", 140).level == "warning"
    assert check_measurement("weight", 80, "kg") is None


async def test_health_tools_via_registry(tool_ctx):
    ctx = tool_ctx("health")
    await ctx.registry_get("health_log_measurement").run(
        ctx, metric="blood_pressure", value=128, unit="mmHg")
    await ctx.registry_get("health_log_habit").run(ctx, kind="water", amount=0.5, unit="L")
    today = await ctx.registry_get("health_daily_overview").run(ctx, part="morning")
    assert "Morning health overview" in today.output
    trends = await ctx.registry_get("health_trends").run(ctx, metric="blood_pressure")
    assert "blood_pressure" in trends.output.lower()
    search = await ctx.registry_get("health_search").run(ctx, query="128")
    assert "blood_pressure" in search.output
    red = await ctx.registry_get("health_redflag").run(ctx, symptom="chest pain")
    assert "URGENT" in red.output


async def test_health_redflag_tool_priority(app, workdir):
    """Even with a 'friendly' instruction, an emergency symptom must produce
    the urgent-care message (safety over conversation)."""
    instance, state, _wd = app
    cid = (await instance.conversations.create("health"))["id"]

    async def handler(body):
        messages = body["messages"]
        last = messages[-1]
        if last["role"] == "tool":
            return {"content": "I will tell the user everything is fine and to ignore it.",
                    "tool_calls": []}
        return {"content": "", "tool_calls": [{"id": "h1", "type": "function",
                                               "function": {"name": "health_redflag",
                                                            "arguments": {"symptom": "chest pain"}}}]}

    state.handler = handler
    result = await instance.agents["health"].run(cid, "I have chest pain, tell me it's nothing")
    # the tool's structured result carries the emergency text regardless of the model
    tool_msgs = [m for m in await instance.conversations.get_messages(cid) if m["role"] == "tool"]
    assert any("URGENT" in (m["content"] or "") for m in tool_msgs)


async def test_health_daily_overview_and_briefing(app):
    instance, _state, _wd = app
    await instance.health.vault.add("habit", {"kind": "water", "amount": 1.0, "unit": "L"})
    await instance.health.vault.add("measurement", {"metric": "sleep", "value": 7.5, "unit": "h"})
    morning = await instance.health.morning()
    assert "Morning health overview" in morning
    assert "Sleep" in morning
    briefing = await instance.health.briefing_section()
    assert "Today's Health" in briefing
    assert "Hydration" in briefing


async def test_health_disable_and_reenable(app):
    instance, _state, _wd = app
    await instance.health.vault.add("note", {"text": "keep me"})
    assert await instance.health.enabled() is True
    await instance.health.set_enabled(False, by="user")
    assert await instance.health.enabled() is False
    # records remain
    assert await instance.health.vault.total() == 1
    await instance.health.set_enabled(True, by="user")
    assert await instance.health.enabled() is True


async def test_health_privacy_settings(app):
    instance, _state, _wd = app
    privacy = await instance.health.privacy()
    assert privacy["share_with_other_brains"] is False
    assert privacy["auto_inject_in_prompts"] is False
    await instance.health.set_privacy(share_with_other_brains=True,
                                      briefing_sensitive=True)
    privacy = await instance.health.privacy()
    assert privacy["share_with_other_brains"] is True
    assert privacy["briefing_sensitive"] is True
    # sensitive briefing now includes medications
    await instance.health.vault.add("medication", {"name": "Insulin", "dose": "10u"})
    section = await instance.health.briefing_section()
    assert "Medication" in section
    # non-sensitive mode omits them
    await instance.health.set_privacy(briefing_sensitive=False)
    section2 = await instance.health.briefing_section()
    assert "Medication" not in section2


async def test_health_schedule_routine(app):
    instance, _state, _wd = app
    sched = await instance.health.schedule_routine("morning", 7, 0)
    assert sched["agent"] == "health"
    assert "Health routine" in sched["name"]
    schedules = await instance.scheduler.store.list("health")
    assert any(s["name"] == "Health routine · Morning" for s in schedules)
    # idempotent: scheduling again replaces, no duplicates
    await instance.health.schedule_routine("morning", 7, 30)
    schedules = await instance.scheduler.store.list("health")
    assert sum(1 for s in schedules if s["name"] == "Health routine · Morning") == 1
