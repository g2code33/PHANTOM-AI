"""TIER 6 — permissions & safety: levels, overrides, command policy,
confirmation flow, secret guard, audit trail."""

from __future__ import annotations

import asyncio
import os

import pytest

from phantom_ai.permissions.validation import PermissionLevel as VL, validate_command
from phantom_ai.tools.base import PermissionLevel, ToolError


async def test_effective_levels_default(app):
    instance, _state, _wd = app
    level, source, _ = await instance.permissions.effective_level("phantom", "read_file", {})
    assert level == PermissionLevel.READ_ONLY
    level2, _, _ = await instance.permissions.effective_level("phantom", "delete_file", {})
    assert level2 == PermissionLevel.CONFIRM_REQUIRED


async def test_per_agent_override(app):
    instance, _state, _wd = app
    await instance.settings.set("perm:delete_file", "safe_action", "phantom")
    level, source, _ = await instance.permissions.effective_level("phantom", "delete_file", {})
    assert level == PermissionLevel.SAFE_ACTION
    assert source == "override(phantom)"
    # coded unaffected
    level2, _, _ = await instance.permissions.effective_level("coded", "delete_file", {})
    assert level2 == PermissionLevel.CONFIRM_REQUIRED


async def test_blocked_tool_raises(app):
    instance, _state, _wd = app
    await instance.settings.set("perm:write_file", "blocked", "coded")
    with pytest.raises(ToolError) as ei:
        await instance.permissions.authorize("coded", "write_file", {"path": "/tmp/x"},
                                             "c", "s", interactive=True)
    assert ei.value.kind == "permission"
    denied = await instance.audit.query(event="permission.denied")
    assert denied and denied[0]["detail"]["tool"] == "write_file"


async def test_secret_guard_blocks_credential_lookalikes(app):
    instance, _state, _wd = app
    with pytest.raises(ToolError) as ei:
        await instance.permissions.authorize(
            "phantom", "write_file",
            {"path": "~/.env", "content": "API_KEY=nvapi-abcdefghijklmnop1234567890"},
            "c", "s", interactive=True)
    assert ei.value.kind == "permission"


def test_command_policy_classification():
    assert validate_command("ls -la").level == VL.SAFE_ACTION
    assert validate_command("cat /etc/hostname").level == VL.SAFE_ACTION
    assert validate_command("rm old-file.zip").level == VL.CONFIRM_REQUIRED
    assert validate_command("apt install nginx").level == VL.CONFIRM_REQUIRED
    assert validate_command("git push origin main").level == VL.CONFIRM_REQUIRED
    assert validate_command("rm -rf /").level == VL.BLOCKED
    assert validate_command("mkfs.ext4 /dev/sdb").level == VL.BLOCKED
    assert validate_command("dd if=/dev/zero of=/dev/sda").level == VL.BLOCKED
    assert validate_command("shutdown -h now").level == VL.BLOCKED
    assert validate_command("echo x > /etc/hosts").level == VL.BLOCKED
    assert validate_command("echo x > notes.txt").level == VL.SAFE_ACTION


async def test_command_policy_flows_through_permission_manager(app):
    instance, _state, _wd = app
    # blocked command → ToolError even though run_command default is safe
    with pytest.raises(ToolError) as ei:
        await instance.permissions.authorize(
            "phantom", "run_command", {"command": "rm -rf /"}, "c", "s", interactive=True)
    assert ei.value.kind == "permission"
    # mutating command → requires a confirmation; expiry → denied, not executed
    try:
        await instance.permissions.authorize(
            "phantom", "run_command", {"command": "rm notes.txt"}, "c", "s",
            interactive=False, confirm_timeout=0.001)
        assert False, "should have raised on expiry"
    except ToolError as ei:
        assert "expired" in ei.message


async def test_approval_is_scoped_to_single_action(app, workdir):
    """Approving one delete must NOT auto-approve a second delete."""
    instance, _state, _wd = app
    f1 = os.path.join(workdir, "a.txt")
    f2 = os.path.join(workdir, "b.txt")
    for f in (f1, f2):
        with open(f, "w") as fh:
            fh.write("x")

    # approve the first delete
    auth_task = asyncio.create_task(instance.permissions.authorize(
        "phantom", "delete_file", {"path": f1}, "c", "s", interactive=True))
    for _ in range(100):
        pending = await instance.confirmations.pending_for_agent("phantom")
        if pending:
            break
        await asyncio.sleep(0.02)
    await instance.confirmations.decide(pending[0]["id"], True)
    decision = await asyncio.wait_for(auth_task, timeout=10)
    assert decision.source == "confirmed"
    assert os.path.exists(f1)  # authorize only approves; the tool still runs it

    # the second delete must ask AGAIN (and here expire → denied)
    with pytest.raises(ToolError):
        await instance.permissions.authorize(
            "phantom", "delete_file", {"path": f2}, "c", "s",
            interactive=False, confirm_timeout=0.001)
    assert os.path.exists(f2)
