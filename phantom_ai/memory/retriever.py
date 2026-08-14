"""Intelligent memory retrieval: build a compact context package for each
agent turn.

- Retrieves relevant durable facts (own + shared), ranked by importance,
  recency and keyword match.
- Retrieves relevant PAST conversations (full-text search) with snippets.
- Includes the current conversation's rolling summary when one exists.
- Caps total size so we never dump the entire lifetime conversation into the
  model prompt.

Memory is always presented as DATA blocks the model is told to treat as data.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from ..config import SecretRedactor

MAX_FACTS = 12
MAX_HISTORY = 3
MAX_FACT_CHARS = 300
MAX_PACKAGE_CHARS = 6000


class MemoryRetriever:
    def __init__(self, store: Any, conversation_store: Any, settings: Any) -> None:
        self.store = store
        self.conversation_store = conversation_store
        self.settings = settings

    async def build_context_package(self, agent_id: str, user_text: str,
                                    conversation_id: str | None) -> str:
        max_facts = int(await self.settings.get("memory.max_facts", agent_id, MAX_FACTS))
        package_chars = int(await self.settings.get("memory.package_chars", agent_id, MAX_PACKAGE_CHARS))

        blocks: list[str] = []

        facts = await self._ranked_facts(agent_id, user_text, max_facts)
        if facts:
            lines = []
            for f in facts:
                scope = "shared" if f["agent"] == "shared" else "own"
                content = SecretRedactor.redact(f["content"])[:MAX_FACT_CHARS]
                lines.append(f"[{scope}·{f['kind']}] {content}")
            blocks.append("<memory>\n" + "\n".join(lines) + "\n</memory>")

        summary = None
        if conversation_id:
            conv = await self.conversation_store.get(conversation_id)
            if conv and conv.get("summary"):
                summary = SecretRedactor.redact(conv["summary"])[:1500]
        if summary:
            blocks.append(f"<history summary of current conversation>\n{summary}\n</history>")

        related = await self._related_conversations(agent_id, user_text)
        if related:
            lines = []
            for r in related:
                date = (r.get("created_at") or "")[:10]
                lines.append(
                    f"· {r['conversation_title']} ({date}): {SecretRedactor.redact(r['snippet'])[:280]}"
                )
            blocks.append("<history related past conversations>\n" + "\n".join(lines) + "\n</history>")

        package = "\n\n".join(blocks)
        if len(package) > package_chars:
            package = package[:package_chars] + "\n…(memory package truncated)"
        return package

    async def _ranked_facts(self, agent_id: str, user_text: str, limit: int) -> list[dict]:
        facts = await self.store.list(agent=agent_id, limit=500)
        if not facts:
            return []
        keywords = {w.lower() for w in re.findall(r"[a-zA-Z0-9_]{4,}", user_text or "")}
        scored = []
        for f in facts:
            content = (f["content"] or "").lower()
            kw_hits = sum(1 for w in keywords if w in content)
            score = (
                float(f["importance"]) * 0.6
                + (0.2 if f["status"] == "active" else 0)
                + min(kw_hits, 5) * 0.08
            )
            scored.append((score, f))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [f for _, f in scored[:limit]]

    async def _related_conversations(self, agent_id: str, user_text: str) -> list[dict]:
        words = [w for w in re.split(r"\W+", user_text or "") if len(w) > 3][:6]
        if not words:
            return []
        query = " OR ".join(f'"{w}"' for w in words)
        try:
            return await self.conversation_store.search(agent_id, query, limit=MAX_HISTORY)
        except Exception:  # noqa: BLE001
            return []
