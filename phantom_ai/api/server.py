"""FastAPI application: REST API + WebSocket event stream + static UI."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

from fastapi import (FastAPI, File, Form, HTTPException, Request, UploadFile,
                     WebSocket, WebSocketDisconnect)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..agents.identities import identity, identity_summary
from ..config import (AGENTS, APP_VERSION, DEEPGRAM_BASE_URL, GROQ_BASE_URL,
                      KEY_ENV, NVIDIA_BASE_URL, UI_DIR, SecretRedactor, mask_key)
from ..permissions.policy import PermissionLevel
from ..tools.base import ToolContext, ToolError
from .app import App

log = logging.getLogger("phantom.api")

# ---- in-app diagnostics: keep the last N log lines so the UI can show what
# ---- went wrong without a terminal (Settings → Diagnostics) ---------------
import collections as _collections

DIAG_LOG = _collections.deque(maxlen=400)


class _DiagLogHandler(logging.Handler):
    def emit(self, record):  # noqa: D102
        try:
            DIAG_LOG.append(self.format(record))
        except Exception:  # noqa: BLE001
            pass


def _init_diag_log() -> None:
    h = _DiagLogHandler()
    h.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    # don't double-add on repeated module imports
    if not any(isinstance(x, _DiagLogHandler) for x in root.handlers):
        root.addHandler(h)


_init_diag_log()

ACCESS_TOKEN = os.environ.get("PHAI_ACCESS_TOKEN", "")


def _check_token(request: Request) -> None:
    if not ACCESS_TOKEN:
        return
    if request.headers.get("X-Access-Token") != ACCESS_TOKEN:
        raise HTTPException(status_code=401, detail="missing or invalid access token")


def _key_test_result(kind: str, resp) -> dict:
    """Map a provider HTTP status to a friendly test result (never the key)."""
    status = resp.status_code
    kind_l = kind.lower()
    if status == 200:
        return {"ok": True, "kind": kind_l, "message": f"✓ {kind} key works"}
    if status in (401, 403):
        return {"ok": False, "kind": kind_l,
                "message": f"✗ {kind} rejected the key (unauthorized)"}
    if status == 402:
        return {"ok": False, "kind": kind_l,
                "message": f"✗ {kind}: no credits / quota exceeded"}
    if status == 429:
        return {"ok": False, "kind": kind_l,
                "message": f"⚠️ {kind} is rate-limited — try again later"}
    return {"ok": False, "kind": kind_l,
            "message": f"✗ {kind} returned an error ({status}) — try again later"}


async def _diagnostic_providers(app: App) -> list[dict]:
    """Resolved provider config per agent (URLs + model — never keys) so the
    user can see in-app why a request 404s (e.g. a wrong base_url)."""
    from ..brains.config import resolve_brain_config

    out = []
    for bid in await app.brain_ids():
        try:
            brain = await app.brains.get(bid) if app.brains else None
            definition = dict(brain) if brain else {}
            cfg = await resolve_brain_config(bid, definition, app.secrets,
                                             app.settings)
            out.append({
                "agent": bid,
                "base_url": cfg.get("base_url", ""),
                "model": cfg.get("model", ""),
                "provider": app.providers.get(bid, None).name if app.providers.get(bid) else "",
            })
        except Exception:  # noqa: BLE001
            continue
    return out


async def _fetch_weather(lat: float, lon: float) -> Optional[dict]:
    """Open-Meteo fetch (module-level seam so tests can monkeypatch it).
    Returns the parsed JSON or None on any failure (honest 'unavailable')."""
    import httpx as _httpx

    url = ("https://api.open-meteo.com/v1/forecast"
           f"?latitude={lat}&longitude={lon}"
           "&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
           "is_day,precipitation,weather_code,wind_speed_10m,surface_pressure"
           "&daily=weather_code,temperature_2m_max,temperature_2m_min,"
           "sunrise,sunset,precipitation_probability_max&timezone=auto"
           "&forecast_days=5")
    try:
        async with _httpx.AsyncClient(timeout=12) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("weather fetch failed: %s", SecretRedactor.redact(str(exc)))
        return None


async def _save_voice_upload(app: App, upload: UploadFile) -> Path:
    """Persist an uploaded mic recording to a temp WAV under the data dir."""
    import uuid

    data = await upload.read()
    if not data:
        raise HTTPException(400, "empty audio upload")
    if len(data) < 44 or data[:4] != b"RIFF":
        raise HTTPException(400, "audio must be a WAV file (RIFF) — got "
                                 f"{data[:4]!r} ({len(data)} bytes)")
    upload_dir = Path(app.data_dir) / "voice_uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    path = upload_dir / f"rec_{uuid.uuid4().hex[:12]}.wav"
    path.write_bytes(data)
    return path


# ---------------------------------------------------------------------------
# request models
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    conversation_id: str = ""
    session_id: str = ""


class ConversationCreate(BaseModel):
    agent: str = "phantom"
    title: str = "New conversation"


class MemoryCreate(BaseModel):
    agent: str = "phantom"
    content: str
    kind: str = "fact"
    importance: float = 0.5
    tags: list[str] = []


class SettingUpdate(BaseModel):
    agent: str = "*"
    key: str
    value: Any = None
    delete: bool = False


class PermissionUpdate(BaseModel):
    agent: str = "phantom"
    tool: str
    level: str | None = None
    delete: bool = False


class ToolRun(BaseModel):
    agent: str = "phantom"
    arguments: dict[str, Any] = {}


class ScheduleCreate(BaseModel):
    agent: str = "phantom"
    name: str
    expression: str
    prompt: str
    quiet_start: str = ""
    quiet_end: str = ""


class ScheduleUpdate(BaseModel):
    name: str | None = None
    expression: str | None = None
    prompt: str | None = None
    quiet_start: str | None = None
    quiet_end: str | None = None
    enabled: bool | None = None


class KillSwitchBody(BaseModel):
    reason: str = "manual"


class ConfirmBody(BaseModel):
    decided_by: str = "user"


class AgentIdBody(BaseModel):
    agent: str = "phantom"


# ---------------------------------------------------------------------------
# app factory
# ---------------------------------------------------------------------------


def create_app(app: App) -> FastAPI:
    fastapi = FastAPI(title="PHANTOM + CODED", version=APP_VERSION, docs_url="/api/docs")

    @fastapi.middleware("http")
    async def token_middleware(request: Request, call_next):
        if request.url.path.startswith(("/api/", "/ws")):
            _check_token(request)
        response = await call_next(request)
        # Allow the Cloudflare-tunnel PWA origin (https) to call this backend
        # (the PWA may be served from the tunnel URL, not 127.0.0.1).
        origin = request.headers.get("origin", "")
        if origin:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Access-Token"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
            response.headers["Access-Control-Allow-Credentials"] = "true"
        elif request.method == "OPTIONS":
            response.headers["Access-Control-Allow-Origin"] = "*"
        return response

    @fastapi.options("/{path:path}")
    async def preflight(path: str):
        from fastapi.responses import Response

        return Response(status_code=200,
                        headers={"Access-Control-Allow-Origin": "*",
                                 "Access-Control-Allow-Headers": "Content-Type, X-Access-Token",
                                 "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS"})

    # ------------------------------------------------------------------ status
    @fastapi.get("/api/status")
    async def status():
        provider_statuses = {}
        brain_ids = await app.brain_ids()
        for agent_id in brain_ids:
            try:
                provider_statuses[agent_id] = await app.provider_status(agent_id)
            except Exception as exc:  # noqa: BLE001
                provider_statuses[agent_id] = {"ok": False, "detail": str(exc)[:200]}
        return {
            "version": APP_VERSION,
            "uptime_s": round(app.uptime_seconds(), 1),
            "killswitch": app.killswitch.to_dict(),
            "providers": provider_statuses,
            "agents": [
                {
                    **identity(agent_id),
                    "provider": provider_statuses.get(agent_id, {}).get("provider"),
                    "model": app.providers[agent_id].model,
                    "connected": provider_statuses.get(agent_id, {}).get("ok", False),
                }
                for agent_id in brain_ids
            ],
            "active_runs": {a: app.agents[a].active_runs() for a in brain_ids},
            "pending_confirmations": {a: len(await app.confirmations.pending_for_agent(a))
                                      for a in brain_ids},
            "voice": await voice_config(),
        }

    @fastapi.get("/api/agents")
    async def agents():
        out = []
        for agent_id in await app.brain_ids():
            meta = identity(agent_id)
            out.append({
                "id": agent_id,
                "display_name": meta["display_name"],
                "emoji": meta["emoji"],
                "tagline": meta["tagline"],
                "model": app.providers[agent_id].model,
                "provider": app.providers[agent_id].name,
                "api_key_configured": bool(app.secrets.get(KEY_ENV.get(agent_id, "")) or
                                           app.secrets.get(KEY_ENV["phantom"])),
            })
        return {"agents": out}

    # ------------------------------------------------------------ conversations
    @fastapi.get("/api/conversations")
    async def list_conversations(agent: str = "phantom", limit: int = 100):
        if agent not in await app.brain_ids():
            raise HTTPException(400, "unknown agent")
        return {"conversations": await app.conversations.list_for_agent(agent, limit)}

    @fastapi.post("/api/conversations")
    async def create_conversation(body: ConversationCreate):
        if body.agent not in await app.brain_ids():
            raise HTTPException(400, "unknown agent")
        conv = await app.conversations.create(body.agent, body.title)
        return conv

    # NOTE: static path must be registered before /{cid} or FastAPI would
    # capture "search" as a conversation id.
    @fastapi.get("/api/conversations/search")
    async def search_conversations(agent: str = "phantom", q: str = "", all_agents: bool = False):
        if all_agents:
            return {"results": await app.conversations.search_all_agents(q)}
        return {"results": await app.conversations.search(agent, q)}

    @fastapi.get("/api/conversations/{cid}")
    async def get_conversation(cid: str):
        conv = await app.conversations.get(cid)
        if not conv:
            raise HTTPException(404, "conversation not found")
        messages = await app.conversations.get_messages(cid)
        return {"conversation": conv, "messages": messages}

    @fastapi.delete("/api/conversations/{cid}")
    async def delete_conversation(cid: str):
        await app.conversations.delete(cid)
        return {"ok": True}

    @fastapi.post("/api/conversations/{cid}/archive")
    async def archive_conversation(cid: str, body: dict | None = None):
        archived = bool(body and body.get("archived", True))
        await app.conversations.archive(cid, archived)
        return {"ok": True, "archived": archived}

    # ------------------------------------------------------------------ chat
    @fastapi.post("/api/agents/{agent_id}/chat")
    async def chat(agent_id: str, body: ChatRequest):
        if agent_id not in app.agents:
            raise HTTPException(404, "unknown agent")
        if app.killswitch.is_engaged():
            raise HTTPException(409, "kill switch is engaged — disengage it before chatting")
        conversation_id = body.conversation_id
        if not conversation_id:
            conv = await app.conversations.create(agent_id)
            conversation_id = conv["id"]
        else:
            conv = await app.conversations.get(conversation_id)
            if not conv or conv["agent"] != agent_id:
                raise HTTPException(404, "conversation not found for this agent")
        started = await app.start_chat(agent_id, conversation_id, body.text, body.session_id)
        return started

    @fastapi.post("/api/runs/{run_id}/cancel")
    async def cancel_run(run_id: str):
        cancelled = app.cancel_run(run_id)
        return {"ok": cancelled}

    # ---------------------------------------------------------- confirmations
    @fastapi.get("/api/confirmations")
    async def list_confirmations(agent: str = ""):
        if agent:
            return {"confirmations": await app.confirmations.pending_for_agent(agent)}
        out = []
        for a in AGENTS:
            out.extend(await app.confirmations.pending_for_agent(a))
        return {"confirmations": out}

    @fastapi.post("/api/confirmations/{cid}/approve")
    async def approve_confirmation(cid: str, body: ConfirmBody | None = None):
        row = await app.confirmations.decide(cid, True, (body.decided_by if body else "user"))
        await app.events.publish("confirmation.decided", {"confirmation": row})
        return row

    @fastapi.post("/api/confirmations/{cid}/deny")
    async def deny_confirmation(cid: str, body: ConfirmBody | None = None):
        row = await app.confirmations.decide(cid, False, (body.decided_by if body else "user"))
        await app.events.publish("confirmation.decided", {"confirmation": row})
        return row

    # ---------------------------------------------------------------- memory
    @fastapi.get("/api/memories")
    async def list_memories(agent: str = "phantom", kind: str = "", limit: int = 200):
        rows = await app.memories.list(agent=agent, kind=kind or None, limit=limit)
        return {"memories": rows}

    @fastapi.get("/api/memories/search")
    async def search_memories(agent: str = "phantom", q: str = ""):
        return {"memories": await app.memories.search(agent, q)}

    @fastapi.post("/api/memories")
    async def create_memory(body: MemoryCreate):
        if body.agent not in ("phantom", "coded", "shared"):
            raise HTTPException(400, "unknown memory namespace")
        memory = await app.memories.add(
            agent=body.agent, content=body.content, kind=body.kind,
            importance=body.importance, tags=body.tags)
        await app.audit.record(body.agent, "memory.added", {"memory_id": memory["id"]})
        return memory

    @fastapi.post("/api/memories/{mid}/forget")
    async def forget_memory(mid: str):
        await app.memories.deactivate(mid)
        return {"ok": True}

    @fastapi.delete("/api/memories/{mid}")
    async def delete_memory(mid: str):
        await app.memories.hard_delete(mid)
        return {"ok": True}

    # ----------------------------------------------------------------- audit
    @fastapi.get("/api/audit")
    async def audit(agent: str = "", event: str = "", limit: int = 200, offset: int = 0):
        rows = await app.audit.query(agent or None, event or None, min(limit, 500), offset)
        return {"events": rows}

    @fastapi.get("/api/audit/events")
    async def audit_events():
        return {"events": await app.audit.events()}

    # -------------------------------------------------------------- settings
    @fastapi.get("/api/settings")
    async def get_settings():
        all_settings = await app.settings.all()
        return {
            "settings": all_settings,
            "voice": await voice_config(),
            "keys": {
                agent_id: {
                    "env": KEY_ENV.get(agent_id, f"{agent_id.upper()}_NVIDIA_API_KEY"),
                    "configured": bool(app.secrets.get(KEY_ENV.get(agent_id, "")) or
                                       app.secrets.get(KEY_ENV["phantom"])),
                    "masked": mask_key(app.secrets.get(KEY_ENV.get(agent_id, "")) or
                                       app.secrets.get(KEY_ENV["phantom"])),
                }
                for agent_id in await app.brain_ids()
            },
        }

    @fastapi.put("/api/settings")
    async def put_setting(body: SettingUpdate):
        if body.key == "nvidia_api_key":
            env = KEY_ENV.get(body.agent, KEY_ENV["phantom"])
            try:
                if body.delete:
                    persisted = app.secrets.delete(env)
                else:
                    persisted = app.secrets.set(env, str(body.value or ""))
            except Exception as exc:  # noqa: BLE001
                log.warning("settings save failed: %s", exc)
                raise HTTPException(500, "Could not write the key to disk — "
                                         "check the app data folder is writable") from None
            rebuild_ok = True
            if body.agent in AGENTS:
                try:
                    await app.rebuild_provider(body.agent)
                except Exception as exc:  # noqa: BLE001
                    log.warning("provider rebuild after key save failed: %s", exc)
                    rebuild_ok = False
            return {"ok": True, "saved": body.key, "agent": body.agent,
                    "persisted": persisted,
                    "masked": mask_key(app.secrets.get(env)),
                    "rebuild": rebuild_ok}
        try:
            if body.delete:
                await app.settings.delete(body.key, body.agent)
            else:
                await app.settings.set(body.key, body.value, body.agent)
        except Exception as exc:  # noqa: BLE001
            log.warning("settings save failed: %s", exc)
            raise HTTPException(500, "Could not save that setting") from None
        if body.agent in AGENTS and body.key in ("model", "model.temperature", "model.max_tokens"):
            try:
                await app.rebuild_provider(body.agent)
            except Exception as exc:  # noqa: BLE001
                log.warning("provider rebuild after settings change failed: %s", exc)
        return {"ok": True, "saved": body.key, "agent": body.agent, "persisted": True}

    @fastapi.post("/api/settings/rebuild-provider")
    async def rebuild_provider(body: AgentIdBody):
        if body.agent not in app.agents:
            raise HTTPException(400, "unknown agent")
        return await app.rebuild_provider(body.agent)

    # ------------------------------------------------------------- key test
    @fastapi.post("/api/keys/test")
    async def keys_test(body: dict):
        """Server-side live check of a stored key. The key itself never leaves
        the backend; the UI only receives ok + a friendly message."""
        import httpx as _httpx

        kind = str(body.get("kind", "")).lower()
        agent = str(body.get("agent", "phantom"))
        if kind not in ("nvidia", "deepgram", "groq", "cloud"):
            raise HTTPException(400, "unknown kind — use nvidia | deepgram | groq | cloud")
        try:
            async with _httpx.AsyncClient(timeout=20) as client:
                if kind == "nvidia":
                    # per-brain key first (brain.<id>.api_key), then the
                    # agent env key — so each brain's Test button checks ITS
                    # own key, not Phantom's
                    key = app.secrets.get(f"brain.{agent}.api_key") or \
                        app.secrets.get(KEY_ENV.get(agent, KEY_ENV["phantom"]))
                    if not key:
                        raise HTTPException(400, f"No NVIDIA key stored for {agent} — save one first")
                    resp = await client.get(
                        os.environ.get("PHAI_NVIDIA_BASE_URL", NVIDIA_BASE_URL) + "/models",
                        headers={"Authorization": f"Bearer {key}"})
                    return _key_test_result("NVIDIA", resp)
                if kind == "deepgram":
                    key = app.secrets.get("DEEPGRAM_API_KEY")
                    if not key:
                        raise HTTPException(400, "No Deepgram key stored — save one first")
                    resp = await client.get(
                        os.environ.get("PHAI_DEEPGRAM_BASE_URL", DEEPGRAM_BASE_URL) + "/projects",
                        headers={"Authorization": f"Token {key}"})
                    return _key_test_result("Deepgram", resp)
                if kind == "groq":
                    key = app.secrets.get("GROQ_API_KEY")
                    if not key:
                        raise HTTPException(400, "No Groq key stored — save one first")
                    resp = await client.get(
                        os.environ.get("PHAI_GROQ_BASE_URL", GROQ_BASE_URL) + "/models",
                        headers={"Authorization": f"Bearer {key}"})
                    return _key_test_result("Groq", resp)
                if kind == "cloud":
                    url = app.cloudsync._url() if app.cloudsync else ""
                    token = app.cloudsync._token() if app.cloudsync else ""
                    if not url:
                        raise HTTPException(400, "Portable Phantom URL not configured — save it first")
                    if not token:
                        raise HTTPException(400, "Portable Phantom token not configured — save it first")
                    resp = await client.get(
                        url + "/api/status",
                        headers={"X-Access-Token": token})
                    return _key_test_result("Portable Phantom", resp)
        except HTTPException:
            raise
        except _httpx.TimeoutException:
            raise HTTPException(502, f"{kind} test timed out — check your internet connection") from None
        except _httpx.HTTPError as exc:
            log.warning("key test (%s) connection failed: %s", kind,
                        SecretRedactor.redact(str(exc)))
            raise HTTPException(502, f"{kind} test could not connect — "
                                     "check your internet connection and try again") from None
        raise HTTPException(500, "unexpected key test failure")  # pragma: no cover

    # ------------------------------------------------------------ permissions
    @fastapi.get("/api/permissions")
    async def get_permissions():
        per_agent = {}
        for agent_id in AGENTS:
            rows = []
            for tool in app.registry.list():
                if agent_id not in tool.agents:
                    continue
                level, source, reason = await app.permissions.effective_level(
                    agent_id, tool.name, {})
                override = await app.settings.get(f"perm:{tool.name}", agent_id, None)
                if override is None:
                    override = await app.settings.get(f"perm:global:{tool.name}", "*", None)
                rows.append({
                    "tool": tool.name, "category": tool.category,
                    "default": tool.permission.value, "effective": level.value,
                    "source": source, "override": override,
                    "description": tool.description[:200],
                })
            per_agent[agent_id] = rows
        rules = await app.settings.get("path_rules", "*", [])
        return {"agents": per_agent, "path_rules": rules}

    @fastapi.put("/api/permissions")
    async def put_permission(body: PermissionUpdate):
        if not app.registry.has(body.tool):
            raise HTTPException(404, "unknown tool")
        if body.delete:
            await app.settings.delete(f"perm:{body.tool}", body.agent)
            return {"ok": True}
        try:
            level = PermissionLevel(body.level)
        except ValueError:
            raise HTTPException(400, f"invalid level: {body.level}") from None
        if level == PermissionLevel.BLOCKED:
            await app.settings.set(f"perm:{body.tool}", "blocked", body.agent)
        else:
            await app.settings.set(f"perm:{body.tool}", body.level, body.agent)
        return {"ok": True}

    @fastapi.put("/api/permissions/path-rules")
    async def put_path_rules(rules: list[dict]):
        await app.settings.set("path_rules", rules, "*")
        return {"ok": True}

    # ----------------------------------------------------------------- tools
    @fastapi.get("/api/tools")
    async def tools(agent: str = "phantom"):
        return {"tools": app.registry.summary(agent)}

    @fastapi.post("/api/tools/{tool_name}/run")
    async def run_tool(tool_name: str, body: ToolRun):
        """Manual tool execution from the UI (Tool Lab). Real execution,
        permission-checked; confirmations surface as pending + WS events."""
        if body.agent not in AGENTS:
            raise HTTPException(400, "unknown agent")
        if app.killswitch.is_engaged():
            raise HTTPException(409, "kill switch is engaged")
        try:
            spec = app.registry.get(tool_name)
            spec.validate_arguments(body.arguments or {})
        except ToolError as exc:
            raise HTTPException(400, exc.message) from None
        if body.agent not in spec.agents:
            raise HTTPException(403, "tool not available to this agent")

        ctx = ToolContext(
            agent_id=body.agent, conversation_id="tool-lab", session_id="tool-lab",
            data_dir=str(app.data_dir), workspace_root=app.workspace_root,
            db=app.db, settings=app.settings, permission_manager=app.permissions,
            audit=app.audit, events=app.events, memory_store=app.memories,
            conversation_store=app.conversations, delegation_manager=app.delegations,
            secrets=app.secrets, confirmations=app.confirmations,
            notifications=app.notifications,
        )
        try:
            decision = await app.permissions.authorize(
                body.agent, tool_name, body.arguments or {}, "tool-lab", "tool-lab",
                confirm_timeout=600.0, interactive=True)
        except ToolError as exc:
            return JSONResponse(status_code=403, content={"error": exc.message,
                                                          "kind": exc.kind})
        await app.audit.record(body.agent, "tool.lab_run", {
            "tool": tool_name, "arguments": body.arguments,
            "decision_level": decision.level.value})
        try:
            result = await spec.run(ctx, **(body.arguments or {}))
        except ToolError as exc:
            return JSONResponse(status_code=422, content={"error": exc.message,
                                                          "kind": exc.kind,
                                                          "retryable": exc.retryable})
        return {"result": result.output[:20000], "data": result.data,
                "success": result.success, "artifacts": result.artifacts}

    # ----------------------------------------------------------------- tasks
    @fastapi.get("/api/tasks")
    async def tasks(agent: str = "", status: str = ""):
        return {"tasks": await app.tasks.store.list(agent or None, status or None)}

    @fastapi.post("/api/tasks/{tid}/cancel")
    async def cancel_task(tid: str):
        return {"ok": await app.tasks.cancel(tid)}

    # -------------------------------------------------------------- schedules
    @fastapi.get("/api/schedules")
    async def schedules(agent: str = ""):
        return {"schedules": await app.scheduler.store.list(agent or None)}

    @fastapi.post("/api/schedules")
    async def create_schedule(body: ScheduleCreate):
        if body.agent not in AGENTS:
            raise HTTPException(400, "unknown agent")
        from ..heartbeat.scheduler import next_run_at

        try:
            next_run_at(body.expression)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        sched = await app.scheduler.store.create(
            body.agent, body.name, body.expression, body.prompt,
            body.quiet_start or None, body.quiet_end or None)
        sched = await app.scheduler.store.update(sched["id"],
                                                 next_run_at=next_run_at(body.expression))
        app.scheduler.wake()
        return sched

    @fastapi.put("/api/schedules/{sid}")
    async def update_schedule(sid: str, body: ScheduleUpdate):
        fields = body.model_dump(exclude_none=True)
        from ..heartbeat.scheduler import next_run_at

        expr = fields.get("expression")
        if expr:
            try:
                next_run_at(expr)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None
            fields["next_run_at"] = next_run_at(expr)
        sched = await app.scheduler.store.update(sid, **fields)
        if not sched:
            raise HTTPException(404, "schedule not found")
        app.scheduler.wake()
        return sched

    @fastapi.delete("/api/schedules/{sid}")
    async def delete_schedule(sid: str):
        await app.scheduler.store.delete(sid)
        return {"ok": True}

    # ----------------------------------------------------------- notifications
    @fastapi.get("/api/notifications")
    async def notifications(agent: str = ""):
        return {"notifications": await app.notifications.list(agent or None)}

    @fastapi.post("/api/notifications/{nid}/read")
    async def read_notification(nid: str):
        await app.notifications.mark(nid, read=True)
        return {"ok": True}

    @fastapi.post("/api/notifications/{nid}/dismiss")
    async def dismiss_notification(nid: str):
        await app.notifications.mark(nid, dismissed=True)
        return {"ok": True}

    # -------------------------------------------------------------- killswitch
    @fastapi.get("/api/killswitch")
    async def killswitch_state():
        return app.killswitch.to_dict()

    @fastapi.post("/api/killswitch/engage")
    async def engage_killswitch(body: KillSwitchBody):
        await app.killswitch.engage(body.reason or "manual")
        cancelled = await app.tasks.cancel_all()
        for agent_id in await app.brain_ids():
            for run_id in list(app.agents[agent_id].active_runs()):
                app.agents[agent_id].cancel(run_id)
        # stop voice output + microphone capture immediately (UI listens)
        await app.events.publish("voice.stop", {"reason": "kill switch"})
        await app.events.publish("killswitch.state", app.killswitch.to_dict())
        return {"ok": True, "cancelled_tasks": cancelled}

    @fastapi.post("/api/killswitch/disengage")
    async def disengage_killswitch():
        await app.killswitch.disengage()
        await app.events.publish("killswitch.state", app.killswitch.to_dict())
        return {"ok": True}

    # ------------------------------------------------------------------ voice
    @fastapi.get("/api/voice/config")
    async def voice_config():
        stt = await app.settings.get("voice.stt", "*", {"provider": "server"})
        tts = await app.settings.get("voice.tts", "*", {"provider": "server"})
        mode = await app.settings.get("voice.mode", "*", "conversation")
        proactive = await app.settings.get("voice.proactive_speech", "*", False)
        # per-persona voices (Phase 3): phantom & coded each have their own
        voices = await app.settings.get("voice.voices", "*", {})
        dg = app.secrets.get("DEEPGRAM_API_KEY") or ""
        groq = app.secrets.get("GROQ_API_KEY") or ""
        tts_cloud = await app.settings.get("voice.tts_cloud", "*", {}) or {}
        return {
            "stt": {"provider": stt.get("provider", "browser"),
                    "model": stt.get("model", "")},
            "tts": {"provider": tts.get("provider", "browser"),
                    "voice": tts.get("voice", "")},
            "voices": {
                "phantom": (voices or {}).get("phantom", ""),
                "coded": (voices or {}).get("coded", ""),
            },
            "mode": mode,
            "proactive_speech": bool(proactive),
            "deepgram_configured": bool(dg),
            "deepgram_masked": mask_key(dg),
            "groq_configured": bool(groq),
            "groq_masked": mask_key(groq),
            # multi-provider voice engine settings
            "stt_priority": await app.settings.get(
                "voice.stt_priority", "*",
                ["deepgram", "groq", "local_whisper"]),
            "tts_priority": await app.settings.get(
                "voice.tts_priority", "*",
                ["deepgram", "cloud", "local"]),
            "local_model": await app.settings.get("voice.local_model", "*", "base"),
            "tts_cloud": {
                "base_url": (tts_cloud or {}).get("base_url", ""),
                "configured": bool((tts_cloud or {}).get("base_url")),
            },
            "vad_threshold": await app.settings.get("voice.vad_threshold", "*", 0.02),
            "auto_stop_ms": await app.settings.get("voice.auto_stop_ms", "*", 900),
            "max_record_ms": await app.settings.get("voice.max_record_ms", "*", 15000),
            "continuous": bool(await app.settings.get("voice.continuous", "*", True)),
        }

    @fastapi.get("/api/voice/voices")
    async def voice_catalog():
        """Known voice catalog: browser voices are enumerated client-side; the
        Deepgram aura set is the server-side list (both male + female, so
        JOOJO can give Phantom & Coded distinct voices)."""
        return {
            "deepgram": [
                {"id": "aura-orion-en", "gender": "male",
                 "style": "warm, calm — good default for Phantom"},
                {"id": "aura-arcas-en", "gender": "male",
                 "style": "sharper, technical — good default for Coded"},
                {"id": "aura-asteria-en", "gender": "female", "style": "warm"},
                {"id": "aura-luna-en", "gender": "female", "style": "soft"},
                {"id": "aura-athena-en", "gender": "female", "style": "clear"},
                {"id": "aura-helios-en", "gender": "male", "style": "clear"},
                {"id": "aura-zeus-en", "gender": "male", "style": "deep"},
            ],
            "browser": [],  # filled by the UI from speechSynthesis.getVoices()
        }

    @fastapi.put("/api/voice/config")
    async def voice_config_set(body: dict):
        persisted = True
        try:
            if body.get("stt"):
                await app.settings.set("voice.stt", body["stt"], "*")
            if body.get("tts"):
                await app.settings.set("voice.tts", body["tts"], "*")
            if body.get("voices"):
                existing = await app.settings.get("voice.voices", "*", {})
                merged = {**(existing or {}), **body["voices"]}
                await app.settings.set("voice.voices", merged, "*")
            if body.get("mode") in ("private", "push", "conversation"):
                await app.settings.set("voice.mode", body["mode"], "*")
            if body.get("proactive_speech") is not None:
                await app.settings.set("voice.proactive_speech",
                                       bool(body["proactive_speech"]), "*")
            # multi-provider voice engine settings
            if body.get("stt_priority"):
                prio = body["stt_priority"]
                if isinstance(prio, str):
                    prio = [p.strip() for p in prio.split(",") if p.strip()]
                await app.settings.set("voice.stt_priority", list(prio), "*")
            if body.get("tts_priority"):
                prio = body["tts_priority"]
                if isinstance(prio, str):
                    prio = [p.strip() for p in prio.split(",") if p.strip()]
                await app.settings.set("voice.tts_priority", list(prio), "*")
            if body.get("local_model"):
                await app.settings.set("voice.local_model",
                                       str(body["local_model"]).strip(), "*")
            if body.get("tts_cloud") is not None:
                existing_cfg = await app.settings.get("voice.tts_cloud", "*", {}) or {}
                merged_cfg = {**(existing_cfg or {}), **body["tts_cloud"]}
                # never store a key in the DB — move it to the secrets file
                if merged_cfg.get("api_key"):
                    app.secrets.set("VOICE_TTS_CLOUD_API_KEY",
                                    str(merged_cfg.pop("api_key")).strip())
                await app.settings.set("voice.tts_cloud", merged_cfg, "*")
            if body.get("vad_threshold") is not None:
                await app.settings.set("voice.vad_threshold",
                                       float(body["vad_threshold"]), "*")
            if body.get("auto_stop_ms") is not None:
                await app.settings.set("voice.auto_stop_ms",
                                       int(body["auto_stop_ms"]), "*")
            if body.get("max_record_ms") is not None:
                await app.settings.set("voice.max_record_ms",
                                       int(body["max_record_ms"]), "*")
            if body.get("continuous") is not None:
                await app.settings.set("voice.continuous",
                                       bool(body["continuous"]), "*")
        except Exception as exc:  # noqa: BLE001
            log.warning("voice settings save failed: %s", exc)
            persisted = False
        if body.get("deepgram_api_key"):
            # never echoed back; stored server-side only
            ok = app.secrets.set("DEEPGRAM_API_KEY", str(body["deepgram_api_key"]).strip())
            persisted = persisted and ok
        if body.get("delete_deepgram_key"):
            app.secrets.delete("DEEPGRAM_API_KEY")
        if body.get("groq_api_key"):
            # stored server-side only; never returned to the UI
            ok = app.secrets.set("GROQ_API_KEY", str(body["groq_api_key"]).strip())
            persisted = persisted and ok
        if body.get("delete_groq_key"):
            app.secrets.delete("GROQ_API_KEY")
        await app.audit.record("user", "voice.config_changed",
                               {"keys": [k for k in ("stt", "tts", "voices", "mode",
                                                     "proactive_speech",
                                                     "deepgram_api_key",
                                                     "groq_api_key") if k in body]})
        cfg = await voice_config()
        cfg["persisted"] = persisted
        return cfg

    @fastapi.post("/api/voice/deepgram-token")
    async def deepgram_token():
        """Server-minted ephemeral Deepgram token (10 min) so the frontend never
        sees the real API key."""
        import httpx as _httpx

        key = app.secrets.get("DEEPGRAM_API_KEY")
        if not key:
            raise HTTPException(400, "DEEPGRAM_API_KEY not configured")
        try:
            async with _httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    "https://api.deepgram.com/v1/keys",
                    headers={"Authorization": f"Token {key}"},
                    params={"comment": "phantom-voice", "lifetime": "10",
                            "scopes": "member", "tags": "phantom"},
                )
                resp.raise_for_status()
                token = (resp.json() or {}).get("key") or \
                    ((resp.json() or {}).get("data") or {}).get("key")
                if not token:
                    raise HTTPException(502, "Deepgram did not return a key")
                return {"token": token, "expires_in": 600}
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"Deepgram token failed: {exc}") from None

    # ---------------------------------------------------------- diagnostics
    @fastapi.get("/api/diagnostics")
    async def diagnostics():
        """In-app diagnostics: recent backend log lines + key status, so the
        user can see what's wrong without a terminal (never keys/secrets)."""
        speaker = None
        if app.speaker is not None:
            try:
                speaker = await app.speaker.available()
            except Exception as exc:  # noqa: BLE001
                speaker = {"available": False, "reason": str(exc)[:120]}
        return {
            "version": APP_VERSION,
            "uptime_s": round(app.uptime_seconds(), 1),
            "logs": list(DIAG_LOG),
            "speaker": speaker,
            "keys": {
                "nvidia": bool(app.secrets.get("PHANTOM_NVIDIA_API_KEY")),
                "deepgram": bool(app.secrets.get("DEEPGRAM_API_KEY")),
                "groq": bool(app.secrets.get("GROQ_API_KEY")),
            },
            "providers": await _diagnostic_providers(app),
            "wake": {
                "state": (app.wake._state.state if getattr(app.wake, "_state", None)
                          else "unknown"),
            },
        }

    # ------------------------------------------------------------- HUD (real)
    @fastapi.get("/api/hud")
    async def hud_telemetry():
        """Real system telemetry for the HUD: per-core CPU, memory, disk/net I/O
        rates, battery, top CPU process, process count. Nothing is simulated;
        unreadable values report available=false."""
        if app.hud is None:
            raise HTTPException(503, "hud not ready")
        return await app.hud.snapshot()

    @fastapi.get("/api/hud/weather")
    async def hud_weather(lat: float = 0, lon: float = 0):
        """Weather via Open-Meteo (no API key). Location: settings
        hud.weather.lat/lon, else Accra (JOOJO's city). Cached 10 min; honest
        unavailable on network failure. Never leaks keys."""
        import time as _t

        lat = lat or float(await app.settings.get("hud.weather.lat", "*", 5.6037) or 5.6037)
        lon = lon or float(await app.settings.get("hud.weather.lon", "*", -0.1870) or -0.1870)
        cache = getattr(app, "_weather_cache", None)
        now = _t.time()
        if cache and now - cache[0] < 600 and abs(cache[1] - lat) < 0.01 and abs(cache[2] - lon) < 0.01:
            return cache[3]
        data = await _fetch_weather(lat, lon)
        if data is None:
            return {"available": False, "reason": "Open-Meteo unreachable — check internet"}
        out = {"available": True, "lat": round(lat, 4), "lon": round(lon, 4),
               "current": data.get("current"), "daily": data.get("daily"),
               "units": data.get("current_units") or {}}
        app._weather_cache = (now, lat, lon, out)
        return out

    # ------------------------------------------- multi-provider voice engine
    @fastapi.get("/api/voice/status")
    async def voice_status():
        if app.voice_mgr is None:
            raise HTTPException(503, "voice engine not ready")
        return await app.voice_mgr.status()

    @fastapi.get("/api/voice/usage")
    async def voice_usage():
        if app.voice_mgr is None:
            raise HTTPException(503, "voice engine not ready")
        return await app.voice_mgr.usage_summary()

    @fastapi.post("/api/voice/stt")
    async def voice_stt(audio: UploadFile = File(...), language: str = Form("en")):
        """Mic audio (WAV) -> STT failover chain. Returns the transcript plus
        which provider handled it (never the keys)."""
        if app.voice_mgr is None:
            raise HTTPException(503, "voice engine not ready")
        from ..voice.errors import VoiceProviderError

        path = await _save_voice_upload(app, audio)
        try:
            return await app.voice_mgr.transcribe(str(path), language=language)
        except VoiceProviderError as exc:
            raise HTTPException(502, exc.friendly) from None
        finally:
            try:
                path.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass

    @fastapi.post("/api/voice/tts")
    async def voice_tts(body: dict):
        """Text -> TTS failover chain. Returns synthesized audio (mp3/wav)
        plus the provider used in a response header."""
        if app.voice_mgr is None:
            raise HTTPException(503, "voice engine not ready")
        from ..voice.errors import VoiceProviderError

        text = (body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "text required")
        try:
            out = await app.voice_mgr.synthesize(text, voice=str(body.get("voice") or ""))
        except VoiceProviderError as exc:
            raise HTTPException(502, exc.friendly) from None
        media = "audio/mpeg" if out["format"] == "mp3" else "audio/wav"
        return FileResponse(
            out["audio_path"], media_type=media,
            headers={"X-TTS-Provider": out["provider"],
                     "X-Fallbacks": ",".join(out["fallbacks"])})

    @fastapi.post("/api/voice/test")
    async def voice_test(audio: UploadFile = File(...)):
        """Full pipeline test: mic -> STT -> Phantom AI -> TTS -> audio.
        The 'Test Voice System' button records 2s and calls this."""
        if app.voice_mgr is None:
            raise HTTPException(503, "voice engine not ready")
        from ..voice.errors import VoiceProviderError

        path = await _save_voice_upload(app, audio)

        async def _reply(transcript: str) -> str:
            try:
                if await app.conversations.get("voice-test") is None:
                    await app.conversations.create(
                        "phantom", title="Voice test", conversation_id="voice-test")
                result = await app._run_agent(
                    "phantom", "voice-test",
                    f"Acknowledge this in one short sentence: {transcript}",
                    mode="voice_test")
                content = ((result or {}).get("content") or "").strip()
                return content or f"You said: {transcript}"
            except Exception:  # noqa: BLE001
                return f"You said: {transcript}"

        try:
            out = await app.voice_mgr.test_pipeline(str(path), reply_fn=_reply)
        except VoiceProviderError as exc:
            raise HTTPException(502, exc.friendly) from None
        finally:
            try:
                path.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
        media = "audio/mpeg" if out["format"] == "mp3" else "audio/wav"
        return FileResponse(
            out["audio_path"], media_type=media,
            headers={"X-STT-Provider": out["stt_provider"],
                     "X-TTS-Provider": out["tts_provider"],
                     "X-Fallbacks": ",".join(out["stt_fallbacks"] + out["tts_fallbacks"]),
                     "X-Transcript": out["transcript"][:200],
                     "X-Reply": out["reply"][:200]})

    @fastapi.post("/api/voice/speak")
    async def voice_speak(body: dict):
        """Proactive speech trigger (heartbeat/testing): publishes a voice.speak
        event over WebSocket. Respects mute/quiet rules via the UI."""
        text = (body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "text required")
        await app.events.publish("voice.speak", {"text": text[:2000],
                                                 "priority": body.get("priority", "normal")})
        await app.audit.record("system", "voice.proactive", {"text": text[:200]})
        return {"ok": True}

    # -------------------------------------------------------------- evolution
    @fastapi.get("/api/brains")
    async def brains():
        if app.brains is None:
            return {"brains": []}
        if app.brain_health is not None:
            await app.brain_health.update_all(app.brains)
        return {"brains": [app.brains.summary(b) for b in await app.brains.list()]}

    @fastapi.post("/api/brains")
    async def register_brain(body: dict):
        """Dynamic brain creation (Brain Definition format)."""
        from ..brains.definitions import BrainDefinition

        definition = BrainDefinition.from_dict(body)
        try:
            result = await app.brains.register(
                definition, created_by=body.get("created_by", "user"),
                require_approval=bool(definition.permissions))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from None
        return result

    @fastapi.get("/api/brains/configs")
    async def brains_configs():
        from ..brains.config import brain_key_envs, resolve_brain_config

        out = []
        for bid in await app.brain_ids():
            brain = await app.brains.get(bid) if app.brains else None
            definition = dict(brain) if brain else {}
            cfg = await resolve_brain_config(bid, definition, app.secrets,
                                             app.settings)
            own = cfg["api_key"] and cfg["api_key"] != app.secrets.get(
                "PHANTOM_NVIDIA_API_KEY")
            out.append({
                "brain_id": bid,
                "name": (brain or {}).get("name", bid),
                "role": (brain or {}).get("role", ""),
                "model": cfg["model"],
                "key_configured": bool(own),
                "key_masked": mask_key(cfg["api_key"]) if cfg["api_key"] else "",
                "key_env": brain_key_envs(bid)["api_key_env"],
            })
        return {"brains": out}

    @fastapi.get("/api/brains/{bid}/config")
    async def brain_config(bid: str):
        from ..brains.config import brain_key_envs, resolve_brain_config

        if bid not in app.agents:
            raise HTTPException(404, "unknown brain")
        brain = await app.brains.get(bid) if app.brains else None
        definition = dict(brain) if brain else {}
        cfg = await resolve_brain_config(bid, definition, app.secrets, app.settings)
        key_envs = brain_key_envs(bid)
        key_configured = bool(
            cfg["api_key"] and cfg["api_key"] != app.secrets.get("PHANTOM_NVIDIA_API_KEY"))
        return {
            "brain_id": bid,
            "name": (brain or {}).get("name", bid),
            "model": cfg["model"],
            "base_url": cfg["base_url"],
            "key_configured": key_configured,
            "key_masked": mask_key(cfg["api_key"]) if cfg["api_key"] else "",
            "key_env": key_envs["api_key_env"],
            "provider": app.providers[bid].name,
        }

    @fastapi.put("/api/brains/{bid}/config")
    async def brain_config_set(bid: str, body: dict):
        if bid not in app.agents:
            raise HTTPException(404, "unknown brain")
        persisted = True
        try:
            if body.get("api_key"):
                # never logged; stored in chmod-600 secrets store
                ok = app.secrets.set(f"brain.{bid}.api_key", str(body["api_key"]).strip())
                persisted = persisted and ok
            if body.get("delete_key"):
                app.secrets.delete(f"brain.{bid}.api_key")
            if body.get("model"):
                await app.settings.set(f"brain.{bid}.model", str(body["model"]).strip(), "*")
            if body.get("base_url"):
                await app.settings.set(f"brain.{bid}.base_url", str(body["base_url"]).strip(), "*")
        except Exception as exc:  # noqa: BLE001
            log.warning("brain config save failed: %s", exc)
            persisted = False
        try:
            await app.rebuild_provider(bid)
        except Exception as exc:  # noqa: BLE001
            log.warning("brain provider rebuild failed: %s", exc)
        await app.audit.record("user", "brain.config_changed", {"brain": bid})
        result = await brain_config(bid)
        result["persisted"] = persisted
        return result

    @fastapi.delete("/api/brains/{bid}")
    async def delete_brain(bid: str):
        if bid in ("phantom", "coded", "evolution"):
            raise HTTPException(400, "builtin brains cannot be deleted")
        await app.brains.delete(bid)
        app.agents.pop(bid, None)
        return {"ok": True}

    @fastapi.get("/api/graph")
    async def graph_stats():
        return {"stats": await app.graph.stats()}

    @fastapi.get("/api/graph/query")
    async def graph_query(q: str = "", node_type: str = "", limit: int = 25):
        return {"nodes": await app.graph.store.search(q, node_type or None, limit)}

    @fastapi.get("/api/graph/neighbors")
    async def graph_neighbors(node: str, depth: int = 2):
        return {"nodes": await app.graph.related(node, depth=min(depth, 5))}

    @fastapi.get("/api/graph/path")
    async def graph_path(source: str, target: str):
        ids = await app.graph.path_between(source, target)
        nodes = []
        for nid in ids:
            node = await app.graph.store.get_node(nid)
            if node:
                nodes.append(node)
        return {"path": nodes}

    @fastapi.post("/api/graph/track")
    async def graph_track(body: dict):
        node_type = body.get("node_type", "")
        label = body.get("label", "")
        if not node_type or not label:
            raise HTTPException(400, "node_type and label required")
        try:
            result = await app.graph.track(
                node_type, label, body.get("properties") or {},
                agent=body.get("agent", "user"),
                relation=body.get("relation", ""), target=body.get("target", ""))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return result

    @fastapi.get("/api/proposals")
    async def proposals(status: str = "", limit: int = 100):
        return {"proposals": await app.proposals.list(status or None, limit)}

    @fastapi.post("/api/proposals")
    async def create_proposal(body: dict):
        proposal = await app.proposals.create(
            title=body.get("title", "Untitled"),
            description=body.get("description", ""),
            kind=body.get("kind", "config"),
            changes=body.get("changes") or {},
            risk=body.get("risk", "low"),
            created_by=body.get("created_by", "user"))
        return proposal

    @fastapi.post("/api/proposals/{pid}/approve")
    async def approve_proposal(pid: str):
        """Approve + deploy a proposal: snapshot pre-change, apply changes,
        mark deployed. This is the PROPOSE → APPROVE → DEPLOY step."""
        proposal = await app.proposals.get(pid)
        if not proposal:
            raise HTTPException(404, "proposal not found")
        if proposal["status"] != "proposed":
            raise HTTPException(409, f"proposal already {proposal['status']}")
        snapshot = await app.snapshots.capture(
            label=f"pre-deploy:{proposal['title'][:60]}",
            description="snapshot before proposal deployment")
        await app.proposals.set_status(pid, "approved")
        result = await _apply_proposal_changes(app, proposal)
        status = "deployed" if result else "failed"
        await app.proposals.set_status(pid, status, result=str(result)[:2000])
        await app.audit.record("user", "proposal.deployed", {
            "proposal_id": pid, "status": status, "snapshot_id": snapshot["id"]})
        return {"ok": True, "status": status, "snapshot_id": snapshot["id"]}

    @fastapi.post("/api/proposals/{pid}/reject")
    async def reject_proposal(pid: str):
        await app.proposals.set_status(pid, "rejected", result="rejected by user")
        return {"ok": True}

    @fastapi.get("/api/snapshots")
    async def snapshots(limit: int = 50):
        return {"snapshots": await app.snapshots.list()}

    @fastapi.post("/api/snapshots")
    async def create_snapshot(body: dict):
        snap = await app.snapshots.capture(
            label=body.get("label", "manual snapshot"),
            description=body.get("description", ""))
        return snap

    @fastapi.post("/api/snapshots/{sid}/restore")
    async def restore_snapshot(sid: str, body: dict | None = None):
        try:
            snapshot = await app.snapshots.restore(
                sid, reason=(body or {}).get("reason", "manual"))
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None
        return {"ok": True, "snapshot": snapshot}

    @fastapi.get("/api/evolution/metrics")
    async def evolution_metrics(since_hours: int = 24):
        return await app.analyst.metrics(since_hours=min(since_hours, 24 * 30))

    @fastapi.get("/api/evolution/opportunities")
    async def evolution_opportunities(since_hours: int = 24):
        return {"opportunities": await app.analyst.opportunities(
            since_hours=min(since_hours, 24 * 30))}

    @fastapi.post("/api/evolution/audit")
    async def run_self_audit(body: dict | None = None):
        period = (body or {}).get("period", "daily")
        report = await app.analyst.generate_report(period)
        return {"report": report}

    @fastapi.post("/api/loops/run")
    async def run_loop(body: dict):
        """Run an autonomous task through the improvement loop (background)."""
        from ..loops.engine import LoopConfig

        objective = body.get("objective", "")
        if not objective:
            raise HTTPException(400, "objective required")
        agent = body.get("agent", "phantom")
        if agent not in app.agents:
            raise HTTPException(400, f"unknown agent: {agent}")

        async def factory(task_id: str):
            cfg = LoopConfig(
                max_iterations=max(1, min(int(body.get("max_iterations", 5)), 20)),
                timeout_sec=max(30, min(int(body.get("timeout_sec", 300)), 1800)),
                failure_threshold=max(1, min(int(body.get("failure_threshold", 3)), 10)),
                escalate_to=body.get("escalate_to", "") or "",
                rollback=bool(body.get("rollback", True)),
                success_criteria=body.get("success_criteria",
                                          "The objective is achieved and verifiable."),
            )
            return await app.loops.run(agent, objective, body.get("context", ""), cfg)

        task = await app.tasks.launch(agent, f"Loop: {objective[:60]}", "loop", factory)
        return task

    # ------------------------------------------------------------- presence
    @fastapi.get("/api/presence")
    async def presence():
        return app.wake.state_dict()

    @fastapi.post("/api/presence/wake")
    async def presence_wake_v2(request: Request):
        """Wake with optional voice sample (raw body when it's a WAV, else JSON).
        Speaker lock: a matching voiceprint is required when enabled+enrolled."""
        ctype = request.headers.get("content-type", "")
        agent = request.query_params.get("agent", "phantom") or "phantom"
        wav_bytes = None
        if "octet-stream" in ctype or "wav" in ctype:
            wav_bytes = await request.body()
        else:
            body = await request.json()
            agent = str(body.get("agent", agent))
            import base64 as _b64

            b64 = body.get("sample_wav")
            if b64:
                wav_bytes = _b64.b64decode(b64)
        if agent not in ("phantom", "coded"):
            raise HTTPException(400, "agent must be phantom or coded")
        return await app.wake.wake(agent, source="wakeword", wav_bytes=wav_bytes)

    # --------------------------------------------------------- voice enrollment
    @fastapi.get("/api/voice/enroll/status")
    async def enroll_status(agent: str = "phantom"):
        return {
            "agent": agent,
            "enrolled": bool(await app.speaker.enrolled(agent)),
            "speaker_lock": bool(await app.settings.get("wake.speaker_lock", "*", False)),
            "engine": await app.speaker.available(),
        }

    @fastapi.post("/api/voice/enroll/sample")
    async def enroll_sample(request: Request):
        agent = request.query_params.get("agent", "phantom") or "phantom"
        if agent not in ("phantom", "coded"):
            raise HTTPException(400, "agent must be phantom or coded")
        wav_bytes = await request.body()
        if not wav_bytes or len(wav_bytes) < 100:
            raise HTTPException(400, "empty audio sample")
        try:
            result = await app.speaker.enroll_sample(agent, wav_bytes)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        except Exception as exc:  # noqa: BLE001 — engine/weights unavailable
            raise HTTPException(503,
                f"speaker engine unavailable: {exc}") from None
        return result

    @fastapi.post("/api/voice/enroll/clear")
    async def enroll_clear(body: dict | None = None):
        agent = (body or {}).get("agent", "phantom") or "phantom"
        await app.speaker.clear(agent)
        return {"ok": True}

    @fastapi.put("/api/voice/enroll/speaker-lock")
    async def enroll_speaker_lock(body: dict):
        value = bool(body.get("enabled", True))
        await app.wake.set_speaker_lock(value)
        return {"speaker_lock": value}

    @fastapi.post("/api/presence/wake")
    async def presence_wake(request: Request):
        """Wake with optional voice sample (raw WAV body or JSON with base64
        sample_wav). Speaker lock: a matching voiceprint is required when
        enabled and enrolled."""
        ctype = request.headers.get("content-type", "")
        agent = request.query_params.get("agent", "phantom") or "phantom"
        wav_bytes = None
        if "octet-stream" in ctype or "wav" in ctype:
            wav_bytes = await request.body()
        else:
            import base64 as _b64

            body = await request.json()
            agent = str(body.get("agent", agent))
            b64 = body.get("sample_wav")
            if b64:
                wav_bytes = _b64.b64decode(b64)
        if agent not in ("phantom", "coded"):
            raise HTTPException(400, "agent must be phantom or coded")
        return await app.wake.wake(agent, source="wakeword", wav_bytes=wav_bytes)

    @fastapi.post("/api/presence/sleep")
    async def presence_sleep(body: dict | None = None):
        return await app.wake.sleep(reason=(body or {}).get("reason", "manual"))

    @fastapi.post("/api/presence/stay-silent")
    async def presence_stay_silent():
        return await app.wake.stay_silent()

    @fastapi.post("/api/presence/touch")
    async def presence_touch():
        await app.wake.touch()
        return {"ok": True}

    @fastapi.put("/api/presence/config")
    async def presence_config(body: dict):
        if body.get("enabled") is not None:
            await app.wake.set_enabled(bool(body["enabled"]))
        if body.get("idle_minutes") is not None:
            await app.wake.set_idle_minutes(int(body["idle_minutes"]))
        return app.wake.state_dict()

    @fastapi.get("/api/presence/config")
    async def presence_config_get():
        return {
            "enabled": bool(await app.settings.get("wake.enabled", "*", True)),
            "idle_minutes": int(await app.settings.get("wake.idle_minutes", "*", 60)),
            "engine": await app.settings.get("wake.engine", "*", "client"),
            "words": {"phantom": "phantom", "coded": "coded"},
        }

    # -------------------------------------------------------------- profiles
    @fastapi.get("/api/profiles")
    async def profiles_list():
        return {"profiles": await app.profiles.list()}

    @fastapi.get("/api/profiles/default")
    async def profiles_default():
        profile = await app.profiles.default()
        if not profile:
            raise HTTPException(404, "no profile yet")
        return profile

    @fastapi.post("/api/profiles")
    async def profiles_create(body: dict):
        name = str(body.get("name", "")).strip()
        if not name:
            raise HTTPException(400, "name required")
        profile = await app.profiles.create(
            name, str(body.get("display_name", "")),
            body.get("fields") or {})
        await app.audit.record("user", "profile.created", {"profile": profile["id"]})
        return profile

    @fastapi.put("/api/profiles/{pid}")
    async def profiles_update(pid: str, body: dict):
        profile = await app.profiles.update(
            pid, body.get("fields") or {}, display_name=body.get("display_name"))
        if not profile:
            raise HTTPException(404, "profile not found")
        return profile

    @fastapi.delete("/api/profiles/{pid}")
    async def profiles_delete(pid: str):
        ok = await app.profiles.delete(pid)
        if not ok:
            raise HTTPException(404, "profile not found")
        return {"ok": True}

    # ------------------------------------------------------------- briefing
    @fastapi.get("/api/briefing")
    async def briefing_get(force: bool = False):
        """Cached briefing — instant; force=true regenerates."""
        return await app.briefing.get(force=force)

    @fastapi.post("/api/briefing/refresh")
    async def briefing_refresh():
        return await app.briefing.refresh()

    @fastapi.get("/api/briefing/config")
    async def briefing_config():
        return {
            "time": await app.settings.get("briefing.time", "*", "07:30"),
            "schedules": [
                {"name": s["name"], "agent": s["agent"], "expression": s["expression"]}
                for s in await app.scheduler.store.list()
                if s["name"] in ("Daily briefing", "Daily health briefing")
            ],
        }

    @fastapi.put("/api/briefing/config")
    async def briefing_config_set(body: dict):
        try:
            return await app.set_briefing_time(str(body["time"]))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @fastapi.get("/api/monitor")
    async def monitor_get():
        return await app.monitor.snapshot()

    @fastapi.get("/api/monitor/watch")
    async def monitor_watch():
        return await app.monitor.watch_list()

    # ------------------------------------------------------------- companion
    @fastapi.get("/api/companion/status")
    async def companion_status():
        return await app.companion.status()

    @fastapi.get("/api/companion/confirmations")
    async def companion_confirmations():
        return {"confirmations": await app.companion.pending_confirmations()}

    @fastapi.post("/api/companion/confirmations/{cid}/approve")
    async def companion_confirm_approve(cid: str):
        row = await app.companion.decide_confirmation(cid, True)
        await app.events.publish("confirmation.decided", {"confirmation": row})
        return row

    @fastapi.post("/api/companion/confirmations/{cid}/deny")
    async def companion_confirm_deny(cid: str):
        row = await app.companion.decide_confirmation(cid, False)
        await app.events.publish("confirmation.decided", {"confirmation": row})
        return row

    @fastapi.post("/api/companion/voice")
    async def companion_voice(request: Request):
        """Phone tap-to-talk: raw WAV body + ?agent=phantom|coded. The PC
        transcribes (Deepgram), runs the agent, returns the reply."""
        agent = request.query_params.get("agent", "phantom") or "phantom"
        if agent not in ("phantom", "coded"):
            raise HTTPException(400, "agent must be phantom or coded")
        wav = await request.body()
        if not wav or len(wav) < 200:
            raise HTTPException(400, "audio too short or empty")
        try:
            result = await app.companion.voice_forward(agent, wav)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from None
        return result

    # ------------------------------------------------------------- cloudsync
    @fastapi.get("/api/cloud/config")
    async def cloud_config():
        return await app.cloudsync.config()

    @fastapi.put("/api/cloud/config")
    async def cloud_config_set(body: dict):
        return await app.cloudsync.save_config(
            url=str(body.get("url", "")).strip(),
            token=str(body.get("token", "")).strip(),
            clear=bool(body.get("clear")))

    @fastapi.get("/api/cloud/memories")
    async def cloud_memories():
        if not await app.cloudsync.configured():
            from fastapi import HTTPException

            raise HTTPException(503, "portable Phantom URL not configured")
        return {"memories": await app.cloudsync.list_cloud_memories()}

    @fastapi.post("/api/cloud/keys")
    async def cloud_keys(body: dict):
        """Forward cloud NVIDIA/Deepgram keys to the Worker secret store (the
        PC app's Settings → Portable Phantom). Masked, never returned."""
        import httpx as _httpx

        if not await app.cloudsync.configured():
            from fastapi import HTTPException

            raise HTTPException(503, "portable Phantom URL not configured")
        payload = {}
        if body.get("nvidia_key"):
            payload["nvidia_key"] = str(body["nvidia_key"]).strip()
        if body.get("deepgram_key"):
            payload["deepgram_key"] = str(body["deepgram_key"]).strip()
        if body.get("model"):
            payload["model"] = str(body["model"]).strip()
        if body.get("clear_nvidia"):
            payload["clear_nvidia"] = True
        if body.get("clear_deepgram"):
            payload["clear_deepgram"] = True
        if not payload:
            from fastapi import HTTPException

            raise HTTPException(400, "no keys provided")
        headers = {"Content-Type": "application/json"}
        token = app.cloudsync._token()
        if token:
            headers["X-Access-Token"] = token
        url = app.cloudsync._url() + "/api/config/keys"
        try:
            async with _httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(url, headers=headers, json=payload)
                if resp.status_code >= 400:
                    raise RuntimeError(f"cloud rejected keys: {resp.status_code}")
                data = resp.json()
        except Exception as exc:  # noqa: BLE001
            from fastapi import HTTPException

            raise HTTPException(502, f"could not save cloud keys: {exc}") from None
        await app.audit.record("user", "cloud.keys_updated",
                               {"masked": data.get("masked")})
        return data

    @fastapi.post("/api/cloud/deploy")
    async def cloud_deploy(body: dict | None = None):
        """Redeploy the Portable Worker straight from Settings (no terminal).
        Keys/token optional (env → deploy.sh); streams output as cloud.deploy_log."""
        body = body or {}
        try:
            task = await app.clouddeploy.deploy(
                nvidia_key=str(body.get("nvidia_key", "")).strip(),
                deepgram_key=str(body.get("deepgram_key", "")).strip(),
                cloud_token=str(body.get("cloud_token", "")).strip())
        except RuntimeError as exc:
            from fastapi import HTTPException

            raise HTTPException(400, str(exc)) from None
        return {"task": task}

    @fastapi.get("/api/cloud/deploy/logs")
    async def cloud_deploy_logs():
        return {"logs": await app.clouddeploy.last_logs()}

    @fastapi.post("/api/cloud/save")
    async def cloud_save(body: dict):
        """'Save this specifically to cloud' — explicit, audited."""
        try:
            return await app.cloudsync.save_to_cloud(
                content=str(body.get("content", "")).strip(),
                kind=str(body.get("kind", "fact")),
                tags=body.get("tags"))
        except (ValueError, RuntimeError) as exc:
            from fastapi import HTTPException

            raise HTTPException(400 if isinstance(exc, ValueError) else 503,
                                str(exc)) from None

    @fastapi.post("/api/cloud/sync")
    async def cloud_sync_all():
        """Push profile + reminders, pull cloud memories (the shared slice)."""
        out: dict[str, Any] = {}
        if not await app.cloudsync.configured():
            from fastapi import HTTPException

            raise HTTPException(503, "portable Phantom URL not configured")
        out["profile"] = await app.cloudsync.push_profile()
        out["reminders"] = await app.cloudsync.push_reminders()
        out["memories"] = await app.cloudsync.fetch_cloud_memories()
        return out

    # ---------------------------------------------------------------- health
    @fastapi.get("/api/health/status")
    async def health_status():
        return await app.health.status()

    @fastapi.get("/api/health/today")
    async def health_today():
        return await app.health.today()

    @fastapi.get("/api/health/morning")
    async def health_morning():
        return {"text": await app.health.morning()}

    @fastapi.get("/api/health/evening")
    async def health_evening():
        return {"text": await app.health.evening()}

    @fastapi.get("/api/health/trends")
    async def health_trends(metric: str, limit: int = 14):
        return await app.health.trends(metric, limit)

    @fastapi.get("/api/health/memories")
    async def health_memories(category: str = "", q: str = "", limit: int = 100):
        if q:
            return {"records": await app.health.vault.search(q, limit)}
        return {"records": await app.health.vault.list(category or None, limit)}

    @fastapi.post("/api/health/memories")
    async def health_add_memory(body: dict):
        category = body.get("category", "note")
        data = body.get("data") or {}
        title = body.get("title", "")
        try:
            record = await app.health.vault.add(category, data, title=title)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        await app.audit.record("health", "health.record_added",
                               {"category": category, "id": record["id"]})
        return record

    @fastapi.put("/api/health/memories/{rid}")
    async def health_update_memory(rid: str, body: dict):
        record = await app.health.vault.update(
            rid, body.get("data") or {}, title=body.get("title"))
        if not record:
            raise HTTPException(404, "record not found")
        return record

    @fastapi.post("/api/health/memories/{rid}/delete")
    async def health_delete_memory(rid: str):
        ok = await app.health.vault.delete(rid)
        if not ok:
            raise HTTPException(404, "record not found")
        await app.audit.record("health", "health.record_deleted", {"id": rid})
        return {"ok": True}

    @fastapi.post("/api/health/clear")
    async def health_clear(body: dict | None = None):
        category = (body or {}).get("category", "")
        count = await app.health.vault.clear(category)
        await app.audit.record("health", "health.records_cleared",
                               {"category": category or "ALL", "count": count})
        return {"ok": True, "cleared": count}

    @fastapi.get("/api/health/export")
    async def health_export():
        data = await app.health.vault.export()
        await app.audit.record("health", "health.exported", {"count": data["count"]})
        return JSONResponse(content=data)

    @fastapi.get("/api/health/privacy")
    async def health_privacy():
        return await app.health.privacy()

    @fastapi.put("/api/health/privacy")
    async def health_privacy_set(body: dict):
        return await app.health.set_privacy(**body)

    @fastapi.get("/api/health/routines")
    async def health_routines():
        return {"routines": await app.health.routines()}

    @fastapi.put("/api/health/routines")
    async def health_routines_set(body: dict):
        block = body.get("block", "")
        if block not in ("morning", "afternoon", "evening"):
            raise HTTPException(400, "block must be morning|afternoon|evening")
        return await app.health.set_routine(block, body.get("items") or [])

    @fastapi.post("/api/health/routines/schedule")
    async def health_routine_schedule(body: dict):
        block = body.get("block", "")
        hour = int(body.get("hour", 7))
        minute = int(body.get("minute", 0))
        try:
            sched = await app.health.schedule_routine(block, hour, minute)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return sched

    @fastapi.post("/api/health/redflag")
    async def health_redflag(body: dict):
        from ..health.redflags import RedFlag, check_measurement, check_symptom

        flag = None
        if body.get("symptom"):
            flag = check_symptom(body["symptom"], int(body.get("severity", 0)))
        elif body.get("metric"):
            flag = check_measurement(body["metric"], float(body.get("value", 0)),
                                     body.get("unit", ""))
        return {"red_flag": flag.to_dict() if flag else None,
                "text": _flag_text(flag) if flag else ""}

    @fastapi.post("/api/health/enable")
    async def health_enable(body: dict | None = None):
        value = bool((body or {}).get("enabled", True))
        return await app.health.set_enabled(value, by="user")

    @fastapi.get("/api/health/briefing")
    async def health_briefing():
        return {"section": await app.health.briefing_section()}

    # ------------------------------------------------------------- artifacts
    @fastapi.get("/api/artifacts/{fname}")
    async def artifact(fname: str):
        from ..config import SCREENSHOT_DIR

        name = Path(fname).name
        if not name or ".." in name or "/" in name:
            raise HTTPException(400, "invalid artifact name")
        path = SCREENSHOT_DIR / name
        if not path.exists():
            raise HTTPException(404, "artifact not found")
        return FileResponse(path)

    # --------------------------------------------------------------------- ws
    @fastapi.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        if ACCESS_TOKEN and ws.query_params.get("token") != ACCESS_TOKEN:
            await ws.close(code=4401)
            return
        await ws.accept()
        subscriber_id = f"ws-{id(ws)}"
        queue = await app.events.subscribe(subscriber_id)
        try:
            import asyncio

            async def pump():
                while True:
                    event = await queue.get()
                    await ws.send_json(event)

            pump_task = asyncio.ensure_future(pump())
            try:
                while True:
                    msg = await ws.receive_json()
                    msg_type = msg.get("type")
                    if msg_type == "cancel_run":
                        app.cancel_run(msg.get("run_id", ""))
                    elif msg_type == "ping":
                        await ws.send_json({"event": "pong"})
            except WebSocketDisconnect:
                pump_task.cancel()
        except Exception:  # noqa: BLE001
            pass
        finally:
            await app.events.unsubscribe(subscriber_id)

    # ----------------------------------------------------------------- static
    if UI_DIR.exists():
        fastapi.mount("/ui", StaticFiles(directory=str(UI_DIR)), name="ui")

        @fastapi.get("/")
        async def index():
            return FileResponse(UI_DIR / "index.html")

        @fastapi.get("/app.js")
        async def app_js():
            return FileResponse(UI_DIR / "app.js", media_type="text/javascript")

        @fastapi.get("/styles.css")
        async def styles_css():
            return FileResponse(UI_DIR / "styles.css", media_type="text/css")

        @fastapi.get("/voice.js")
        async def voice_js():
            return FileResponse(UI_DIR / "voice.js", media_type="text/javascript")

        @fastapi.get("/hud.js")
        async def hud_js():
            return FileResponse(UI_DIR / "hud.js", media_type="text/javascript")

        @fastapi.get("/mobile")
        async def mobile_index():
            return FileResponse(UI_DIR / "mobile.html")

        @fastapi.get("/mobile.js")
        async def mobile_js():
            return FileResponse(UI_DIR / "mobile.js", media_type="text/javascript")

        @fastapi.get("/mobile.css")
        async def mobile_css():
            return FileResponse(UI_DIR / "mobile.css", media_type="text/css")

        @fastapi.get("/manifest.webmanifest")
        async def manifest():
            return FileResponse(UI_DIR / "manifest.webmanifest",
                                media_type="application/manifest+json")

        @fastapi.get("/sw.js")
        async def sw():
            return FileResponse(UI_DIR / "sw.js", media_type="text/javascript")

        @fastapi.get("/apple-touch-icon.png")
        async def apple_icon():
            return FileResponse(UI_DIR / "apple-touch-icon.png",
                                media_type="image/png")

        @fastapi.get("/icons/{fname}")
        async def pwa_icon(fname: str):
            name = Path(fname).name
            if ".." in name or "/" in name:
                raise HTTPException(400, "invalid icon")
            path = UI_DIR / "icons" / name
            if not path.exists():
                raise HTTPException(404, "icon not found")
            return FileResponse(path, media_type="image/png")

    return fastapi


def _flag_text(flag) -> str:
    from ..health.redflags import explain_red_flag

    return explain_red_flag(flag)


async def _apply_proposal_changes(app: Any, proposal: dict) -> bool:
    """Deploy a proposal's changes (settings/permissions). Wrapped in
    snapshots by the caller; a failure leaves the system untouched."""
    changes = proposal.get("changes") or {}
    try:
        for agent, kv in (changes.get("settings") or {}).items():
            for key, value in kv.items():
                await app.settings.set(key, value, agent)
        for agent, tool_levels in (changes.get("permissions") or {}).items():
            for tool, level in tool_levels.items():
                if app.registry.has(tool):
                    await app.settings.set(f"perm:{tool}", level, agent)
        return True
    except Exception:  # noqa: BLE001
        return False
