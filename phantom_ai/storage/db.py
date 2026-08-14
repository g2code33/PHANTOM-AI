"""SQLite storage core: connection management, schema, and small query helpers.

Single database file. FTS5 powers full-text conversation search.
Everything is async (aiosqlite) so the event loop is never blocked.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    agent      TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at TEXT,
    PRIMARY KEY (agent, key)
);

CREATE TABLE IF NOT EXISTS conversations (
    id         TEXT PRIMARY KEY,
    agent      TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT 'New conversation',
    summary    TEXT,
    archived   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_conversations_agent ON conversations(agent, updated_at);

CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    uid           TEXT NOT NULL UNIQUE,
    conversation_id TEXT NOT NULL,
    agent         TEXT NOT NULL,
    msg_index     INTEGER NOT NULL,
    role          TEXT NOT NULL,
    content       TEXT,
    tool_calls    TEXT,
    tool_results  TEXT,
    session_id    TEXT,
    created_at    TEXT,
    UNIQUE (conversation_id, msg_index)
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, msg_index);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(content);

CREATE TABLE IF NOT EXISTS memories (
    id             TEXT PRIMARY KEY,
    agent          TEXT NOT NULL,
    kind           TEXT NOT NULL DEFAULT 'fact',
    content        TEXT NOT NULL,
    source_conversation TEXT,
    importance     REAL NOT NULL DEFAULT 0.5,
    tags           TEXT,
    status         TEXT NOT NULL DEFAULT 'active',
    created_at     TEXT,
    updated_at     TEXT,
    last_used_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_memories_agent ON memories(agent, status);

CREATE TABLE IF NOT EXISTS audit_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT,
    agent     TEXT,
    event     TEXT,
    detail    TEXT,
    latency_ms REAL,
    model     TEXT,
    tokens    TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_audit_agent ON audit_log(agent, ts);

CREATE TABLE IF NOT EXISTS tasks (
    id         TEXT PRIMARY KEY,
    agent      TEXT,
    name       TEXT,
    kind       TEXT,
    status     TEXT NOT NULL DEFAULT 'queued',
    created_at TEXT,
    started_at TEXT,
    ended_at   TEXT,
    error      TEXT,
    logs       TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_agent ON tasks(agent, status);

CREATE TABLE IF NOT EXISTS delegations (
    id           TEXT PRIMARY KEY,
    task_id      TEXT,
    origin       TEXT,
    target       TEXT,
    objective    TEXT,
    context      TEXT,
    timeout_sec  INTEGER,
    status       TEXT NOT NULL DEFAULT 'queued',
    result       TEXT,
    error        TEXT,
    created_at   TEXT,
    started_at   TEXT,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_delegations_origin ON delegations(origin, created_at);
CREATE INDEX IF NOT EXISTS idx_delegations_target ON delegations(target, created_at);

CREATE TABLE IF NOT EXISTS schedules (
    id         TEXT PRIMARY KEY,
    agent      TEXT,
    name       TEXT,
    expression TEXT,
    prompt     TEXT,
    quiet_start TEXT,
    quiet_end  TEXT,
    enabled    INTEGER NOT NULL DEFAULT 1,
    last_run_at TEXT,
    next_run_at TEXT,
    last_status TEXT,
    last_result TEXT
);

CREATE TABLE IF NOT EXISTS notifications (
    id         TEXT PRIMARY KEY,
    agent      TEXT,
    title      TEXT,
    body       TEXT,
    created_at TEXT,
    read       INTEGER NOT NULL DEFAULT 0,
    dismissed  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_notifications_read ON notifications(read, created_at);

CREATE TABLE IF NOT EXISTS confirmations (
    id         TEXT PRIMARY KEY,
    agent      TEXT,
    conversation_id TEXT,
    tool_name  TEXT,
    arguments  TEXT,
    reason     TEXT,
    impact     TEXT,
    risk       TEXT,
    status     TEXT NOT NULL DEFAULT 'pending',
    requested_at TEXT,
    decided_at TEXT,
    decided_by TEXT
);

-- ---- Evolution & System Intelligence --------------------------------------

CREATE TABLE IF NOT EXISTS brains (
    id            TEXT PRIMARY KEY,
    name          TEXT,
    role          TEXT,
    description   TEXT,
    system_prompt TEXT,
    model         TEXT,
    provider      TEXT DEFAULT 'nvidia',
    key_env       TEXT,
    tools         TEXT,
    memory_scope  TEXT DEFAULT 'own',
    permissions   TEXT,
    input_schema  TEXT,
    output_schema TEXT,
    routing_rules TEXT,
    dependencies  TEXT,
    verification  TEXT,
    version       INTEGER DEFAULT 1,
    status        TEXT DEFAULT 'active',
    stats         TEXT,
    health        TEXT,
    created_at    TEXT,
    updated_at    TEXT
);

CREATE TABLE IF NOT EXISTS graph_nodes (
    id         TEXT PRIMARY KEY,
    type       TEXT NOT NULL,
    label      TEXT NOT NULL,
    properties TEXT,
    agent      TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_nodes_type ON graph_nodes(type);
CREATE INDEX IF NOT EXISTS idx_nodes_label ON graph_nodes(label);

CREATE TABLE IF NOT EXISTS graph_edges (
    id         TEXT PRIMARY KEY,
    source     TEXT NOT NULL,
    target     TEXT NOT NULL,
    relation   TEXT NOT NULL,
    properties TEXT,
    weight     REAL DEFAULT 1.0,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_edges_source ON graph_edges(source);
CREATE INDEX IF NOT EXISTS idx_edges_target ON graph_edges(target);
CREATE INDEX IF NOT EXISTS idx_edges_relation ON graph_edges(relation);

CREATE TABLE IF NOT EXISTS proposals (
    id          TEXT PRIMARY KEY,
    title       TEXT,
    description TEXT,
    kind        TEXT DEFAULT 'config',
    changes     TEXT,
    risk        TEXT DEFAULT 'low',
    status      TEXT DEFAULT 'proposed',
    created_by  TEXT,
    snapshot_id TEXT,
    tests       TEXT,
    result      TEXT,
    created_at  TEXT,
    decided_at  TEXT,
    deployed_at TEXT
);

CREATE TABLE IF NOT EXISTS snapshots (
    id          TEXT PRIMARY KEY,
    label       TEXT,
    description TEXT,
    kind        TEXT DEFAULT 'config',
    data        TEXT,
    previous_id TEXT,
    created_at  TEXT,
    restored_at TEXT
);

-- ---- Health Brain (encrypted vault) ---------------------------------------

CREATE TABLE IF NOT EXISTS health_records (
    id         TEXT PRIMARY KEY,
    category   TEXT NOT NULL,
    title      TEXT,
    payload    TEXT NOT NULL,      -- Fernet-encrypted JSON
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_health_category ON health_records(category, created_at);
"""


