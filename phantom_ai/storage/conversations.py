"""Conversation + message storage with FTS5 search. Also the memory store."""

from __future__ import annotations

import uuid
from typing import Any, Optional

from .db import Database, dumps, loads

# ---------------------------------------------------------------------------
# Conversations & messages
# ---------------------------------------------------------------------------


class ConversationStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, agent: str, title: str = "New conversation", conversation_id: str | None = None) -> dict:
        from ..config import now_iso

        cid = conversation_id or uuid.uuid4().hex
        ts = now_iso()
        await self.db.execute(
            "INSERT INTO conversations (id, agent, title, created_at, updated_at) VALUES (?,?,?,?,?)",
            (cid, agent, title, ts, ts),
        )
        return {"id": cid, "agent": agent, "title": title, "created_at": ts}

    async def get(self, conversation_id: str) -> Optional[dict]:
        return await self.db.fetchone(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
        )

    async def list_for_agent(self, agent: str, limit: int = 100, include_archived: bool = False) -> list[dict]:
        archived_sql = "" if include_archived else " AND archived = 0"
        return await self.db.fetchall(
            "SELECT id, agent, title, summary, created_at, updated_at, archived,"
            " (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = conversations.id) AS message_count"
            " FROM conversations WHERE agent = ?" + archived_sql +
            " ORDER BY updated_at DESC LIMIT ?",
            (agent, limit),
        )

    async def set_title(self, conversation_id: str, title: str) -> None:
        await self.db.execute(
            "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
            (title[:200], from_ts(), conversation_id),
        )

    async def set_summary(self, conversation_id: str, summary: str) -> None:
        await self.db.execute(
            "UPDATE conversations SET summary = ? WHERE id = ?", (summary, conversation_id)
        )

    async def archive(self, conversation_id: str, archived: bool = True) -> None:
        await self.db.execute(
            "UPDATE conversations SET archived = ? WHERE id = ?", (1 if archived else 0, conversation_id)
        )

    async def delete(self, conversation_id: str) -> None:
        await self.db.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
        await self.db.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))

    # -- messages -----------------------------------------------------------

    async def append_message(
        self,
        conversation_id: str,
        agent: str,
        role: str,
        content: str | None,
        tool_calls: Any = None,
        tool_results: Any = None,
        session_id: str | None = None,
    ) -> dict:
        from ..config import now_iso

        idx = await self.db.scalar(
            "SELECT COALESCE(MAX(msg_index), -1) + 1 FROM messages WHERE conversation_id = ?",
            (conversation_id,),
        )
        uid = uuid.uuid4().hex
        ts = now_iso()
        await self.db.execute(
            "INSERT INTO messages (uid, conversation_id, agent, msg_index, role, content,"
            " tool_calls, tool_results, session_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (uid, conversation_id, agent, idx, role, content, dumps(tool_calls), dumps(tool_results),
             session_id, ts),
        )
        await self.db.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?", (ts, conversation_id)
        )
        if content and len(content) > 8:
            await self.db.execute(
                "INSERT INTO messages_fts (rowid, content) VALUES (?, ?)",
                (await self.db.scalar("SELECT id FROM messages WHERE uid = ?", (uid,)), content),
            )
        return {"id": uid, "conversation_id": conversation_id, "role": role, "content": content}

    async def get_messages(self, conversation_id: str, limit: int = 200, offset: int = 0) -> list[dict]:
        return await self.db.fetchall(
            "SELECT uid AS id, conversation_id, agent, role, content, tool_calls, tool_results,"
            " session_id, created_at FROM messages WHERE conversation_id = ?"
            " ORDER BY msg_index ASC LIMIT ? OFFSET ?",
            (conversation_id, limit, offset),
        )

    async def count_messages(self, conversation_id: str) -> int:
        return await self.db.scalar(
            "SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (conversation_id,)
        )

    # -- search -------------------------------------------------------------

    async def search(self, agent: str, query: str, limit: int = 10) -> list[dict]:
        """FTS5 search across an agent's archived conversations. Returns message snippets."""
        cleaned = _fts_query(query)
        if not cleaned:
            return []
        rows = await self.db.fetchall(
            "SELECT m.uid AS message_id, m.conversation_id, m.agent, m.role, m.content, m.created_at,"
            " c.title AS conversation_title,"
            " COALESCE(snippet(messages_fts, 0, '[', ']', '…', 12), substr(m.content, 1, 160)) AS snippet"
            " FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid"
            " JOIN conversations c ON c.id = m.conversation_id"
            " WHERE messages_fts MATCH ? AND m.agent = ? AND m.role IN ('user','assistant')"
            " ORDER BY rank LIMIT ?",
            (cleaned, agent, limit),
        )
        return rows

    async def search_all_agents(self, query: str, limit: int = 10) -> list[dict]:
        cleaned = _fts_query(query)
        if not cleaned:
            return []
        return await self.db.fetchall(
            "SELECT m.uid AS message_id, m.conversation_id, m.agent, m.role, m.content, m.created_at,"
            " c.title AS conversation_title,"
            " COALESCE(snippet(messages_fts, 0, '[', ']', '…', 12), substr(m.content, 1, 160)) AS snippet"
            " FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid"
            " JOIN conversations c ON c.id = m.conversation_id"
            " WHERE messages_fts MATCH ? AND m.role IN ('user','assistant')"
            " ORDER BY rank LIMIT ?",
            (cleaned, limit),
        )


