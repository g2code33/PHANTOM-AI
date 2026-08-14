"""Health Memory Vault — encrypted, strictly separated health records.

Design:
- Health data NEVER becomes general Phantom memory. It lives in its own table,
  encrypted at rest with Fernet (key stored in the chmod-600 secrets store).
- Only the Health brain (and the user, via the Health Center / API) can access
  it. Other brains have no path to it.
- Records are only ever what the user provides — nothing is invented.
- Supports: view, correct (update), delete individual records, delete by
  category, export, clear all, disable.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Optional

from cryptography.fernet import Fernet, InvalidToken

from ..config import now_iso
from ..storage.db import Database

CATEGORIES = ("measurement", "symptom", "medication", "appointment", "goal",
              "habit", "routine", "note")
CATEGORY_LABELS = {
    "measurement": "Measurements", "symptom": "Symptoms", "medication": "Medications",
    "appointment": "Appointments", "goal": "Health goals", "habit": "Habits",
    "routine": "Routines", "note": "Notes",
}


class HealthVault:
    def __init__(self, db: Database, secrets: Any) -> None:
        self.db = db
        self.secrets = secrets
        self._fernet: Optional[Fernet] = None

    # ---- encryption ------------------------------------------------------
    def _key(self) -> bytes:
        key = self.secrets.get("HEALTH_VAULT_KEY")
        if not key:
            key = Fernet.generate_key().decode()
            self.secrets.set("HEALTH_VAULT_KEY", key)
        return key.encode()

    @property
    def fernet(self) -> Fernet:
        if self._fernet is None:
            self._fernet = Fernet(self._key())
        return self._fernet

    def _encrypt(self, payload: dict) -> str:
        return self.fernet.encrypt(json.dumps(payload, default=str).encode()).decode()

    def _decrypt(self, token: str) -> dict:
        try:
            return json.loads(self.fernet.decrypt(token.encode()).decode())
        except InvalidToken:
            raise PermissionError("health record cannot be decrypted (vault key changed?)") from None

    # ---- CRUD -------------------------------------------------------------
    async def add(self, category: str, data: dict[str, Any],
                  title: str = "") -> dict[str, Any]:
        if category not in CATEGORIES:
            raise ValueError(f"unknown health category: {category}")
        rid = uuid.uuid4().hex
        ts = now_iso()
        payload = {"data": data, "recorded_at": ts}
        await self.db.execute(
            "INSERT INTO health_records (id, category, title, payload, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (rid, category, title[:200], self._encrypt(payload), ts, ts),
        )
        return await self.get(rid)

    async def get(self, record_id: str) -> Optional[dict[str, Any]]:
        row = await self.db.fetchone(
            "SELECT * FROM health_records WHERE id = ?", (record_id,))
        if not row:
            return None
        payload = self._decrypt(row["payload"])
        return {
            "id": row["id"], "category": row["category"], "title": row["title"],
            "data": payload.get("data", {}), "recorded_at": payload.get("recorded_at"),
            "created_at": row["created_at"],
        }

    async def update(self, record_id: str, data: dict[str, Any],
                     title: str | None = None) -> Optional[dict[str, Any]]:
        row = await self.db.fetchone(
            "SELECT * FROM health_records WHERE id = ?", (record_id,))
        if not row:
            return None
        payload = self._decrypt(row["payload"])
        payload["data"] = {**payload.get("data", {}), **data}
        ts = now_iso()
        await self.db.execute(
            "UPDATE health_records SET payload = ?, title = COALESCE(?, title), updated_at = ?"
            " WHERE id = ?",
            (self._encrypt(payload), title, ts, record_id))
        return await self.get(record_id)

    async def delete(self, record_id: str) -> bool:
        cur = await self.db.conn.execute(
            "DELETE FROM health_records WHERE id = ?", (record_id,))
        await self.db.conn.commit()
        return cur.rowcount > 0

    async def clear(self, category: str = "") -> int:
        if category and category not in CATEGORIES:
            raise ValueError(f"unknown health category: {category}")
        if category:
            cur = await self.db.conn.execute(
                "DELETE FROM health_records WHERE category = ?", (category,))
        else:
            cur = await self.db.conn.execute("DELETE FROM health_records")
        await self.db.conn.commit()
        return cur.rowcount

    # ---- queries ----------------------------------------------------------
    async def list(self, category: str = "", limit: int = 200,
                   offset: int = 0) -> list[dict[str, Any]]:
        sql = "SELECT * FROM health_records"
        params: list[Any] = []
        if category:
            sql += " WHERE category = ?"
            params.append(category)
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params += [min(limit, 500), offset]
        rows = await self.db.fetchall(sql, tuple(params))
        out = []
        for row in rows:
            try:
                payload = self._decrypt(row["payload"])
            except PermissionError:
                continue
            out.append({
                "id": row["id"], "category": row["category"], "title": row["title"],
                "data": payload.get("data", {}),
                "recorded_at": payload.get("recorded_at") or row["created_at"],
                "created_at": row["created_at"],
            })
        return out

    async def since(self, category: str = "", cutoff_iso: str = "") -> list[dict[str, Any]]:
        rows = await self.list(category, limit=1000)
        return [r for r in rows if (r["recorded_at"] or "") >= cutoff_iso]

    async def search(self, query: str, limit: int = 50) -> list[dict[str, Any]]:
        q = query.lower()
        out = []
        for row in await self.list(limit=1000):
            blob = (row.get("title") or "") + " " + json.dumps(row.get("data", {}), default=str)
            if q in blob.lower():
                out.append(row)
            if len(out) >= limit:
                break
        return out

    async def export(self) -> dict[str, Any]:
        records = await self.list(limit=100000)
        return {
            "exported_at": now_iso(),
            "app": "PHANTOM + CODED",
            "vault": "health",
            "records": records,
            "count": len(records),
        }

    async def counts(self) -> dict[str, int]:
        rows = await self.db.fetchall(
            "SELECT category, COUNT(*) AS n FROM health_records GROUP BY category")
        return {r["category"]: r["n"] for r in rows}

    async def total(self) -> int:
        return int(await self.db.scalar("SELECT COUNT(*) FROM health_records") or 0)
