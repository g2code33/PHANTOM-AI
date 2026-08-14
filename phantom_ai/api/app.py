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
from ..agents.identities import identity
from ..brains.definitions import BrainDefinition
from ..brains.health import BrainHealth
from ..brains.registry import BrainRegistry
from ..config import AGENTS, DB_PATH, DATA_DIR, KEY_ENV, SecretsStore, SettingsStore
from ..core.events import EventBus
from ..core.killswitch import KillSwitch
from ..evolution.analyst import EvolutionAnalyst
from ..evolution.snapshots import ConfigSnapshots
from ..graph.engine import GraphEngine
from ..heartbeat.scheduler import HeartbeatScheduler
from ..loops.engine import LoopEngine
from ..memory.retriever import MemoryRetriever
from ..permissions.confirm import ConfirmationManager
from ..permissions.policy import PermissionManager
from ..providers import build_provider, create_provider
from ..providers.base import ModelProvider
from ..providers.router import ModelRouter
from ..storage.conversations import ConversationStore, MemoryStore
from ..storage.db import Database
from ..storage.evolution import BrainStore, GraphStore, ProposalStore, SnapshotStore
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
        # Evolution & System Intelligence
        self.brains: BrainRegistry | None = None
        self.brain_health: BrainHealth | None = None
        self.graph: GraphEngine | None = None
        self.proposals: ProposalStore | None = None
        self.snapshots: ConfigSnapshots | None = None
        self.analyst: EvolutionAnalyst | None = None
        self.router: ModelRouter | None = None
        self.loops: LoopEngine | None = None
        self.verifier: Any = None

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

        # ---- Evolution & System Intelligence services ----------------------
        self.proposals = ProposalStore(self.db)
        snapshot_store = SnapshotStore(self.db)
        self.graph = GraphEngine(GraphStore(self.db), self.audit, self.events)
        self.router = ModelRouter(self.settings)
        self.analyst = EvolutionAnalyst(self.audit, TaskStore(self.db),
                                        DelegationStore(self.db),
                                        self.confirmations, self.registry)
        self.brain_health = BrainHealth(self.audit, TaskStore(self.db),
                                        DelegationStore(self.db))

        # ---- capability registry -------------------------------------------
        self.brains = BrainRegistry(
            store=BrainStore(self.db), settings=self.settings, audit=self.audit,
            events=self.events, tool_registry=self.registry,
            confirmations=self.confirmations, agent_factory=self._create_agent_from_definition,
        )
        self.snapshots = ConfigSnapshots(snapshot_store, self.settings, self.brains,
                                         self.audit, self.events)

        # seed builtin brains (phantom, coded, evolution) — plus any persisted
        # dynamically-registered brains from previous runs
        for agent_id in (*AGENTS, "evolution"):
            meta = identity(agent_id)
            definition = BrainDefinition(
                brain_id=agent_id, name=meta["display_name"],
                role="evolution" if agent_id == "evolution" else
                     ("general" if agent_id == "phantom" else "technical"),
                description=meta["tagline"], system_prompt=meta["system_prompt"],
                model=meta["default_model"], key_env=meta["key_env"],
                memory_scope=agent_id,
            )
            try:
                await self.brains.register(definition, created_by="system",
                                           require_approval=False)
            except ValueError:
                pass  # already registered (persisted brain)

        # ---- agents (providers built per identity) -------------------------
        for brain in await self.brains.list():
            await self._create_agent_from_definition(
                BrainDefinition.from_dict(brain))

        # delegation manager (runner bound to agents; resolves the circular dep)
        self.delegations = DelegationManager(
            store=DelegationStore(self.db), settings=self.settings, audit=self.audit,
            events=self.events, agent_runner=self._run_agent, killswitch=self.killswitch,
        )
        for agent in self.agents.values():
            agent.delegation_manager = self.delegations

        # ---- loop engine + verifier ----------------------------------------
        self.verifier = self._critic_verify
        self.loops = LoopEngine(
            agent_runner=self._run_agent, settings=self.settings, audit=self.audit,
            events=self.events, killswitch=self.killswitch,
            delegation_manager=self.delegations, snapshots=self.snapshots,
            graph=self.graph, verifier=self.verifier, task_manager=None,
        )
        # late-wire services created after the agents were built
        for agent in self.agents.values():
            agent.verifier = self.verifier
            agent.loop_engine = self.loops

        self.tasks = TaskManager(TaskStore(self.db), self.settings, self.events,
                                 self.killswitch)
        self.scheduler = HeartbeatScheduler(
            store=ScheduleStore(self.db), settings=self.settings,
            task_manager=self.tasks, agent_runner=self._run_agent, events=self.events,
            killswitch=self.killswitch, notification_store=self.notifications,
        )
        await self._seed_self_audit_schedules()

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
    async def _create_agent_from_definition(self, definition: BrainDefinition) -> dict:
        """Create a live Agent for a brain definition (builtin or dynamic)."""
        from ..config import DEFAULT_MODELS

        agent_id = definition.brain_id
        key = (self.secrets.get(definition.key_env or "") or
               self.secrets.get(KEY_ENV.get(agent_id, "")) or
               self.secrets.get(KEY_ENV["phantom"]))
        model = definition.model or DEFAULT_MODELS.get(agent_id, DEFAULT_MODELS["phantom"])
        provider = build_provider(agent_id, api_key=key or "", model=model)

        agent = Agent(
            agent_id=agent_id, provider=provider, registry=self.registry,
            permission_manager=self.permissions, confirmations=self.confirmations,
            conversation_store=self.conversations, memory_store=self.memories,
            retriever=self.retriever, audit=self.audit, events=self.events,
            settings=self.settings, killswitch=self.killswitch,
            secrets=self.secrets, notifications=self.notifications,
        )
        if definition.tools:
            agent.tool_allowlist = set(definition.tools)
        agent.graph = self.graph
        agent.router = self.router
        agent.verifier = self.verifier
        agent.brain_registry = self.brains
        agent.proposals = self.proposals
        agent.snapshots = self.snapshots
        agent.loop_engine = self.loops
        agent.analyst = self.analyst

        self.providers[agent_id] = provider
        self.agents[agent_id] = agent
        return {"id": agent_id, "model": model}

    async def _critic_verify(self, objective: str, produced: str) -> dict:
        agent = self.agents.get("phantom")
        if agent is None:
            return {"verdict": "FAIL", "score": 0.0,
                    "issues": ["no critic agent available"], "notes": ""}
        return await agent.verify(objective, produced)

    async def _seed_self_audit_schedules(self) -> None:
        """Daily + weekly self-audit for the Evolution brain (idempotent)."""
        schedules = ScheduleStore(self.db)
        existing = await schedules.list("evolution")
        names = {s["name"] for s in existing}
        seeds = [
            ("Daily self-audit", "daily at 08:00",
             "Run the self_audit tool with period=daily. Reply with the report summary "
             "and any high-priority opportunities."),
            ("Weekly self-audit", "weekly at 09:00",
             "Run the self_audit tool with period=weekly. Reply with the report summary "
             "and proposed improvements."),
        ]
        for name, expression, prompt in seeds:
            if name not in names:
                sched = await schedules.create("evolution", name, expression, prompt,
                                               quiet_start="22:00", quiet_end="07:00")
                from ..heartbeat.scheduler import next_run_at

                await schedules.update(sched["id"], next_run_at=next_run_at(expression))

    # ------------------------------------------------------------------
    async def _run_agent(self, agent_id: str, conversation_id: str, user_text: str,
                         session_id: str = "", mode: str = "chat",
                         permissions_override: Optional[dict] = None) -> dict:
        agent = self.agents.get(agent_id)
        if agent is None:
            return {"status": "error", "error": f"unknown agent/brain: {agent_id}"}
        result = await agent.run(
            conversation_id=conversation_id, user_text=user_text, session_id=session_id,
            mode=mode, permissions_override=permissions_override,
        )
        return result.to_dict()

    async def brain_ids(self) -> list[str]:
        if self.brains is not None:
            return await self.brains.ids()
        return list(self.agents)

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

        model = (await self.settings.get("model", agent_id, DEFAULT_MODELS.get(agent_id, DEFAULT_MODELS["phantom"])))
        key = (self.secrets.get(KEY_ENV.get(agent_id, "")) or
               self.secrets.get(KEY_ENV["phantom"]))
        provider = build_provider(
            agent_id,
            api_key=key or "",
            model=model or DEFAULT_MODELS.get(agent_id, DEFAULT_MODELS["phantom"]),
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
