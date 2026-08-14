"""Confirmation manager: pending approvals with explicit, per-action consent.

Approval applies ONLY to the single action it was requested for. There is no
"always allow" blanket mode: every consequential action asks again.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from ..storage.ops import ConfirmationStore

DEFAULT_TIMEOUT = 900.0      # interactive: 15 minutes to decide
BACKGROUND_TIMEOUT = 180.0   # background/proactive runs: 3 minutes


class ConfirmationManager:
    def __init__(self, store: ConfirmationStore) -> None:
        self.store = store
        self._waiters: dict[str, asyncio.Event] = {}
        self._decisions: dict[str, str] = {}

    async def create(self, agent: str, conversation_id: str, tool_name: str,
                     arguments: dict, reason: str, impact: str, risk: str,
                     **extra: Any) -> dict:
        row = await self.store.create(agent, conversation_id, tool_name, arguments,
                                      reason, impact, risk)
        self._waiters[row["id"]] = asyncio.Event()
        return row

    async def wait(self, confirmation_id: str, timeout: float = DEFAULT_TIMEOUT,
                   interactive: bool = True) -> str:
        """Block until the user decides. Returns approved|denied|expired."""
        event = self._waiters.get(confirmation_id)
        if event is None:
            row = await self.store.get(confirmation_id)
            return row["status"] if row and row["status"] in ("approved", "denied") else "expired"
        # Background/proactive runs get a shorter decision window.
        effective_timeout = timeout if interactive else min(timeout, BACKGROUND_TIMEOUT)
        try:
            await asyncio.wait_for(event.wait(), timeout=effective_timeout)
        except asyncio.TimeoutError:
            await self.store.decide(confirmation_id, "expired", decided_by="timeout")
            self._cleanup(confirmation_id)
            return "expired"
        status = self._decisions.pop(confirmation_id, "expired")
        self._cleanup(confirmation_id)
        return status

    async def decide(self, confirmation_id: str, approve: bool, decided_by: str = "user") -> dict:
        row = await self.store.get(confirmation_id)
        if not row:
            raise ValueError(f"unknown confirmation: {confirmation_id}")
        if row["status"] != "pending":
            raise ValueError(f"confirmation already {row['status']}")
        status = "approved" if approve else "denied"
        updated = await self.store.decide(confirmation_id, status, decided_by=decided_by)
        event = self._waiters.get(confirmation_id)
        if event:
            self._decisions[confirmation_id] = status
            event.set()
        return updated

    def _cleanup(self, confirmation_id: str) -> None:
        self._waiters.pop(confirmation_id, None)
        self._decisions.pop(confirmation_id, None)

    async def pending_for_agent(self, agent: str) -> list[dict]:
        return await self.store.pending_for_agent(agent)

    def active_count(self) -> int:
        return len(self._waiters)