class Database:
    """Thin async wrapper around aiosqlite with dict rows + helpers."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(SCHEMA)
        await self._migrate_fts()
        await self._conn.commit()

    async def _migrate_fts(self) -> None:
        """v0→v1: FTS table was contentless (snippet() unusable) — rebuild it."""
        row = await self.fetchone(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='messages_fts'")
        if row and "content='" in (row.get("sql") or ""):
            await self.conn.executescript(
                "DROP TABLE IF EXISTS messages_fts;"
                "CREATE VIRTUAL TABLE messages_fts USING fts5(content);"
            )
            await self.conn.execute(
                "INSERT INTO messages_fts (rowid, content)"
                " SELECT id, content FROM messages WHERE content IS NOT NULL")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database not connected"
        return self._conn

    async def execute(self, sql: str, params: tuple = ()) -> None:
        await self.conn.execute(sql, params)
        await self.conn.commit()

    async def executemany(self, sql: str, seq: list[tuple]) -> None:
        await self.conn.executemany(sql, seq)
        await self.conn.commit()

    async def executescript(self, sql: str) -> None:
        await self.conn.executescript(sql)
        await self.conn.commit()

    async def fetchone(self, sql: str, params: tuple = ()) -> Optional[dict]:
        cur = await self.conn.execute(sql, params)
        row = await cur.fetchone()
        await cur.close()
        return dict(row) if row else None

    async def fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        cur = await self.conn.execute(sql, params)
        rows = await cur.fetchall()
        await cur.close()
        return [dict(r) for r in rows]

    async def scalar(self, sql: str, params: tuple = ()) -> Any:
        row = await self.fetchone(sql, params)
        if row is None:
            return None
        return next(iter(row.values()))


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=str, ensure_ascii=False)


def loads(text: Optional[str], default: Any = None) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return default
