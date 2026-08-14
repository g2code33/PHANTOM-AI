"""Agent-to-agent delegation: Phantom ⇄ Coded.

Every delegation carries a task ID, originating agent, receiving agent, clear
objective, context, effective tool permissions, timeout, result and status.

Loop prevention: mutual delegations between the same two agents are counted in
a rolling window; exceeding the configured limit blocks the delegation. Global
concurrency is also limited (no unbounded agent spawning).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from ..storage.ops import DelegationStore
from .identities import identity

MAX_LOOP_WINDOW_MINUTES = 30


class DelegationManager:
    def __init__(self, store: DelegationStore, settings: Any, audit: Any, events: Any,
                 agent_runner: Any, killswitch: Any) -> None:
        self.store = store
        self.settings = settings
        self.audit = audit
        self.events = events
        self.agent_runner = agent_runner  # async callable(agent_id, conversation_id, text, mode, ...) -> result
        self.killswitch = killswitch
        self._active: dict[str, asyncio.Task] = {}
        self._semaphore: Optional[asyncio.Semaphore] = None

    async def _ensure_semaphore(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            limit = int(await self.settings.get("delegation.max_concurrent", "*", 2))
            self._semaphore = asyncio.Semaphore(max(1, limit))
        return self._semaphore

    async def delegate(
        self,
        origin: str,
        target: str,
        objective: str,
        context: str = "",
        timeout_sec: int = 300,
        permissions_override: Optional[dict] = None,
        origin_conversation_id: str = "",
        origin_session_id: str = "",
    ) -> dict[str, Any]:
        if self.killswitch.is_engaged():
            return {"status": "blocked", "error": "kill switch engaged"}
        if origin == target:
            return {"status": "blocked", "error": "agents cannot delegate to themselves"}
        if origin not in ("phantom", "coded") or target not in ("phantom", "coded"):
            return {"status": "blocked", "error": f"unknown agent: {origin}->{target}"}

        # ---- loop prevention -------------------------------------------
        max_loops = int(await self.settings.get("delegation.max_loops", "*", 3))
        recent = await self.store.count_recent_between(origin, target, MAX_LOOP_WINDOW_MINUTES)
        if recent >= max_loops:
            msg = (f"delegation loop guard: {origin}⇄{target} have exchanged {recent} "
                   f"delegations in the last {MAX_LOOP_WINDOW_MINUTES} min (limit {max_loops})")
            await self.audit.record(origin, "delegation.blocked", {
                "target": target, "objective": objective[:300], "reason": msg})
            return {"status": "blocked", "error": msg}

        row = await self.store.create(origin, target, objective, context, timeout_sec,
                                      permissions_override)
        delegation_id = row["id"]
        task_id = row["task_id"]
        await self.audit.record(origin, "delegation.started", {
            "delegation_id": delegation_id, "task_id": task_id, "target": target,
            "objective": objective[:500],
        })
        if self.events:
            await self.events.publish("delegation.started", {
                "delegation_id": delegation_id, "origin": origin, "target": target,
                "objective": objective[:400], "task_id": task_id,
            })

        sem = await self._ensure_semaphore()
        async with sem:
            await self.store.set_status(delegation_id, "running")
            conversation_id = await self._create_delegation_conversation(target, objective)
            run = asyncio.ensure_future(self._run_target(
                target, conversation_id, delegation_id, objective, context,
                origin, permissions_override or {},
            ))
            self._active[delegation_id] = run
            try:
                result = await asyncio.wait_for(run, timeout=timeout_sec)
            except asyncio.TimeoutError:
                run.cancel()
                try:
                    await run
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
                result = {"status": "timed_out",
                          "error": f"delegation exceeded its {timeout_sec}s timeout"}
            finally:
                self._active.pop(delegation_id, None)

        status = result.get("status", "failed")
        await self.store.set_status(delegation_id, status, result=result,
                                    error=result.get("error"))
        await self.audit.record(origin, "delegation.completed", {
            "delegation_id": delegation_id, "target": target, "status": status,
            "latency_s": round(time.monotonic() - 0, 2), "error": result.get("error"),
        })
        if self.events:
            await self.events.publish("delegation.completed", {
                "delegation_id": delegation_id, "status": status,
                "summary": (result.get("summary") or result.get("error") or "")[:400],
            })
        return {"delegation_id": delegation_id, "task_id": task_id, **result}

    async def _run_target(self, target: str, conversation_id: str, delegation_id: str,
                          objective: str, context: str, origin: str,
                          permissions_override: dict) -> dict[str, Any]:
        task_text = (f"TASK FROM {identity(origin)['display_name'].upper()}\n\n"
                     f"OBJECTIVE: {objective}\n")
        if context:
            task_text += f"\nCONTEXT:\n{context}\n"
        task_text += ("\nWork through this task using your tools. When finished, reply with a "
                      "clear STRUCTURED REPORT: what you did, what you found, the outcome, and "
                      "any caveats. Keep it concise but complete.")
        result = await self.agent_runner(
            agent_id=target,
            conversation_id=conversation_id,
            user_text=task_text,
            session_id=f"delegation:{delegation_id}",
            mode="delegation",
            permissions_override=permissions_override,
        )
        if result.get("status") == "cancelled":
            return {"status": "cancelled", "error": "run cancelled"}
        if result.get("status") != "ok":
            return {"status": "failed", "error": result.get("error", "run failed")}
        return {
            "status": "completed",
            "summary": result.get("content", "")[:6000],
            "tool_calls_made": result.get("tool_calls_made", 0),
            "model": result.get("model", ""),
        }

    async def _create_delegation_conversation(self, target: str, objective: str) -> str:
        from ..config import now_iso
        import uuid

        cid = uuid.uuid4().hex
        await self.store.db.execute(
            "INSERT INTO conversations (id, agent, title, created_at, updated_at) VALUES (?,?,?,?,?)",
            (cid, target, f"Delegation: {objective[:120]}", now_iso(), now_iso()),
        )
        return cid

    async def cancel(self, delegation_id: str) -> bool:
        task = self._active.get(delegation_id)
        if task is None:
            return False
        task.cancel()
        return True