def _fts_query(raw: str) -> str:
    """Turn free text into a safe FTS5 MATCH expression."""
    tokens = [t for t in raw.replace('"', " ").split() if len(t) > 1]
    if not tokens:
        return ""
    cleaned = [t.replace("'", "").replace('"', "") for t in tokens[:8]]
    return " OR ".join(f'"{t}"' for t in cleaned)


def from_ts() -> str:
    from ..config import now_iso

    return now_iso()


# ---------------------------------------------------------------------------
# Memories (durable long-term memory, per agent + shared)
# ---------------------------------------------------------------------------


class MemoryStore:
    KINDS = {"fact", "preference", "project", "decision", "technical"}

    def __init__(self, db: Database) -> None:
        self.db = db

    async def add(
        self,
        agent: str,
        content: str,
        kind: str = "fact",
        importance: float = 0.5,
        tags: list[str] | None = None,
        source_conversation: str | None = None,
    ) -> dict:
        from ..config import now_iso

        if kind not in self.KINDS:
            kind = "fact"
        importance = max(0.0, min(1.0, float(importance)))
        mid = uuid.uuid4().hex
        ts = now_iso()
        await self.db.execute(
            "INSERT INTO memories (id, agent, kind, content, source_conversation, importance, tags,"
            " status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (mid, agent, kind, content[:4000], source_conversation, importance,
             dumps(tags or []), "active", ts, ts),
        )
        return await self.get(mid)

    async def get(self, memory_id: str) -> Optional[dict]:
        row = await self.db.fetchone("SELECT * FROM memories WHERE id = ?", (memory_id,))
        if row:
            row["tags"] = loads(row["tags"], [])
        return row

    async def update(self, memory_id: str, **fields: Any) -> Optional[dict]:
        allowed = {"content", "kind", "importance", "tags", "status"}
        sets, params = [], []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k} = ?")
                params.append(dumps(v) if k == "tags" else v)
        if not sets:
            return await self.get(memory_id)
        sets.append("updated_at = ?")
        params.append(from_ts())
        params.append(memory_id)
        await self.db.execute(f"UPDATE memories SET {', '.join(sets)} WHERE id = ?", tuple(params))
        return await self.get(memory_id)

    async def deactivate(self, memory_id: str) -> None:
        await self.update(memory_id, status="forgotten")

    async def list(self, agent: str | None = None, kind: str | None = None, status: str = "active",
                   limit: int = 200) -> list[dict]:
        sql = "SELECT * FROM memories WHERE status = ?"
        params: list[Any] = [status]
        if agent:
            if agent == "shared":
                sql += " AND agent = 'shared'"
            else:
                sql += " AND (agent = ? OR agent = 'shared')"
                params.append(agent)
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY importance DESC, updated_at DESC LIMIT ?"
        params.append(limit)
        rows = await self.db.fetchall(sql, tuple(params))
        for r in rows:
            r["tags"] = loads(r["tags"], [])
        return rows

    async def search(self, agent: str, query: str, limit: int = 12) -> list[dict]:
        q = f"%{query}%"
        rows = await self.db.fetchall(
            "SELECT * FROM memories WHERE status = 'active' AND (agent = ? OR agent = 'shared')"
            " AND content LIKE ? ORDER BY importance DESC, updated_at DESC LIMIT ?",
            (agent, q, limit),
        )
        for r in rows:
            r["tags"] = loads(r["tags"], [])
        return rows

    async def touch(self, memory_id: str) -> None:
        await self.db.execute(
            "UPDATE memories SET last_used_at = ? WHERE id = ?", (from_ts(), memory_id)
        )

    async def hard_delete(self, memory_id: str) -> None:
        await self.db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
