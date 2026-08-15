"""Profile sheet — the user's editable identity that Phantom & Coded know.

Jarvis Phase 1 (seed): JOOJO's profile is pre-filled so both agents already know
him. Supports multiple profiles (e.g. a new person's profile); agents can add
fields via profile tools (Phase 4); everything is editable/deletable and stored
locally.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from ..config import now_iso
from ..storage.db import Database, dumps, loads

JOOJO_SEED: dict[str, Any] = {
    "name": "Blessing Jojo Ewusi",
    "display_name": "JOOJO",
    "nickname": "JOOJO",
    "location": "Ghana",
    "university": "University of Cape Coast (UCC)",
    "school": "School of Pharmacy and Pharmaceutical Sciences (SOPPS)",
    "program": "Doctor of Pharmacy (PharmD)",
    "level": "200",
    "clinical_context": "Clinical rotations incl. Afrancho Polyclinic",
    "identity": ("PharmD student becoming a technology builder — pharmacy × "
                 "coding × AI × digital health × entrepreneurship"),
    "organizations": ["Code Rx Society (founder — 'Coding the Future of Pharmacy')"],
    "projects": [
        "PharmaGAME (pharmaGAME — Calcitonin v1, gamified pharmacy learning)",
        "PharmaQUIZ", "PharmaTRACK", "Cure Link", "TAWOMO",
        "Pharmacy Management System (Ghana-focused, MoMo/Paystack/Hubtel)",
        "KICK LIVE / Rx Live (sports/tournament platform, UCC GPSA pharmacy league)",
        "RxStore (app distribution ecosystem)", "Clinicals app (offline/online)",
        "Phantom (this AI)",
    ],
    "learning_style": ("mechanistic, step-by-step, cause→effect, wants 'what "
                       "happens next', direct then deep, practice questions"),
    "study_topics": ["pharmacology", "pathology", "physiology", "microbiology",
                     "pharmacognosy", "pharmaceutical chemistry", "pharmaceutics"],
    "design_preferences": ("clean, modern, premium; yellow #FFD600 + white; "
                           "Rod of Asclepius; mobile-friendly; no fake UI"),
    "goals": ["build useful products", "make money while in school",
              "independence through tech", "Code Rx ecosystem"],
    "communication": ("address by name (JOOJO, not sir); direct; honest; "
                      "never fake success; always suggest next best action"),
    "working_style": ("iterative builder: idea → test → criticize → layer → "
                      "redesign; dislikes fake functionality"),
}


class ProfileStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def _ensure_table(self) -> None:
        await self.db.execute(
            "CREATE TABLE IF NOT EXISTS profiles ("
            " id TEXT PRIMARY KEY,"
            " name TEXT,"
            " display_name TEXT,"
            " fields TEXT,"
            " created_at TEXT,"
            " updated_at TEXT)")

    async def list(self) -> list[dict[str, Any]]:
        await self._ensure_table()
        rows = await self.db.fetchall("SELECT * FROM profiles ORDER BY updated_at DESC")
        for r in rows:
            r["fields"] = loads(r["fields"], {})
        return rows

    async def get(self, profile_id: str) -> Optional[dict[str, Any]]:
        await self._ensure_table()
        row = await self.db.fetchone("SELECT * FROM profiles WHERE id = ?", (profile_id,))
        if row:
            row["fields"] = loads(row["fields"], {})
        return row

    async def default(self) -> Optional[dict[str, Any]]:
        await self._ensure_table()
        row = await self.db.fetchone(
            "SELECT * FROM profiles ORDER BY updated_at DESC LIMIT 1")
        if row:
            row["fields"] = loads(row["fields"], {})
        return row

    async def create(self, name: str, display_name: str = "",
                     fields: dict[str, Any] | None = None) -> dict[str, Any]:
        await self._ensure_table()
        pid = uuid.uuid4().hex
        ts = now_iso()
        await self.db.execute(
            "INSERT INTO profiles (id, name, display_name, fields, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (pid, name, display_name or name, dumps(fields or {}), ts, ts),
        )
        return await self.get(pid)  # type: ignore[return-value]

    async def update(self, profile_id: str, fields: dict[str, Any],
                     display_name: str | None = None) -> Optional[dict[str, Any]]:
        await self._ensure_table()
        existing = await self.get(profile_id)
        if not existing:
            return None
        merged = {**existing["fields"], **fields}
        await self.db.execute(
            "UPDATE profiles SET fields = ?, display_name = COALESCE(?, display_name),"
            " updated_at = ? WHERE id = ?",
            (dumps(merged), display_name, now_iso(), profile_id),
        )
        return await self.get(profile_id)  # type: ignore[return-value]

    async def delete(self, profile_id: str) -> bool:
        await self._ensure_table()
        cur = await self.db.conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        await self.db.conn.commit()
        return cur.rowcount > 0

    async def seed_joojo(self) -> dict[str, Any]:
        """Create the JOOJO profile if no profile exists yet."""
        await self._ensure_table()
        existing = await self.default()
        if existing:
            return existing
        profile = await self.create(
            name=JOOJO_SEED["name"],
            display_name=JOOJO_SEED["display_name"],
            fields=JOOJO_SEED,
        )
        return profile  # type: ignore[return-value]

    # ------------------------------------------------------------------
    def compact(self, profile: dict[str, Any], max_chars: int = 1600) -> str:
        """Build a compact, readable profile block for context injection."""
        fields = profile.get("fields") or {}
        lines = [f"{profile.get('display_name') or profile.get('name') or 'User'}"]
        ordered_keys = [
            "nickname", "location", "university", "school", "program", "level",
            "clinical_context", "identity", "organizations", "projects",
            "study_topics", "learning_style", "design_preferences", "goals",
            "communication", "working_style",
        ]
        used = set()
        for key in ordered_keys:
            val = fields.get(key)
            if val is None or key in used:
                continue
            used.add(key)
            if isinstance(val, list):
                val = ", ".join(str(v) for v in val[:8])
            if isinstance(val, str) and val.strip():
                lines.append(f"{key.replace('_', ' ').title()}: {val.strip()[:300]}")
        # any extra custom fields the user/agent added
        for key, val in fields.items():
            if key in used or key in ("name", "display_name"):
                continue
            if isinstance(val, list):
                val = ", ".join(str(v) for v in val[:8])
            if isinstance(val, str) and val.strip():
                lines.append(f"{key.replace('_', ' ').title()}: {val.strip()[:300]}")
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n…(profile truncated)"
        return text
