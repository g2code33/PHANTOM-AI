"""TIER 6/9 — kill switch: blocks new runs, cancels in-flight runs, refuses
tool execution, and the app remains usable."""

from __future__ import annotations

import asyncio

from phantom_ai.tools.base import ToolError


async def test_engage_blocks_new_runs(app):
    instance, _state, _wd = app
    await instance.killswitch.engage("emergency test")
    conv = await instance.conversations.create("phantom")
    result = await instance.agents["phantom"].run(conv["id"], "hello?")
    assert result.status == "cancelled"
    assert "kill switch" in result.error


async def test_engage_cancels_inflight_run(app):
    instance, state, _wd = app
    conv = await instance.conversations.create("phantom")

    async def handler(body):
        await asyncio.sleep(30)
        return {"content": "slow", "tool_calls": []}

    state.handler = handler
    run_task = asyncio.create_task(instance.agents["phantom"].run(conv["id"], "slow"))
    await asyncio.sleep(0.2)
    assert instance.agents["phantom"].active_runs()
    await instance.killswitch.engage("user pressed the button")
    result = await asyncio.wait_for(run_task, timeout=15)
    assert result.status == "cancelled"


async def test_killswitch_blocks_tool_execution(app):
    instance, _state, _wd = app
    await instance.killswitch.engage("test")
    with pytest_raises_toolerror():
        await instance.permissions.authorize("phantom", "read_file", {"path": "~"},
                                             "c", "s", interactive=True)


async def test_disengage_restores_function(app):
    instance, _state, _wd = app
    await instance.killswitch.engage("test")
    await instance.killswitch.disengage()
    assert not instance.killswitch.is_engaged()
    conv = await instance.conversations.create("phantom")
    # offline provider is used (no handler needed): run should not be cancelled
    result = await instance.agents["phantom"].run(conv["id"], "help")
    assert result.status == "ok"
    audit = await instance.audit.query(event="killswitch.engaged")
    assert audit


def pytest_raises_toolerror():
    import pytest

    return pytest.raises(ToolError)
