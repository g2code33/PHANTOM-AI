"""TIERS 1–2 — agent loop: chat, tool round-trips, per-agent identity/keys,
confirmation gating, blocked commands, memory tools, error recovery, text
protocol fallback."""

from __future__ import annotations

import asyncio
import os

import pytest


async def _new_conv(app, agent="phantom"):
    conv = await app.conversations.create(agent)
    return conv["id"]


async def test_phantom_and_coded_have_distinct_providers_and_keys(app):
    instance, _state, _wd = app
    assert instance.providers["phantom"].api_key == "nvapi-test-phantom-key-0000000000"
    assert instance.providers["coded"].api_key == "nvapi-test-coded-key-0000000000"
    assert instance.agents["phantom"].meta["display_name"] == "Phantom"
    assert instance.agents["coded"].meta["display_name"] == "Coded"
    assert "PHANTOM" in instance.agents["phantom"].meta["system_prompt"]
    assert "CODED" in instance.agents["coded"].meta["system_prompt"]
    assert "PHANTOM" not in instance.agents["coded"].meta["system_prompt"].split("IDENTITY")[1].split("\n\n")[0]


async def test_simple_chat_roundtrip(app):
    instance, state, _wd = app
    cid = await _new_conv(instance)

    async def handler(body):
        return {"content": "Hello! I am Phantom.", "tool_calls": []}

    state.handler = handler
    result = await instance.agents["phantom"].run(cid, "hello there", session_id="t1")
    assert result.status == "ok"
    assert result.content == "Hello! I am Phantom."
    # conversation archived
    msgs = await instance.conversations.get_messages(cid)
    assert msgs[0]["role"] == "user" and msgs[0]["content"] == "hello there"
    assert msgs[1]["role"] == "assistant"


async def test_tool_call_roundtrip_reads_real_file(app, workdir):
    instance, state, _wd = app
    target = os.path.join(workdir, "notes.txt")
    with open(target, "w") as fh:
        fh.write("the secret answer is 42")

    cid = await _new_conv(instance)

    async def handler(body):
        messages = body["messages"]
        if messages[-1]["role"] == "tool":
            return {"content": "I read the file. The answer is 42.", "tool_calls": []}
        return {"content": "", "tool_calls": [{"id": "call_1", "type": "function",
                                               "function": {"name": "read_file",
                                                            "arguments": {"path": target}}}]}

    state.handler = handler
    result = await instance.agents["phantom"].run(cid, "read notes.txt and tell me the answer")
    assert result.status == "ok"
    assert "42" in result.content
    assert result.tool_calls_made == 1
    # tool activity archived
    msgs = await instance.conversations.get_messages(cid)
    roles = [m["role"] for m in msgs]
    assert roles == ["user", "assistant", "tool", "assistant"]
    audit = await instance.audit.query(agent="phantom", event="tool.executed")
    assert any(a["detail"].get("tool") == "read_file" for a in audit)


async def test_confirmation_gate_approve_then_executes(app, workdir):
    instance, state, _wd = app
    target = os.path.join(workdir, "victim.txt")
    with open(target, "w") as fh:
        fh.write("do not delete me")

    cid = await _new_conv(instance)

    async def handler(body):
        messages = body["messages"]
        if messages[-1]["role"] == "tool":
            return {"content": "Deleted as you wished.", "tool_calls": []}
        return {"content": "", "tool_calls": [{"id": "call_d", "type": "function",
                                               "function": {"name": "delete_file",
                                                            "arguments": {"path": target}}}]}

    state.handler = handler
    run_task = asyncio.create_task(
        instance.agents["phantom"].run(cid, "delete victim.txt"))

    # wait for the confirmation request
    conf = None
    for _ in range(100):
        pending = await instance.confirmations.pending_for_agent("phantom")
        if pending:
            conf = pending[0]
            break
        await asyncio.sleep(0.02)
    assert conf is not None, "confirmation was not requested"
    assert conf["tool_name"] == "delete_file"
    assert "victim.txt" in conf["impact"]

    await instance.confirmations.decide(conf["id"], True)
    result = await asyncio.wait_for(run_task, timeout=30)
    assert result.status == "ok"
    assert not os.path.exists(target), "file should have been deleted after approval"
    audit = await instance.audit.query(event="confirmation.approved")
    assert audit


async def test_confirmation_gate_deny_blocks_execution(app, workdir):
    instance, state, _wd = app
    target = os.path.join(workdir, "keep.txt")
    with open(target, "w") as fh:
        fh.write("precious")

    cid = await _new_conv(instance)

    async def handler(body):
        messages = body["messages"]
        if messages[-1]["role"] == "tool":
            return {"content": "Understood, I did not delete it.", "tool_calls": []}
        return {"content": "", "tool_calls": [{"id": "call_x", "type": "function",
                                               "function": {"name": "delete_file",
                                                            "arguments": {"path": target}}}]}

    state.handler = handler
    run_task = asyncio.create_task(
        instance.agents["phantom"].run(cid, "delete keep.txt"))

    conf = None
    for _ in range(100):
        pending = await instance.confirmations.pending_for_agent("phantom")
        if pending:
            conf = pending[0]
            break
        await asyncio.sleep(0.02)
    assert conf is not None

    await instance.confirmations.decide(conf["id"], False)
    result = await asyncio.wait_for(run_task, timeout=30)
    assert result.status == "ok"
    assert os.path.exists(target), "file must NOT be deleted after denial"
    audit = await instance.audit.query(event="confirmation.denied")
    assert audit


