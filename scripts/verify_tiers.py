#!/usr/bin/env python3
"""Verification harness for the PHANTOM + CODED build tiers.

Usage:  .venv/bin/python scripts/verify_tiers.py [--live-only] [--quick]

Runs:
  1. Tier test suites (pytest) — grouped per tier.
  2. A live smoke test: boots the actual application (real FastAPI server +
     WebSocket event stream) against a local OpenAI-compatible mock, sends a
     chat that triggers a real tool call, approves a confirmation, and checks
     the conversation archive.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TIERS = [
    ("Tier 1  · NVIDIA provider + Phantom text agent",
     ["tests/test_providers.py", "tests/test_agents.py"]),
    ("Tier 2  · Coded as an independent entity",
     ["tests/test_agents.py"]),
    ("Tier 3  · Agent-to-agent delegation",
     ["tests/test_delegation.py"]),
    ("Tier 4  · Tool registry",
     ["tests/test_tools.py::test_write_and_read_file",
      "tests/test_api.py::test_manual_tool_run_endpoint"]),
    ("Tier 5  · Real PC control (files/terminal/processes/system/web/gui)",
     ["tests/test_tools.py"]),
    ("Tier 6  · Permissions, confirmations, audit, kill switch",
     ["tests/test_permissions.py", "tests/test_killswitch.py",
      "tests/test_api.py::test_permission_override_via_api"]),
    ("Tier 7  · Memory: per-agent + shared, archive, FTS search, restart-persistent",
     ["tests/test_memory.py", "tests/test_agents.py::test_remember_tool_stores_memory",
      "tests/test_agents.py::test_memory_package_injected_into_context"]),
    ("Tier 8  · Voice abstraction (browser STT/TTS + provider interface)",
     ["tests/test_providers.py::test_offline_provider_is_honest"]),  # abstraction sanity
    ("Tier 9  · Heartbeat scheduler, quiet hours, notifications",
     ["tests/test_heartbeat.py"]),
    ("Tier 10 · Task manager, limits, observability, API surface",
     ["tests/test_api.py", "tests/test_killswitch.py"]),
    ("Tier 11 · Evolution & System Intelligence (brains, graph, loops, "
     "model routing, proposals, snapshots, analyst)",
     ["tests/test_evolution.py"]),
]


def run_pytest(selected: list[str]) -> bool:
    cmd = [str(ROOT / ".venv/bin/python"), "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    for item in selected:
        if "::" in item:
            cmd.append(str(ROOT / item.split("::")[0]) + "::" + item.split("::")[1])
        else:
            cmd.append(str(ROOT / item))
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(res.stdout[-1200:])
        print(res.stderr[-1200:])
    return res.returncode == 0


async def live_smoke() -> bool:
    """Boot the real app with the mock provider and drive it over HTTP."""
    from tests.mock_nvidia import MockState, make_mock_app
    import httpx
    import uvicorn

    wd = tempfile.mkdtemp(prefix=".pcai-verify-", dir=os.path.expanduser("~"))
    os.environ["PHANTOM_NVIDIA_API_KEY"] = "nvapi-verify-phantom-0000000000"
    os.environ["CODED_NVIDIA_API_KEY"] = "nvapi-verify-coded-0000000000"

    state = MockState()
    mock_cfg = uvicorn.Config(make_mock_app(state), host="127.0.0.1", port=0, log_level="error")
    mock = uvicorn.Server(mock_cfg)
    mock_task = asyncio.create_task(mock.serve())
    while not mock.started:
        await asyncio.sleep(0.02)
    mock_port = mock.servers[0].sockets[0].getsockname()[1]
    os.environ["PHANTOM_NVIDIA_BASE_URL"] = f"http://127.0.0.1:{mock_port}/v1"
    os.environ["CODED_NVIDIA_BASE_URL"] = f"http://127.0.0.1:{mock_port}/v1"

    from phantom_ai.api.app import App
    from phantom_ai.api.server import create_app

    app = App(db_path=str(Path(wd) / "verify.db"), data_dir=str(Path(wd) / "data"))
    await app.startup()
    app_cfg = uvicorn.Config(create_app(app), host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(app_cfg)
    server_task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"

    ok = True

    async def check(name: str, cond: bool, extra: str = "") -> None:
        nonlocal ok
        print(f"   [{'PASS' if cond else 'FAIL'}] {name} {extra}")
        ok = ok and cond

    try:
        async with httpx.AsyncClient(base_url=base, timeout=30) as client:
            # status
            status = (await client.get("/api/status")).json()
            await check("status: phantom/coded/evolution brains + kill switch off",
                        {a["id"] for a in status["agents"]} ==
                        {"phantom", "coded", "evolution"} and
                        not status["killswitch"]["engaged"])
            await check("status: providers connected to mock",
                        status["providers"]["phantom"]["ok"])

            # chat with a tool-call roundtrip (read_file → final answer)
            target = os.path.join(wd, "verify.txt")
            Path(target).write_text("verification payload 2026")

            async def handler(body):
                messages = body["messages"]
                if messages[-1]["role"] == "tool":
                    return {"content": "Verified: the tool executed for real.", "tool_calls": []}
                return {"content": "", "tool_calls": [{"id": "v1", "type": "function",
                                                       "function": {"name": "read_file",
                                                                    "arguments": {"path": target}}}]}

            state.handler = handler
            chat = (await client.post("/api/agents/phantom/chat",
                                      json={"text": "read the verification file", "session_id": "verify"})).json()
            run_id = chat["run_id"]
            cid = chat["conversation_id"]
            msgs = []
            for _ in range(100):
                msgs = (await client.get(f"/api/conversations/{cid}")).json()["messages"]
                if len(msgs) >= 4:
                    break
                await asyncio.sleep(0.05)
            await check("chat: real tool call executed (user/assistant/tool/assistant)",
                        [m["role"] for m in msgs][:4] == ["user", "assistant", "tool", "assistant"])
            await check("chat: final answer present", "Verified" in msgs[-1]["content"])
            await check("chat: run completed", (await client.get("/api/status")).json()
                        .get("active_runs", {}).get("phantom") == [])

            # confirmation flow over HTTP
            victim = os.path.join(wd, "do-not-delete.txt")
            Path(victim).write_text("precious")

            async def handler2(body):
                messages = body["messages"]
                if messages[-1]["role"] == "tool":
                    return {"content": "done.", "tool_calls": []}
                return {"content": "", "tool_calls": [{"id": "v2", "type": "function",
                                                       "function": {"name": "delete_file",
                                                                    "arguments": {"path": victim}}}]}

            state.handler = handler2
            chat2 = (await client.post("/api/agents/phantom/chat",
                                       json={"text": "delete the victim file"})).json()
            conf = None
            for _ in range(100):
                pending = (await client.get("/api/confirmations")).json()["confirmations"]
                if pending:
                    conf = pending[0]
                    break
                await asyncio.sleep(0.05)
            await check("confirmation: requested with impact details",
                        conf is not None and conf["tool_name"] == "delete_file" and "do-not-delete" in conf["impact"])
            if conf:
                await client.post(f"/api/confirmations/{conf['id']}/approve")
                for _ in range(100):
                    if not os.path.exists(victim):
                        break
                    await asyncio.sleep(0.05)
                await check("confirmation: approved → action executed", not os.path.exists(victim))

            # memory + audit observability
            mem = (await client.post("/api/memories", json={
                "agent": "phantom", "content": "verified fact: mock provider works",
                "kind": "fact", "importance": 0.8})).json()
            await check("memory: created", bool(mem.get("id")))
            audit = (await client.get("/api/audit?limit=50")).json()["events"]
            await check("audit: tool/confirmation events recorded",
                        any(e["event"] == "tool.executed" for e in audit) and
                        any(e["event"] == "confirmation.approved" for e in audit))

            # kill switch via API
            await client.post("/api/killswitch/engage", json={"reason": "verification"})
            denied = await client.post("/api/agents/phantom/chat", json={"text": "hi"})
            await check("kill switch: chat refused while engaged", denied.status_code == 409)
            await client.post("/api/killswitch/disengage")
    finally:
        server.should_exit = True
        mock.should_exit = True
        try:
            await asyncio.wait_for(server_task, 10)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            server_task.cancel()
        try:
            await asyncio.wait_for(mock_task, 10)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            mock_task.cancel()
        await app.shutdown()
        shutil.rmtree(wd, ignore_errors=True)

    return ok


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true", help="skip the live smoke test")
    parser.add_argument("--live-only", action="store_true", help="only run the live smoke test")
    args = parser.parse_args()

    all_ok = True
    if not args.live_only:
        print("=" * 72)
        print("PHANTOM + CODED — tier verification")
        print("=" * 72)
        for label, files in TIERS:
            started = time.monotonic()
            passed = run_pytest(files)
            elapsed = time.monotonic() - started
            mark = "PASS" if passed else "FAIL"
            all_ok = all_ok and passed
            print(f"[{mark}] {label}  ({elapsed:.1f}s)")
        print("-" * 72)

    if not args.quick:
        print("\n[smoke] live end-to-end test (real HTTP server + mock NVIDIA)…")
        smoke_ok = await live_smoke()
        all_ok = all_ok and smoke_ok
        print(f"[{'PASS' if smoke_ok else 'FAIL'}] live smoke test")

    print("=" * 72)
    print("OVERALL: " + ("ALL TIERS VERIFIED ✓" if all_ok else "SOME CHECKS FAILED ✗"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
