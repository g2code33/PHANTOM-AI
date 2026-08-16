"""Regression: agent runs must never crash on malformed persisted tool_calls.

The 'phantom not responding' report traced to
TypeError: 'NoneType' object is not iterable in _parse_tool_calls — a stored
assistant message had tool_calls = "null" (valid JSON → None) and the list
comprehension iterated it. This guards all malformed shapes.
"""

from __future__ import annotations

import json

from phantom_ai.agents.core import _parse_tool_calls


def test_tool_calls_none_raw():
    assert _parse_tool_calls(None) is None


def test_tool_calls_empty_string():
    assert _parse_tool_calls("") is None


def test_tool_calls_json_null():
    # the crash: json.loads("null") -> None, then `for t in None`
    assert _parse_tool_calls("null") is None


def test_tool_calls_json_empty_array():
    assert _parse_tool_calls("[]") == []


def test_tool_calls_json_object_not_list():
    # a dict should not crash either
    assert _parse_tool_calls('{"tool_call_id": "x"}') is None


def test_tool_calls_valid_list():
    calls = _parse_tool_calls(json.dumps([
        {"id": "1", "name": "web_search", "arguments": {"q": "hi"}},
        {"id": "2", "name": "search_memory", "arguments": {}},
        "not_a_dict",  # non-dict entries are skipped safely
    ]))
    assert calls is not None
    assert len(calls) == 2
    assert calls[0].name == "web_search"
    assert calls[0].arguments == {"q": "hi"}
    assert calls[1].name == "search_memory"


async def test_history_messages_survives_null_tool_calls(app):
    """Full path: a conversation with an assistant msg stored as 'null' tool
    calls must load history without raising (the 'phantom not responding'
    crash)."""
    instance, _state, _wd = app
    conv = await instance.conversations.create("phantom", title="crash-test")
    cid = conv["id"]
    # messages table: uid TEXT unique, msg_index INT, agent TEXT, session_id
    await instance.db.execute(
        "INSERT INTO messages (uid, conversation_id, agent, msg_index, role,"
        " content, tool_calls, tool_results, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        ("m1", cid, "phantom", 0, "assistant", "hi there", "null", "",
         "2026-08-16T00:00:00Z"))
    await instance.db.execute(
        "INSERT INTO messages (uid, conversation_id, agent, msg_index, role,"
        " content, tool_calls, tool_results, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        ("m2", cid, "phantom", 1, "user", "hello", "", "",
         "2026-08-16T00:00:01Z"))

    agent = instance.agents["phantom"]
    history = await agent._history_messages(cid, window=50, skip_uid="m2")
    # must not raise; the assistant message loads with no tool calls
    assert any(getattr(m, "content", "") == "hi there" for m in history)
