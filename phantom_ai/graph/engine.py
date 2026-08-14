"""Graph Engine — a persistent graph of users, projects, memories, tasks,
tools, agents, workflows, files and events, with relationship discovery.

The system is NOT represented only as linear conversations: nodes and typed
edges let Phantom answer "what is relevant to this task?" by traversing
relationships (user → project → tool → memory → previous outcome → …).
"""

from __future__ import annotations

from typing import Any, Optional

from ..storage.evolution import GraphStore

NODE_TYPES = {
    "user", "project", "memory", "conversation", "goal", "task", "file",
    "application", "website", "agent", "brain", "tool", "api", "model",
    "workflow", "knowledge", "skill", "permission", "dependency", "result",
    "error", "event", "learning",
}
RELATIONS = {
    "has_goal", "has_project", "uses", "requires", "assigned_to", "produces",
    "verified_by", "stored_in", "related_to", "delegates_to", "part_of",
    "mentions", "occurred_in", "affects", "tracks", "precedes", "follows",
}


class GraphEngine:
    def __init__(self, store: GraphStore, audit: Any, events: Any,
                 enabled: bool = True) -> None:
        self.store = store
        self.audit = audit
        self.events = events
        self.enabled = enabled

    # ---- high-level helpers ------------------------------------------

    async def track(self, node_type: str, label: str, properties: dict | None = None,
                    agent: str = "", relation: str = "", target: str = "",
                    weight: float = 1.0) -> dict[str, Any]:
        """Upsert a node (and optionally an edge to an existing node)."""
        if node_type not in NODE_TYPES:
            raise ValueError(f"unknown node type: {node_type} (allowed: {sorted(NODE_TYPES)})")
        nid = await self.store.upsert_node(node_type, label, properties, agent)
        if relation:
            if relation not in RELATIONS and not relation.startswith("x_"):
                raise ValueError(f"unknown relation: {relation}")
            await self.store.upsert_edge(nid, target, relation,
                                         properties={"by": agent or "system"},
                                         weight=weight)
        return {"node_id": nid, "type": node_type, "label": label}

    async def relate(self, source: str, target: str, relation: str,
                     properties: dict | None = None, weight: float = 1.0) -> str:
        return await self.store.upsert_edge(source, target, relation, properties, weight)

    async def link_user_conversation(self, user_label: str, conversation_id: str) -> None:
        user_node = await self.store.upsert_node("user", user_label or "user",
                                                 {"origin": "local"})
        conv_node = await self.store.upsert_node(
            "conversation", conversation_id, {"conversation_id": conversation_id})
        await self.store.upsert_edge(user_node, conv_node, "has_project" if False else "mentions",
                                     properties={"kind": "conversation"})

    async def task_to_conversation(self, task_label: str, conversation_id: str,
                                   agent: str) -> str:
        task_node = await self.store.upsert_node("task", task_label,
                                                 {"agent": agent}, agent)
        conv_node = await self.store.upsert_node(
            "conversation", conversation_id, {"conversation_id": conversation_id}, agent)
        await self.store.upsert_edge(task_node, conv_node, "occurred_in",
                                     properties={"agent": agent})
        return task_node

    # ---- discovery ----------------------------------------------------

    async def relevant_to(self, text: str, agent: str = "", limit: int = 8) -> list[dict[str, Any]]:
        """Find graph nodes plausibly relevant to a task description."""
        import re

        keywords = {w.lower() for w in re.findall(r"[a-zA-Z0-9_]{4,}", text or "")}
        if not keywords:
            return []
        candidates = await self.store.by_type("project", limit=200)
        candidates += await self.store.by_type("task", limit=200)
        candidates += await self.store.by_type("memory", limit=200)
        candidates += await self.store.by_type("tool", limit=200)
        scored = []
        for node in candidates:
            hay = (node.get("label") or "").lower() + " " + \
                  " ".join(str(v) for v in (node.get("properties") or {}).values()).lower()
            hits = sum(1 for k in keywords if k in hay)
            if hits:
                scored.append((hits, node))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [n for _, n in scored[:limit]]

    async def related(self, node_id: str, depth: int = 2, limit: int = 50) -> list[dict[str, Any]]:
        return await self.store.neighbors(node_id, max_depth=depth, max_nodes=limit)

    async def path_between(self, a: str, b: str, max_depth: int = 4) -> list[str]:
        return await self.store.path(a, b, max_depth)

    async def stats(self) -> dict[str, Any]:
        return await self.store.stats()
