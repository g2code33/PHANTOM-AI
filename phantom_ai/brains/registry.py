"""Capability Registry (Brain Registry).

Maintains a live, machine-readable representation of every brain/agent in the
system: identity, purpose, model/provider, tools, memory scope, permissions,
dependencies, health, performance, version, status.

New brains are registered dynamically (validate → assign capabilities →
assign permissions → test → activate) without rebuilding the application.
Creation of privileged brains requires explicit authorization (confirmation).
"""

from __future__ import annotations

from typing import Any, Optional

from ..storage.evolution import BrainStore
from .definitions import BrainDefinition

BUILTIN_BRAIN_IDS = ("phantom", "coded", "evolution")


class BrainRegistry:
    def __init__(self, store: BrainStore, settings: Any, audit: Any,
                 events: Any, tool_registry: Any, confirmations: Any,
                 agent_factory: Any = None) -> None:
        self.store = store
        self.settings = settings
        self.audit = audit
        self.events = events
        self.tool_registry = tool_registry
        self.confirmations = confirmations
        self.agent_factory = agent_factory  # async (BrainDefinition) -> dict
        self._cache: dict[str, dict[str, Any]] = {}
        self._ids: set[str] = set()

    # ------------------------------------------------------------------
    async def seed_builtins(self, definitions: list[BrainDefinition]) -> None:
        for definition in definitions:
            await self.register(definition, created_by="system", require_approval=False)

    async def register(self, definition: BrainDefinition, created_by: str = "user",
                       require_approval: bool = True) -> dict[str, Any]:
        errors = definition.validate()
        if errors:
            raise ValueError("invalid brain definition: " + "; ".join(errors))

        existing = await self.store.get(definition.brain_id)
        if existing:
            raise ValueError(f"brain already registered: {definition.brain_id}")

        # tools must exist in the registry
        if definition.tools:
            for tool_name in definition.tools:
                if not self.tool_registry.has(tool_name):
                    raise ValueError(f"unknown tool in brain definition: {tool_name}")

        # privileged brains (extra tools beyond the safe default set, or
        # request to use a permission override) require authorization
        is_privileged = bool(definition.permissions)
        if require_approval and is_privileged:
            confirmation = await self.confirmations.create(
                agent=created_by if created_by in ("phantom", "coded", "evolution") else "phantom",
                conversation_id="brain-registry",
                tool_name="register_brain",
                arguments={"brain_id": definition.brain_id, "permissions": definition.permissions},
                reason="A new brain with privilege overrides is being registered.",
                impact=f"New brain '{definition.brain_id}' will be activated with custom "
                       f"permissions: {definition.permissions}",
                risk="Privileged agents can act on the system; authorization is required.",
            )
            decision = await self.confirmations.wait(confirmation["id"], timeout=600)
            if decision != "approved":
                await self.audit.record("system", "brain.registration_denied",
                                        {"brain_id": definition.brain_id})
                raise PermissionError(f"brain registration not authorized: {definition.brain_id}")

        brain = await self.store.upsert(definition.to_dict())
        self._cache[definition.brain_id] = brain
        self._ids.add(definition.brain_id)

        # optionally create a live agent for this brain
        created_agent = None
        if self.agent_factory is not None:
            try:
                created_agent = await self.agent_factory(definition)
            except Exception as exc:  # noqa: BLE001
                await self.audit.record("system", "brain.agent_create_failed",
                                        {"brain_id": definition.brain_id, "error": str(exc)[:300]})

        await self.audit.record(created_by, "brain.registered", {
            "brain_id": definition.brain_id, "role": definition.role,
            "tools": definition.tools or "all", "privileged": is_privileged,
            "agent_created": bool(created_agent)})
        if self.events:
            await self.events.publish("brain.registered", {"brain": brain})
        return {"brain": brain, "agent_created": bool(created_agent)}

    # ------------------------------------------------------------------
    async def get(self, brain_id: str) -> Optional[dict[str, Any]]:
        if brain_id in self._cache:
            return self._cache[brain_id]
        brain = await self.store.get(brain_id)
        if brain:
            self._cache[brain_id] = brain
        return brain

    async def list(self) -> list[dict[str, Any]]:
        rows = await self.store.list()
        for r in rows:
            self._cache[r["id"]] = r
            self._ids.add(r["id"])
        return rows

    async def set_status(self, brain_id: str, status: str) -> None:
        brain = await self.store.get(brain_id)
        if not brain:
            return
        brain["status"] = status
        await self.store.upsert(brain)
        self._cache[brain_id] = brain

    async def update_health(self, brain_id: str, stats: dict[str, Any],
                            health: dict[str, Any]) -> None:
        await self.store.update_stats(brain_id, stats, health)
        brain = await self.store.get(brain_id)
        if brain:
            self._cache[brain_id] = brain

    async def delete(self, brain_id: str) -> None:
        await self.store.delete(brain_id)
        self._cache.pop(brain_id, None)
        self._ids.discard(brain_id)

    def ids_sync(self) -> tuple[str, ...]:
        return tuple(sorted(self._ids))

    # ------------------------------------------------------------------
    async def ids(self) -> list[str]:
        return [b["id"] for b in await self.list()]

    def summary(self, brain: dict[str, Any]) -> dict[str, Any]:
        stats = brain.get("stats") or {}
        health = brain.get("health") or {}
        return {
            "id": brain["id"], "name": brain.get("name"), "role": brain.get("role"),
            "model": brain.get("model"), "provider": brain.get("provider"),
            "version": brain.get("version"), "status": brain.get("status"),
            "tools": brain.get("tools") or "all",
            "memory_scope": brain.get("memory_scope"),
            "success_rate": stats.get("success_rate"),
            "avg_latency_ms": stats.get("avg_latency_ms"),
            "error_rate": stats.get("error_rate"),
            "tasks": stats.get("tasks", 0),
            "health_state": health.get("state", "unknown"),
            "last_active": stats.get("last_active"),
        }