async def test_blocked_command_never_executes(app):
    instance, state, _wd = app
    cid = await _new_conv(instance)

    async def handler(body):
        messages = body["messages"]
        if messages[-1]["role"] == "tool":
            content = messages[-1]["content"] or ""
            return {"content": "The command was blocked by policy." if "blocked" in content
                    else "I ran it.", "tool_calls": []}
        return {"content": "", "tool_calls": [{"id": "c1", "type": "function",
                                               "function": {"name": "run_command",
                                                            "arguments": {"command": "rm -rf /"}}}]}

    state.handler = handler
    result = await instance.agents["phantom"].run(cid, "wipe the disk")
    assert result.status == "ok"
    assert "blocked" in result.content
    audit = await instance.audit.query(event="permission.denied")
    assert audit


async def test_remember_tool_stores_memory(app):
    instance, state, _wd = app
    cid = await _new_conv(instance)

    async def handler(body):
        messages = body["messages"]
        if messages[-1]["role"] == "tool":
            return {"content": "Stored.", "tool_calls": []}
        return {"content": "", "tool_calls": [{"id": "m1", "type": "function",
                                               "function": {"name": "remember",
                                                            "arguments": {"content": "User prefers dark mode",
                                                                          "kind": "preference",
                                                                          "importance": 0.9}}}]}

    state.handler = handler
    result = await instance.agents["phantom"].run(cid, "remember I like dark mode")
    assert result.status == "ok"
    memories = await instance.memories.list(agent="phantom")
    assert any("dark mode" in m["content"] for m in memories)
    # Coded's memory namespace must NOT contain it
    coded_memories = await instance.memories.list(agent="coded")
    assert all("dark mode" not in m["content"] for m in coded_memories)


async def test_memory_package_injected_into_context(app):
    instance, state, _wd = app
    await instance.memories.add("phantom", "The Code Rx backend uses FastAPI and PostgreSQL",
                                kind="project", importance=0.9)
    cid = await _new_conv(instance)

    seen_package = {}

    async def handler(body):
        system_msg = body["messages"][0]["content"]
        seen_package["has"] = "RETRIEVED CONTEXT" in system_msg
        seen_package["has_fact"] = "FastAPI" in system_msg
        return {"content": "ok", "tool_calls": []}

    state.handler = handler
    await instance.agents["phantom"].run(cid, "Tell me about the Code Rx backend project.")
    assert seen_package.get("has") is True
    assert seen_package.get("has_fact") is True


async def test_provider_error_surfaces_and_conversation_survives(app):
    instance, state, _wd = app
    cid = await _new_conv(instance)

    async def handler(body):
        return 500, {"error": {"message": "boom"}}

    state.handler = handler
    result = await instance.agents["phantom"].run(cid, "this will fail")
    assert result.status == "error"
    assert "error" in result.error.lower() or "provider" in result.error.lower()
    # conversation still stored
    msgs = await instance.conversations.get_messages(cid)
    assert msgs[0]["role"] == "user"


async def test_text_protocol_fallback(app, workdir):
    """When the API refuses `tools`, the agent falls back to <tool_call> JSON
    protocol and still executes real tools."""
    instance, state, _wd = app
    target = os.path.join(workdir, "hello.txt")
    with open(target, "w") as fh:
        fh.write("greetings from file")

    cid = await _new_conv(instance)
    calls = {"n": 0}

    async def handler(body):
        messages = body["messages"]
        if "tools" in body and calls["n"] == 0:
            return 400, {"error": {"message": "this model does not support tools"}}
        calls["n"] += 1
        if (messages[-1].get("content") or "").startswith("[tool result"):
            return {"content": "I read it via text protocol.", "tool_calls": []}
        # text-protocol tool call inside content
        return {"content": '<tool_call>{"name": "read_file", "arguments": {"path": "%s"}}</tool_call>' % target,
                "tool_calls": []}

    state.handler = handler
    result = await instance.agents["phantom"].run(cid, "read hello.txt")
    assert result.status == "ok"
    assert result.tool_calls_made == 1
    assert "text protocol" in result.content


async def test_cancellation(app):
    instance, state, _wd = app
    cid = await _new_conv(instance)
    started = asyncio.Event()

    async def handler(body):
        started.set()
        await asyncio.sleep(30)
        return {"content": "late", "tool_calls": []}

    state.handler = handler
    run_task = asyncio.create_task(instance.agents["phantom"].run(cid, "slow request"))
    await asyncio.wait_for(started.wait(), timeout=10)
    assert instance.agents["phantom"].active_runs()
    run_id = instance.agents["phantom"].active_runs()[0]
    instance.agents["phantom"].cancel(run_id)
    result = await asyncio.wait_for(run_task, timeout=10)
    assert result.status == "cancelled"


def test_casual_gate_detects_small_talk():
    """Greetings/small talk must be flagged so tools are skipped."""
    from phantom_ai.agents.core import Agent

    agent = Agent.__new__(Agent)  # no __init__ — we only test the helper
    assert agent._is_casual("hi") is True
    assert agent._is_casual("how are you?") is True
    assert agent._is_casual("Hello there!") is True
    assert agent._is_casual("thanks") is True
    assert agent._is_casual("what's up") is True
    # real requests are NOT casual
    assert agent._is_casual("open firefox") is False
    assert agent._is_casual("check my cpu usage") is False
    assert agent._is_casual("write a python script that sorts a list") is False
    assert agent._is_casual("tell me about the weather in Accra") is False
