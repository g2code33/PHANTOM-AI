"""Version control for Phantom itself: configuration snapshots with restore
(rollback). Snapshot → Change → Test → Deploy → Monitor → Rollback.

A snapshot captures the live system configuration (settings, permission
overrides, registered brains, graph summary). Restoring rewinds those values so
an experimental improvement can never permanently damage the stable system.
"""

from __future__ import annotations

from typing import Any

from ..config import now_iso
from ..storage.evolution import SnapshotStore


class ConfigSnapshots:
    def __init__(self, store: SnapshotStore, settings: Any, brain_registry: Any,
                 audit: Any, events: Any) -> None:
        self.store = store
        self.settings = settings
        self.brain_registry = brain_registry
        self.audit = audit
        self.events = events

    async def capture(self, label: str, description: str = "") -> dict:
        data = await self._collect()
        return await self.store.create(label, description, data)

    async def create(self, label: str, description: str = "") -> dict:
        """Alias for capture() — used by the loop engine and tools."""
        return await self.capture(label, description)

    async def _collect(self) -> dict[str, Any]:
        raw = await self.settings.all()
        brains = [b for b in await self.brain_registry.list()]
        return {
            "settings": raw,
            "brains": [{k: b.get(k) for k in ("id", "model", "permissions", "status",
                                              "tools", "memory_scope")} for b in brains],
            "captured_at": now_iso(),
        }

    async def restore(self, snapshot_id: str, reason: str = "manual") -> dict[str, Any]:
        snapshot = await self.store.get(snapshot_id)
        if not snapshot:
            raise ValueError(f"unknown snapshot: {snapshot_id}")
        data = snapshot.get("data") or {}
        await self._apply(data)
        await self.store.mark_restored(snapshot_id)
        await self.audit.record("system", "snapshot.restored", {
            "snapshot_id": snapshot_id, "label": snapshot.get("label"),
            "reason": reason, "previous_snapshot": snapshot.get("previous_id")})
        if self.events:
            await self.events.publish("snapshot.restored", {"snapshot": snapshot})
        return snapshot

    async def _apply(self, data: dict[str, Any]) -> None:
        settings_data = data.get("settings") or {}
        # current keys per agent
        current = await self.settings.all()
        for agent, kv in settings_data.items():
            for key, value in kv.items():
                await self.settings.set(key, value, agent)
            # remove keys that existed before but not in the snapshot
            for key in set(current.get(agent, {})) - set(kv):
                await self.settings.delete(key, agent)
        for agent in set(current) - set(settings_data):
            for key in current[agent]:
                await self.settings.delete(key, agent)

        brains = data.get("brains") or []
        for b in brains:
            existing = await self.brain_registry.get(b["id"])
            if existing:
                existing.update({k: v for k, v in b.items() if v is not None})
                await self.brain_registry.store.upsert(existing)

    async def list(self) -> list[dict]:
        return await self.store.list()
