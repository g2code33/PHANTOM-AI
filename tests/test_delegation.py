"""TIER 3 — agent-to-agent delegation: task ids, structured results, distinct
identities, timeouts, loop prevention."""

from __future__ import annotations

import asyncio


async def _new_conv(app, agent="phantom"):
    conv = await app.conversations.create(agent)
    return conv["id"]


async def test_phantom_delegates_to_coded(app):
    instance, state, _wd = app
    cid = await _new_conv(instance)

    async def handler(body):
        messages = body["messages"]
        system = messages[0]["content"] if messages else ""
        last = messages[-1]
        if "You are CODED" in system:
            # Coded receives the delegated task and answers directly
            return {"content": "STRUCTURED REPORT: inspected project; bug is a missing import "
                               "in `main.py`. Fixed and verified.", "tool_calls": []}
        if last["role"] == "tool":
            return {"content": "Coded investigated it. The bug was a missing import, now fixed.",
                    "tool_calls": []}
        return {"content": "", "tool_calls": [{"id": "del1", "type": "function",
                                               "function": {"name": "delegate_to_coded",
                                                            "arguments": {
                                                                "objective": "Investigate why the website is broken",
                                                                "context": "Project at ~/webapp, build failing"}}}]}

    state.handler = handler
    result = await instance.agents["phantom"].run(cid, "Phantom, investigate why my website is broken.")
    assert result.status == "ok"
    assert "missing import" in result.content
    assert result.tool_calls_made == 1

    delegations = await instance.delegations.store.list(origin="phantom", target="coded")
    assert delegations, "delegation record missing"
    d = delegations[0]
    assert d["status"] == "completed"
    assert d["task_id"]
    assert d["objective"].startswith("Investigate")
    assert "STRUCTURED REPORT" in (d["result"] or {}).get("summary", "")
    # Coded used its own conversation namespace
    coded_convs = await instance.conversations.list_for_agent("coded")
    assert any("Delegation" in c["title"] for c in coded_convs)


async def test_delegation_timeout(app):
    instance, state, _wd = app
    cid = await _new_conv(instance)

    async def handler(body):
        system = body["messages"][0]["content"]
        if "You are CODED" in system:
            await asyncio.sleep(60)
            return {"content": "never", "tool_calls": []}
        return {"content": "", "tool_calls": [{"id": "del_slow", "type": "function",
                                               "function": {"name": "delegate_to_coded",
                                                            "arguments": {"objective": "slow report",
                                                                          "timeout_sec": 10}}}]}

    state.handler = handler
    result = await instance.agents["phantom"].run(cid, "ask coded for a slow report")
    # the delegate tool raises ToolError with the timed-out status
    assert result.status == "ok"
    delegations = await instance.delegations.store.list(origin="phantom")
    assert delegations
    assert delegations[0]["status"] in ("timed_out", "failed")


async def test_delegation_loop_guard(app):
    instance, state, _wd = app
    await instance.settings.set("delegation.max_loops", 1)
    # seed two recent delegations between the pair
    for _ in range(2):
        await instance.delegations.store.create("phantom", "coded",
                                                "seed", "seed", 60)
    result = await instance.delegations.delegate(
        origin="phantom", target="coded", objective="loop me", timeout_sec=30)
    assert result["status"] == "blocked"
    assert "loop guard" in result["error"]


async def test_coded_delegates_to_phantom(app):
    instance, state, _wd = app
    cid = await _new_conv(instance, agent="coded")

    async def handler(body):
        messages = body["messages"]
        system = messages[0]["content"] if messages else ""
        last = messages[-1]
        if "You are PHANTOM" in system:
            return {"content": "RESEARCH SUMMARY: market analysis completed, 3 sources found.",
                    "tool_calls": []}
        if last["role"] == "tool":
            return {"content": "Phantom finished the research.", "tool_calls": []}
        return {"content": "", "tool_calls": [{"id": "d2", "type": "function",
                                               "function": {"name": "delegate_to_phantom",
                                                            "arguments": {"objective": "Research competitor pricing"}}}]}

    state.handler = handler
    result = await instance.agents["coded"].run(cid, "get phantom to research pricing")
    assert result.status == "ok"
    delegations = await instance.delegations.store.list(origin="coded", target="phantom")
    assert delegations and delegations[0]["status"] == "completed"
