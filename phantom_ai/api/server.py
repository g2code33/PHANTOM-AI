"""FastAPI application: REST API + WebSocket event stream + static UI."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..agents.identities import identity_summary
from ..config import AGENTS, KEY_ENV, UI_DIR, mask_key
from ..permissions.policy import PermissionLevel
from ..tools.base import ToolContext, ToolError
from .app import App

ACCESS_TOKEN = os.environ.get("PHAI_ACCESS_TOKEN", "")


def _check_token(request: Request) -> None:
    if not ACCESS_TOKEN:
        return
    if request.headers.get("X-Access-Token") != ACCESS_TOKEN:
        raise HTTPException(status_code=401, detail="missing or invalid access token")


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
    fastapi = FastAPI(title="PHANTOM + CODED", version="0.1.0", docs_url="/api/docs")

    @fastapi.middleware("http")
    async def token_middleware(request: Request, call_next):
        if request.url.path.startswith(("/api/", "/ws")):
            _check_token(request)
        return await call_next(request)

    # ------------------------------------------------------------------ status
    @fastapi.get("/api/status")
    async def status():
        provider_statuses = {}
        for agent_id in AGENTS:
            try:
                provider_statuses[agent_id] = await app.provider_status(agent_id)
            except Exception as exc:  # noqa: BLE001
                provider_statuses[agent_id] = {"ok": False, "detail": str(exc)[:200]}
        return {
            "version": "0.1.0",
            "uptime_s": round(app.uptime_seconds(), 1),
            "killswitch": app.killswitch.to_dict(),
            "providers": provider_statuses,
            "agents": [
                {
                    **identity_summary()[0 if agent_id == "phantom" else 1],
                    "provider": provider_statuses.get(agent_id, {}).get("provider"),
                    "model": app.providers[agent_id].model,
                    "connected": provider_statuses.get(agent_id, {}).get("ok", False),
                }
                for agent_id in AGENTS
            ],
            "active_runs": {a: app.agents[a].active_runs() for a in AGENTS},
            "pending_confirmations": {a: len(await app.confirmations.pending_for_agent(a))
                                      for a in AGENTS},
        }

    @fastapi.get("/api/agents")
    async def agents():
        out = []
        for agent_id in AGENTS:
            meta = identity_summary()[0 if agent_id == "phantom" else 1]
            out.append({
                "id": agent_id,
                "display_name": meta["display_name"],
                "emoji": meta["emoji"],
                "tagline": meta["tagline"],
                "model": app.providers[agent_id].model,
                "provider": app.providers[agent_id].name,
                "api_key_configured": bool(app.secrets.get(KEY_ENV[agent_id])),
            })
        return {"agents": out}

    # ------------------------------------------------------------ conversations
    @fastapi.get("/api/conversations")
    async def list_conversations(agent: str = "phantom", limit: int = 100):
        if agent not in AGENTS:
            raise HTTPException(400, "unknown agent")
        return {"conversations": await app.conversations.list_for_agent(agent, limit)}

    @fastapi.post("/api/conversations")
    async def create_conversation(body: ConversationCreate):
        if body.agent not in AGENTS:
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
        if agent_id not in AGENTS:
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
            "keys": {
                agent_id: {
                    "env": KEY_ENV[agent_id],
                    "configured": bool(app.secrets.get(KEY_ENV[agent_id])),
                    "masked": mask_key(app.secrets.get(KEY_ENV[agent_id])),
                }
                for agent_id in AGENTS
            },
        }

    @fastapi.put("/api/settings")
    async def put_setting(body: SettingUpdate):
        if body.key == "nvidia_api_key":
            if body.delete:
                app.secrets.delete(KEY_ENV[body.agent])
            elif body.value:
                app.secrets.set(KEY_ENV[body.agent], str(body.value))
            if body.agent in AGENTS:
                await app.rebuild_provider(body.agent)
            return {"ok": True}
        if body.delete:
            await app.settings.delete(body.key, body.agent)
        else:
            await app.settings.set(body.key, body.value, body.agent)
        if body.agent in AGENTS and body.key in ("model", "model.temperature", "model.max_tokens"):
            await app.rebuild_provider(body.agent)
        return {"ok": True}

    @fastapi.post("/api/settings/rebuild-provider")
    async def rebuild_provider(body: AgentIdBody):
        if body.agent not in AGENTS:
            raise HTTPException(400, "unknown agent")
        return await app.rebuild_provider(body.agent)

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
        for agent_id in AGENTS:
            for run_id in list(app.agents[agent_id].active_runs()):
                app.agents[agent_id].cancel(run_id)
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
        stt = await app.settings.get("voice.stt", "*", {"provider": "browser"})
        tts = await app.settings.get("voice.tts", "*", {"provider": "browser"})
        return {"stt": {"provider": stt.get("provider", "browser")},
                "tts": {"provider": tts.get("provider", "browser"),
                        "voice": tts.get("voice", "")}}

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

    return fastapi
