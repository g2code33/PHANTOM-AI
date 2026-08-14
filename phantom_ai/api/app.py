"""Application container: wires storage, providers, agents, tools, permissions,
memory, delegation, tasks, heartbeat into one object. The FastAPI layer talks
only to this."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Optional

from ..agents.core import Agent, AgentRunResult
from ..agents.delegation import DelegationManager
from ..config import AGENTS, DB_PATH, DATA_DIR, KEY_ENV, SecretsStore, SettingsStore
from ..core.events import EventBus
from ..core.killswitch import KillSwitch
from ..heartbeat.scheduler import HeartbeatScheduler
from ..memory.retriever import MemoryRetriever
from ..permissions.confirm import ConfirmationManager
from ..permissions.policy import PermissionManager
from ..providers import build_provider, create_provider
from ..providers.base import ModelProvider
from ..storage.conversations import ConversationStore, MemoryStore
from ..storage.db import Database
from ..storage.ops import (
    AuditStore,
    ConfirmationStore,
    DelegationStore,
    NotificationStore,
    ScheduleStore,
    TaskStore,
)
from ..tasks.manager import TaskManager
from ..tools import build_registry


class App:
    """Holds every subsystem. One instance = the whole application."""

    def __init__(self, db_path: Optional[str] = None, data_dir: Optional[str] = None,
                 workspace_root: Optional[str] = None) -> None:
        self.db_path = Path(db_path) if db_path else DB_PATH
        self.data_dir = Path(data_dir) if data_dir else DATA_DIR
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_root = workspace_root or str(Path.home())
        self.started_at = time.time()

        self.db = Database(self.db_path)
        self.secrets = SecretsStore(self.data_dir / "secrets.json")
        self.events = EventBus()

        self.settings: SettingsStore | None = None
        self.audit: AuditStore | None = None
        self.killswitch: KillSwitch | None = None
        self.confirmations: ConfirmationManager | None = None
        self.confirmation_store: ConfirmationStore | None = None
        self.registry = build_registry()
        self.agents: dict[str, Agent] = {}
        self.providers: dict[str, ModelProvider] = {}
        self.delegations: DelegationManager | None = None
        self.tasks: TaskManager | None = None
        self.scheduler: HeartbeatScheduler | None = None
        self.notifications: NotificationStore | None = None
        self.conversations: ConversationStore | None = None
        self.memories: MemoryStore | None = None
        self.retriever: MemoryRetriever | None = None
        self.permissions: PermissionManager | None = None

    # ------------------------------------------------------------------
    async def startup(self) -> None:
        await self.db.connect()
        self.settings = SettingsStore(self.db)
        self.audit = AuditStore(self.db)
        self.killswitch = KillSwitch(self.audit)
        self.confirmation_store = ConfirmationStore(self.db)
        self.confirmations = ConfirmationManager(self.confirmation_store)
        self.conversations = ConversationStore(self.db)
        self.memories = MemoryStore(self.db)
        self.notifications = NotificationStore(self.db)
        self.retriever = MemoryRetriever(self.memories, self.conversations, self.settings)

        self.permissions = PermissionManager(
            registry=self.registry, settings=self.settings, audit=self.audit,
            events=self.events, confirmations=self.confirmations,
            killswitch=self.killswitch,
        )

        # agents (providers built per identity)
        for agent_id in AGENTS:
            provider = create_provider(agent_id, self.secrets)
            self.providers[agent_id] = provider
            self.agents[agent_id] = Agent(
                agent_id=agent_id, provider=provider, registry=self.registry,
                permission_manager=self.permissions, confirmations=self.confirmations,
                conversation_store=self.conversations, memory_store=self.memories,
                retriever=self.retriever, audit=self.audit, events=self.events,
                settings=self.settings, killswitch=self.killswitch,
                secrets=self.secrets, notifications=self.notifications,
            )

        # delegation manager (runner bound to agents; resolves the circular dep)
        self.delegations = DelegationManager(
            store=DelegationStore(self.db), settings=self.settings, audit=self.audit,
            events=self.events, agent_runner=self._run_agent, killswitch=self.killswitch,
        )
        for agent in self.agents.values():
            agent.delegation_manager = self.delegations

        self.tasks = TaskManager(TaskStore(self.db), self.settings, self.events,
                                 self.killswitch)
        self.scheduler = HeartbeatScheduler(
            store=ScheduleStore(self.db), settings=self.settings,
            task_manager=self.tasks, agent_runner=self._run_agent, events=self.events,
            killswitch=self.killswitch, notification_store=self.notifications,
        )

    async def shutdown(self) -> None:
        if self.scheduler:
            await self.scheduler.stop()
        for provider in self.providers.values():
            close = getattr(provider, "aclose", None)
            if close:
                try:
                    await close()
                except Exception:  # noqa: BLE001
                    pass
        await self.db.close()

    # ------------------------------------------------------------------
    async def _run_agent(self, agent_id: str, conversation_id: str, user_text: str,
                         session_id: str = "", mode: str = "chat",
                         permissions_override: Optional[dict] = None) -> dict:
        agent = self.agents[agent_id]
        result = await agent.run(
            conversation_id=conversation_id, user_text=user_text, session_id=session_id,
            mode=mode, permissions_override=permissions_override,
        )
        return result.to_dict()

    async def start_chat(self, agent_id: str, conversation_id: str, text: str,
                         session_id: str = "") -> dict:
        """Kick off a chat run in the background; events stream over WS."""
        run_id = _new_run_id()
        agent = self.agents[agent_id]
        task = asyncio.ensure_future(
            agent.run(conversation_id=conversation_id, user_text=text,
                      session_id=session_id, mode="chat", run_id=run_id)
        )
        task.add_done_callback(lambda _t: None)
        return {"run_id": run_id, "conversation_id": conversation_id, "agent": agent_id}

    def cancel_run(self, run_id: str) -> bool:
        for agent in self.agents.values():
            if agent.cancel(run_id):
                return True
        return False

    async def rebuild_provider(self, agent_id: str) -> dict:
        from ..config import DEFAULT_MODELS

        model = (await self.settings.get("model", agent_id, DEFAULT_MODELS[agent_id]))
        provider = build_provider(
            agent_id,
            api_key=self.secrets.get(KEY_ENV[agent_id]) or "",
            model=model or DEFAULT_MODELS[agent_id],
        )
        old = self.providers.get(agent_id)
        if old and getattr(old, "name", "") == "nvidia":
            close = getattr(old, "aclose", None)
            if close:
                try:
                    await close()
                except Exception:  # noqa: BLE001
                    pass
        self.providers[agent_id] = provider
        self.agents[agent_id].provider = provider
        return {"provider": provider.name, "model": provider.model}

    async def provider_status(self, agent_id: str, cached_ok: bool = True) -> dict:
        return await self.providers[agent_id].check()

    def uptime_seconds(self) -> float:
        return time.time() - self.started_at


def _new_run_id() -> str:
    import uuid

    return uuid.uuid4().hex
