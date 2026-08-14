"""Stores for the Evolution & System Intelligence upgrade: brains (capability
registry), graph nodes/edges, improvement proposals, and configuration
snapshots (version control for Phantom itself)."""

from __future__ import annotations

import json
import uuid
from typing import Any, Optional

from ..config import now_iso
from .db import Database, dumps, loads


class BrainStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def upsert(self, brain: dict[str, Any]) -> dict[str, Any]:
        brain = dict(brain)
        brain["updated_at"] = now_iso()
        if not brain.get("created_at"):
            brain["created_at"] = brain["updated_at"]
        sql = (
            "INSERT INTO brains (id, name, role, description, system_prompt, model, provider,"
            " key_env, tools, memory_scope, permissions, input_schema, output_schema,"
            " routing_rules, dependencies, verification, version, status, stats, health,"
            " created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET"
            " name=excluded.name, role=excluded.role, description=excluded.description,"
            " system_prompt=excluded.system_prompt, model=excluded.model, provider=excluded.provider,"
            " key_env=excluded.key_env, tools=excluded.tools, memory_scope=excluded.memory_scope,"
            " permissions=excluded.permissions, input_schema=excluded.input_schema,"
            " output_schema=excluded.output_schema, routing_rules=excluded.routing_rules,"
            " dependencies=excluded.dependencies, verification=excluded.verification,"
            " version=excluded.version, status=excluded.status, stats=excluded.stats,"
            " health=excluded.health, updated_at=excluded.updated_at"
        )
        await self.db.execute(sql, (
            brain["id"], brain.get("name"), brain.get("role"), brain.get("description"),
            brain.get("system_prompt"), brain.get("model"), brain.get("provider", "nvidia"),
            brain.get("key_env"), dumps(brain.get("tools", [])),
            brain.get("memory_scope", "own"), dumps(brain.get("permissions", {})),
            dumps(brain.get("input_schema", {})), dumps(brain.get("output_schema", {})),
            brain.get("routing_rules", ""), dumps(brain.get("dependencies", [])),
            brain.get("verification", ""), int(brain.get("version", 1)),
            brain.get("status", "active"), dumps(brain.get("stats", {})),
            dumps(brain.get("health", {})), brain["created_at"], brain["updated_at"],
        ))
        return await self.get(brain["id"])

    async def get(self, brain_id: str) -> Optional[dict[str, Any]]:
        row = await self.db.fetchone("SELECT * FROM brains WHERE id = ?", (brain_id,))
        return self._decode(row)

    async def list(self, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM brains"
        params: tuple = ()
        if status:
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY name"
        rows = await self.db.fetchall(sql, params)
        return [self._decode(r) for r in rows]

    async def delete(self, brain_id: str) -> None:
        await self.db.execute("DELETE FROM brains WHERE id = ?", (brain_id,))

    async def update_stats(self, brain_id: str, stats: dict[str, Any],
                           health: dict[str, Any]) -> None:
        await self.db.execute(
            "UPDATE brains SET stats = ?, health = ?, updated_at = ? WHERE id = ?",
            (dumps(stats), dumps(health), now_iso(), brain_id),
        )

    @staticmethod
    def _decode(row: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        if row is None:
            return None
        for key in ("tools", "permissions", "input_schema", "output_schema",
                    "dependencies", "stats", "health"):
            row[key] = loads(row.get(key), {} if key != "tools" else [])
        return row


class GraphStore:
    """Persistent graph: typed nodes and weighted, typed edges."""

    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def node_id(node_type: str, label: str) -> str:
        import hashlib

        return hashlib.sha1(f"{node_type}|{label}".encode("utf-8")).hexdigest()[:24]

    async def upsert_node(self, node_type: str, label: str, properties: dict | None = None,
                          agent: str = "") -> str:
        nid = self.node_id(node_type, label)
        ts = now_iso()
        props = dumps(properties or {})
        await self.db.execute(
            "INSERT INTO graph_nodes (id, type, label, properties, agent, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET properties=excluded.properties,"
            " agent=excluded.agent, updated_at=excluded.updated_at",
            (nid, node_type, label, props, agent, ts, ts),
        )
        return nid

    async def get_node(self, node_id: str) -> Optional[dict[str, Any]]:
        row = await self.db.fetchone("SELECT * FROM graph_nodes WHERE id = ?", (node_id,))
        if row:
            row["properties"] = loads(row["properties"], {})
        return row

    async def find_node(self, node_type: str, label: str) -> Optional[dict[str, Any]]:
        nid = self.node_id(node_type, label)
        return await self.get_node(nid)

    async def upsert_edge(self, source: str, target: str, relation: str,
                          properties: dict | None = None, weight: float = 1.0) -> str:
        eid = GraphStore.node_id(f"{source}->{target}", relation)
        ts = now_iso()
        await self.db.execute(
            "INSERT INTO graph_edges (id, source, target, relation, properties, weight, created_at)"
            " VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET weight=MAX(weight, excluded.weight),"
            " properties=excluded.properties, created_at=excluded.created_at",
            (eid, source, target, relation, dumps(properties or {}), float(weight), ts),
        )
        return eid

    async def neighbors(self, node_id: str, max_depth: int = 1,
                        max_nodes: int = 100) -> list[dict[str, Any]]:
        """BFS from node_id; returns distinct neighbor nodes with distance."""
        seen: dict[str, int] = {node_id: 0}
        frontier = [node_id]
        out: list[dict[str, Any]] = []
        for _ in range(max(1, min(max_depth, 5))):
            next_frontier: list[str] = []
            if not frontier:
                break
            marks = ",".join("?" * len(frontier))
            rows = await self.db.fetchall(
                f"SELECT source, target FROM graph_edges WHERE source IN ({marks})"
                " OR target IN (" + marks + ")", tuple(frontier * 2))
            for r in rows:
                for nid in (r["source"], r["target"]):
                    if nid not in seen:
                        seen[nid] = seen.get(frontier[0], 0) + 1
                        next_frontier.append(nid)
            frontier = next_frontier
        for nid, dist in list(seen.items())[:max_nodes]:
            node = await self.get_node(nid)
            if node:
                node["distance"] = dist
                out.append(node)
        return out

    async def path(self, source: str, target: str, max_depth: int = 4) -> list[str]:
        """Shortest path via BFS; returns node id sequence."""
        if source == target:
            return [source]
        parent: dict[str, str] = {}
        frontier = [source]
        visited = {source}
        for _ in range(max(1, min(max_depth, 8))):
            marks = ",".join("?" * len(frontier))
            rows = await self.db.fetchall(
                f"SELECT source, target FROM graph_edges WHERE source IN ({marks})"
                " OR target IN (" + marks + ")", tuple(frontier * 2))
            nxt: list[str] = []
            for r in rows:
                pairs = [(r["source"], r["target"]), (r["target"], r["source"])]
                for a, b in pairs:
                    if a in frontier and b not in visited:
                        visited.add(b)
                        parent[b] = a
                        if b == target:
                            seq = [b]
                            while seq[-1] != source:
                                seq.append(parent[seq[-1]])
                            return list(reversed(seq))
                        nxt.append(b)
            frontier = nxt
        return []

    async def search(self, query: str, node_type: str | None = None,
                     limit: int = 25) -> list[dict[str, Any]]:
        like = f"%{query}%"
        sql = "SELECT * FROM graph_nodes WHERE label LIKE ?"
        params: list[Any] = [like]
        if node_type:
            sql += " AND type = ?"
            params.append(node_type)
        sql += " LIMIT ?"
        params.append(min(limit, 100))
        rows = await self.db.fetchall(sql, tuple(params))
        for r in rows:
            r["properties"] = loads(r["properties"], {})
        return rows

    async def by_type(self, node_type: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT * FROM graph_nodes WHERE type = ? LIMIT ?", (node_type, min(limit, 500)))
        for r in rows:
            r["properties"] = loads(r["properties"], {})
        return rows

    async def stats(self) -> dict[str, Any]:
        nodes = await self.db.scalar("SELECT COUNT(*) FROM graph_nodes") or 0
        edges = await self.db.scalar("SELECT COUNT(*) FROM graph_edges") or 0
        by_type = await self.db.fetchall(
            "SELECT type, COUNT(*) AS n FROM graph_nodes GROUP BY type ORDER BY n DESC")
        return {"nodes": nodes, "edges": edges,
                "by_type": {r["type"]: r["n"] for r in by_type}}

    async def delete(self, node_id: str) -> None:
        await self.db.execute("DELETE FROM graph_edges WHERE source = ? OR target = ?",
                              (node_id, node_id))
        await self.db.execute("DELETE FROM graph_nodes WHERE id = ?", (node_id,))


class ProposalStore:
    STATUSES = {"proposed", "approved", "deployed", "rejected", "rolled_back", "failed"}

    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, title: str, description: str, kind: str, changes: dict,
                     risk: str, created_by: str, snapshot_id: str = "") -> dict:
        row = {"id": uuid.uuid4().hex, "title": title[:200], "description": description[:4000],
               "kind": kind, "changes": changes, "risk": risk[:50] or "low",
               "status": "proposed", "created_by": created_by, "snapshot_id": snapshot_id,
               "created_at": now_iso()}
        await self.db.execute(
            "INSERT INTO proposals (id, title, description, kind, changes, risk, status,"
            " created_by, snapshot_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (row["id"], row["title"], row["description"], row["kind"], dumps(changes),
             row["risk"], row["status"], row["created_by"], row["snapshot_id"],
             row["created_at"]),
        )
        return await self.get(row["id"])

    async def get(self, proposal_id: str) -> Optional[dict[str, Any]]:
        row = await self.db.fetchone("SELECT * FROM proposals WHERE id = ?", (proposal_id,))
        if row:
            row["changes"] = loads(row["changes"], {})
        return row

    async def set_status(self, proposal_id: str, status: str, result: str = "",
                         tests: str = "") -> Optional[dict[str, Any]]:
        if status not in self.STATUSES:
            raise ValueError(f"bad proposal status {status}")
        fields = {"status": status}
        ts = now_iso()
        if status in ("deployed", "failed", "rolled_back"):
            fields["deployed_at"] = ts
        if status in ("approved", "rejected", "rolled_back"):
            fields["decided_at"] = ts
        if result:
            fields["result"] = result[:4000]
        if tests:
            fields["tests"] = tests[:4000]
        sets = ", ".join(f"{k} = ?" for k in fields)
        params = list(fields.values()) + [proposal_id]
        await self.db.execute(f"UPDATE proposals SET {sets} WHERE id = ?", tuple(params))
        return await self.get(proposal_id)

    async def list(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        sql = "SELECT * FROM proposals"
        params: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(min(limit, 500))
        rows = await self.db.fetchall(sql, tuple(params))
        for r in rows:
            r["changes"] = loads(r["changes"], {})
        return rows


class SnapshotStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, label: str, description: str, data: dict,
                     kind: str = "config") -> dict:
        prev = await self.db.scalar(
            "SELECT id FROM snapshots ORDER BY created_at DESC LIMIT 1")
        row = {"id": uuid.uuid4().hex, "label": label[:200], "description": description[:2000],
               "kind": kind, "data": data, "previous_id": prev or "", "created_at": now_iso()}
        await self.db.execute(
            "INSERT INTO snapshots (id, label, description, kind, data, previous_id, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (row["id"], row["label"], row["description"], row["kind"], dumps(data),
             row["previous_id"], row["created_at"]),
        )
        return await self.get(row["id"])

    async def get(self, snapshot_id: str) -> Optional[dict[str, Any]]:
        row = await self.db.fetchone("SELECT * FROM snapshots WHERE id = ?", (snapshot_id,))
        if row:
            row["data"] = loads(row["data"], {})
        return row

    async def list(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT * FROM snapshots ORDER BY created_at DESC LIMIT ?", (min(limit, 200),))
        for r in rows:
            r["data"] = loads(r["data"], {})
        return rows

    async def mark_restored(self, snapshot_id: str) -> None:
        await self.db.execute("UPDATE snapshots SET restored_at = ? WHERE id = ?",
                              (now_iso(), snapshot_id))
