"""Storage for operational records: audit log, tasks, delegations, schedules,
notifications, confirmations."""

from __future__ import annotations

import uuid
from typing import Any, Optional

from ..config import now_iso
from .db import Database, dumps, loads


class AuditStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def record(self, agent: str, event: str, detail: Any = None,
                     latency_ms: float | None = None, model: str | None = None,
                     tokens: Any = None) -> dict:
        row = {
            "ts": now_iso(), "agent": agent, "event": event, "detail": dumps(detail or {}),
            "latency_ms": latency_ms, "model": model, "tokens": dumps(tokens) if tokens else None,
        }
        await self.db.execute(
            "INSERT INTO audit_log (ts, agent, event, detail, latency_ms, model, tokens)"
            " VALUES (?,?,?,?,?,?,?)",
            (row["ts"], row["agent"], event, row["detail"], row["latency_ms"], row["model"], row["tokens"]),
        )
        return row

    async def query(self, agent: str | None = None, event: str | None = None,
                    limit: int = 200, offset: int = 0) -> list[dict]:
        sql = "SELECT * FROM audit_log WHERE 1=1"
        params: list[Any] = []
        if agent:
            sql += " AND agent = ?"
            params.append(agent)
        if event:
            sql += " AND event = ?"
            params.append(event)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        rows = await self.db.fetchall(sql, tuple(params))
        for r in rows:
            r["detail"] = loads(r["detail"], {})
            r["tokens"] = loads(r["tokens"], None)
        return rows

    async def events(self) -> list[str]:
        rows = await self.db.fetchall("SELECT DISTINCT event FROM audit_log ORDER BY event")
        return [r["event"] for r in rows]


class TaskStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, agent: str, name: str, kind: str = "manual") -> dict:
        tid = uuid.uuid4().hex
        ts = now_iso()
        await self.db.execute(
            "INSERT INTO tasks (id, agent, name, kind, status, created_at, logs) VALUES (?,?,?,?,?,?,?)",
            (tid, agent, name[:200], kind, "queued", ts, dumps([])),
        )
        return await self.get(tid)

    async def get(self, task_id: str) -> Optional[dict]:
        row = await self.db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if row:
            row["logs"] = loads(row["logs"], [])
        return row

    async def set_status(self, task_id: str, status: str, error: str | None = None) -> None:
        ended = now_iso() if status in ("completed", "failed", "cancelled") else None
        started = now_iso() if status == "running" and not (await self.get(task_id))["started_at"] else None
        await self.db.execute(
            "UPDATE tasks SET status = ?, error = ?, started_at = COALESCE(started_at, ?),"
            " ended_at = ? WHERE id = ?",
            (status, error, started, ended, task_id),
        )

    async def append_log(self, task_id: str, entry: str) -> None:
        row = await self.get(task_id)
        if not row:
            return
        logs = row["logs"] + [f"[{now_iso()}] {entry}"]
        await self.db.execute("UPDATE tasks SET logs = ? WHERE id = ?", (dumps(logs[-200:]), task_id))

    async def list(self, agent: str | None = None, status: str | None = None, limit: int = 100) -> list[dict]:
        sql = "SELECT * FROM tasks WHERE 1=1"
        params: list[Any] = []
        if agent:
            sql += " AND agent = ?"
            params.append(agent)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = await self.db.fetchall(sql, tuple(params))
        for r in rows:
            r["logs"] = loads(r["logs"], [])
        return rows


class DelegationStore:
    STATUSES = {"queued", "running", "completed", "failed", "timed_out", "cancelled", "blocked"}

    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, origin: str, target: str, objective: str, context: str,
                     timeout_sec: int, permissions: dict | None = None) -> dict:
        row = {
            "id": uuid.uuid4().hex,
            "task_id": uuid.uuid4().hex,
            "origin": origin, "target": target,
            "objective": objective[:4000], "context": (context or "")[:8000],
            "timeout_sec": timeout_sec, "status": "queued",
            "permissions": dumps(permissions or {}),
            "created_at": now_iso(),
        }
        await self.db.execute(
            "INSERT INTO delegations (id, task_id, origin, target, objective, context, timeout_sec,"
            " status, result, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (row["id"], row["task_id"], row["origin"], row["target"], row["objective"],
             row["context"], row["timeout_sec"], row["status"], None, row["created_at"]),
        )
        return await self.get(row["id"])

    async def get(self, delegation_id: str) -> Optional[dict]:
        row = await self.db.fetchone("SELECT * FROM delegations WHERE id = ?", (delegation_id,))
        if row:
            row["result"] = loads(row["result"], None)
        return row

    async def set_status(self, delegation_id: str, status: str, result: Any = None,
                         error: str | None = None) -> None:
        completed = now_iso() if status in ("completed", "failed", "timed_out", "cancelled", "blocked") else None
        started = now_iso() if status == "running" else None
        await self.db.execute(
            "UPDATE delegations SET status = ?, result = ?, error = ?,"
            " started_at = COALESCE(started_at, ?), completed_at = ? WHERE id = ?",
            (status, dumps(result) if result is not None else None, error, started, completed, delegation_id),
        )

    async def count_recent_between(self, a: str, b: str, window_minutes: int = 15) -> int:
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS n FROM delegations"
            " WHERE ((origin = ? AND target = ?) OR (origin = ? AND target = ?))"
            " AND created_at >= datetime('now', ?)",
            (a, b, b, a, f"-{window_minutes} minutes"),
        )
        return int(row["n"] or 0)

    async def list(self, origin: str | None = None, target: str | None = None,
                   status: str | None = None, limit: int = 100) -> list[dict]:
        sql = "SELECT * FROM delegations WHERE 1=1"
        params: list[Any] = []
        if origin:
            sql += " AND origin = ?"
            params.append(origin)
        if target:
            sql += " AND target = ?"
            params.append(target)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = await self.db.fetchall(sql, tuple(params))
        for r in rows:
            r["result"] = loads(r["result"], None)
        return rows


class ScheduleStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, agent: str, name: str, expression: str, prompt: str,
                     quiet_start: str | None = None, quiet_end: str | None = None) -> dict:
        row = {
            "id": uuid.uuid4().hex, "agent": agent, "name": name[:120], "expression": expression,
            "prompt": prompt[:4000], "quiet_start": quiet_start, "quiet_end": quiet_end,
            "enabled": 1, "next_run_at": None,
        }
        await self.db.execute(
            "INSERT INTO schedules (id, agent, name, expression, prompt, quiet_start, quiet_end,"
            " next_run_at) VALUES (?,?,?,?,?,?,?,?)",
            (row["id"], agent, row["name"], expression, row["prompt"], quiet_start, quiet_end, None),
        )
        return await self.get(row["id"])

    async def get(self, schedule_id: str) -> Optional[dict]:
        return await self.db.fetchone("SELECT * FROM schedules WHERE id = ?", (schedule_id,))

    async def update(self, schedule_id: str, **fields: Any) -> Optional[dict]:
        allowed = {"name", "expression", "prompt", "quiet_start", "quiet_end", "enabled",
                   "last_run_at", "next_run_at", "last_status", "last_result"}
        sets, params = [], []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k} = ?")
                params.append(v)
        if not sets:
            return await self.get(schedule_id)
        params.append(schedule_id)
        await self.db.execute(f"UPDATE schedules SET {', '.join(sets)} WHERE id = ?", tuple(params))
        return await self.get(schedule_id)

    async def delete(self, schedule_id: str) -> None:
        await self.db.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))

    async def list(self, agent: str | None = None) -> list[dict]:
        sql = "SELECT * FROM schedules"
        params: tuple = ()
        if agent:
            sql += " WHERE agent = ?"
            params = (agent,)
        sql += " ORDER BY enabled DESC, next_run_at ASC"
        return await self.db.fetchall(sql, params)


class NotificationStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, agent: str, title: str, body: str) -> dict:
        row = {"id": uuid.uuid4().hex, "agent": agent, "title": title[:200], "body": body[:2000],
               "created_at": now_iso()}
        await self.db.execute(
            "INSERT INTO notifications (id, agent, title, body, created_at) VALUES (?,?,?,?,?)",
            (row["id"], agent, row["title"], row["body"], row["created_at"]),
        )
        return row

    async def list(self, agent: str | None = None, limit: int = 100) -> list[dict]:
        sql = "SELECT * FROM notifications WHERE dismissed = 0"
        params: list[Any] = []
        if agent:
            sql += " AND agent = ?"
            params.append(agent)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return await self.db.fetchall(sql, tuple(params))

    async def mark(self, notification_id: str, **fields: Any) -> None:
        allowed = {"read", "dismissed"}
        sets = [f"{k} = ?" for k in fields if k in allowed]
        if sets:
            await self.db.execute(
                f"UPDATE notifications SET {', '.join(sets)} WHERE id = ?",
                tuple([1 if fields[k] else 0 for k in fields if k in allowed] + [notification_id]),
            )

    async def unread_count(self, agent: str | None = None) -> int:
        if agent:
            return await self.db.scalar(
                "SELECT COUNT(*) FROM notifications WHERE read = 0 AND dismissed = 0 AND agent = ?", (agent,))
        return await self.db.scalar(
            "SELECT COUNT(*) FROM notifications WHERE read = 0 AND dismissed = 0")


class ConfirmationStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, agent: str, conversation_id: str, tool_name: str, arguments: dict,
                     reason: str, impact: str, risk: str) -> dict:
        row = {
            "id": uuid.uuid4().hex, "agent": agent, "conversation_id": conversation_id,
            "tool_name": tool_name, "arguments": arguments, "reason": reason[:2000],
            "impact": impact[:2000], "risk": risk[:2000], "status": "pending",
            "requested_at": now_iso(),
        }
        await self.db.execute(
            "INSERT INTO confirmations (id, agent, conversation_id, tool_name, arguments, reason,"
            " impact, risk, status, requested_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (row["id"], agent, conversation_id, tool_name, dumps(arguments), reason, impact,
             risk, "pending", row["requested_at"]),
        )
        return row

    async def decide(self, confirmation_id: str, status: str, decided_by: str = "user") -> Optional[dict]:
        if status not in ("approved", "denied", "expired"):
            raise ValueError(f"bad confirmation status {status}")
        await self.db.execute(
            "UPDATE confirmations SET status = ?, decided_at = ?, decided_by = ? WHERE id = ?",
            (status, now_iso(), decided_by, confirmation_id),
        )
        return await self.get(confirmation_id)

    async def get(self, confirmation_id: str) -> Optional[dict]:
        row = await self.db.fetchone("SELECT * FROM confirmations WHERE id = ?", (confirmation_id,))
        if row:
            row["arguments"] = loads(row["arguments"], {})
        return row

    async def pending_for_agent(self, agent: str) -> list[dict]:
        rows = await self.db.fetchall(
            "SELECT * FROM confirmations WHERE agent = ? AND status = 'pending' ORDER BY requested_at DESC",
            (agent,),
        )
        for r in rows:
            r["arguments"] = loads(r["arguments"], {})
        return rows
