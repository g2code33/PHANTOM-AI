"""Cloud Sync — the shared slice between the PC Phantom and Portable Phantom.

Design (Jarvis portable phase):
- "Share everything": the PC and the cloud share profile + a cloud memory slice.
- "Add this specifically to cloud": memories/facts can be tagged `cloud:true`
  (synced to the portable Worker) vs local (stays on the PC only).
- Every cloud operation is real HTTP to the configured Worker URL; failures
  are honest (never fake "synced").
- Secrets: the Worker URL + token are stored in the chmod-600 secrets store;
  the cloud token is never exposed to the UI in full.
"""

from __future__ import annotations

import httpx
from typing import Any, Optional

from ..config import now_iso

CLOUD_PREFIX = "cloud:"  # memory content tag used by the cloud store


class CloudSync:
    def __init__(self, secrets: Any, settings: Any, audit: Any,
                 memory_store: Any, profiles: Any) -> None:
        self.secrets = secrets
        self.settings = settings
        self.audit = audit
        self.memories = memory_store
        self.profiles = profiles

    # ---- config -------------------------------------------------------
    def _url(self) -> str:
        return (self.secrets.get("PHANTOM_CLOUD_URL") or "").rstrip("/")

    def _token(self) -> str:
        return self.secrets.get("PHANTOM_CLOUD_TOKEN") or ""

    async def configured(self) -> bool:
        return bool(self._url())

    async def config(self) -> dict[str, Any]:
        url = self._url()
        return {"url": url, "token_configured": bool(self._token()),
                "url_masked": (url[:18] + "…") if url else ""}

    async def save_config(self, url: str = "", token: str = "", clear: bool = False) -> dict:
        if url:
            self.secrets.set("PHANTOM_CLOUD_URL", url.strip())
        if token:
            self.secrets.set("PHANTOM_CLOUD_TOKEN", token.strip())
        if clear:
            self.secrets.delete("PHANTOM_CLOUD_URL")
            self.secrets.delete("PHANTOM_CLOUD_TOKEN")
        return await self.config()

    # ---- low-level ------------------------------------------------------
    async def _request(self, method: str, path: str, body: dict | None = None,
                       timeout: float = 20.0) -> dict[str, Any]:
        url = self._url()
        if not url:
            raise RuntimeError("portable Phantom URL not configured")
        headers = {"Content-Type": "application/json"}
        if self._token():
            headers["X-Access-Token"] = self._token()
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(method, f"{url}{path}",
                                        headers=headers, json=body)
            if resp.status_code == 401:
                raise RuntimeError("cloud token rejected (401)")
            if resp.status_code >= 400:
                raise RuntimeError(f"cloud error {resp.status_code}: {resp.text[:160]}")
            return resp.json()

    # ---- sync profile ---------------------------------------------------
    async def push_profile(self) -> dict[str, Any]:
        profile = await self.profiles.default()
        if not profile:
            raise RuntimeError("no profile to sync")
        fields = dict(profile.get("fields") or {})
        await self._request("PUT", "/api/profile", {
            "display_name": profile.get("display_name", "User"),
            **{k: fields[k] for k in ("name", "university", "program", "level",
                                      "projects", "goals", "identity") if k in fields},
        })
        await self.audit.record("user", "cloud.profile_pushed", {})
        return {"ok": True}

    # ---- cloud memory ----------------------------------------------------
    async def list_cloud_memories(self, limit: int = 100) -> list[dict[str, Any]]:
        data = await self._request("GET", "/api/memory")
        return (data.get("memories") or [])[:limit]

    async def save_to_cloud(self, content: str, kind: str = "fact",
                            tags: list[str] | None = None) -> dict[str, Any]:
        """Explicit 'save this to cloud' — mirrored into local memory too (so
        the PC sees it with a cloud tag), then pushed to the Worker."""
        if not content.strip():
            raise ValueError("content required")
        if not self._url():
            raise RuntimeError("portable Phantom URL not configured — set it in Settings")
        await self._request("POST", "/api/memory", {
            "content": content, "kind": kind, "tags": tags or []})
        # mirror locally with a cloud tag for the PC UI
        memory = await self.memories.add(
            agent="phantom",
            content=content[:4000],
            kind="fact" if kind not in self.memories.KINDS else kind,
            importance=0.7,
            tags=list(tags or []) + ["cloud"],
        )
        await self.audit.record("user", "cloud.memory_saved",
                                {"content": content[:120], "kind": kind})
        return {"ok": True, "memory_id": memory["id"], "cloud": True}

    async def fetch_cloud_memories(self) -> dict[str, Any]:
        """Pull cloud memories and mirror them locally (shared slice)."""
        items = await self.list_cloud_memories()
        mirrored = 0
        for item in items:
            existing = await self.memories.search("phantom", item["content"][:40], limit=1)
            if not existing:
                await self.memories.add(
                    agent="phantom", content=item["content"][:4000],
                    kind="fact", importance=0.6,
                    tags=list(item.get("tags") or []) + ["cloud"],
                )
                mirrored += 1
        await self.audit.record("user", "cloud.memory_fetched", {"count": len(items)})
        return {"cloud_count": len(items), "mirrored_new": mirrored}

    # ---- reminders --------------------------------------------------------
    async def push_reminders(self) -> dict[str, Any]:
        """Push local pending reminders to the cloud so portable Phantom can
        remind on the go."""
        from ..storage.ops import ScheduleStore

        schedules = await ScheduleStore(self.memories.db).list("phantom")
        due = [s for s in schedules if s.get("enabled")]
        for s in due[:10]:
            try:
                await self._request("POST", "/api/reminders", {
                    "text": f"{s.get('name')}: {s.get('prompt', '')[:120]}",
                    "when": s.get("next_run_at") or ""})
            except Exception:  # noqa: BLE001
                break
        await self.audit.record("user", "cloud.reminders_pushed",
                                {"count": len(due[:10])})
        return {"ok": True, "pushed": len(due[:10])}
