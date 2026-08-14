"""TIER 11 — Evolution & System Intelligence: brains/capability registry,
graph engine, loop engineering, model routing, proposals, snapshots, analyst."""

from __future__ import annotations

import asyncio
import os

import pytest

from phantom_ai.api.app import App
from phantom_ai.brains.definitions import BrainDefinition
from phantom_ai.core.killswitch import KillSwitch
from phantom_ai.loops.engine import LoopConfig, LoopEngine
from phantom_ai.providers.router import ModelRouter
from phantom_ai.tools.base import PermissionLevel


async def test_seeded_brains_registered(app):
    instance, _state, _wd = app
    ids = await instance.brains.ids()
    assert {"phantom", "coded", "evolution", "health"} <= set(ids)
    # specialist catalogue seeded
    assert {"planner", "tutor", "research", "security", "critic", "execution"} <= set(ids)
    evolution = await instance.brains.get("evolution")
    assert evolution["role"] == "evolution"
    assert evolution["name"] == "Evolution"
    # evolution has its own live agent wired to the ecosystem services
    agent = instance.agents["evolution"]
    assert agent.brain_registry is not None
    assert agent.graph is not None
    assert agent.router is not None
    assert agent.analyst is not None
    # its own memory namespace
    await instance.memories.add("evolution", "evolution-specific fact", kind="fact")
    rows = await instance.memories.list(agent="evolution")
    assert any("evolution-specific" in m["content"] for m in rows)


async def test_dynamic_brain_registration_and_restart(app, workdir):
    instance, _state, _wd = app
    definition = BrainDefinition(
        brain_id="researcher", name="Researcher", role="research",
        description="Web research specialist",
        system_prompt="You are a research brain. Use web tools.",
        tools=["web_search", "fetch_webpage"],
    )
    result = await instance.brains.register(definition, created_by="system",
                                            require_approval=False)
    assert result["agent_created"] is True
    assert "researcher" in instance.agents
    assert instance.agents["researcher"].tool_allowlist == {"web_search", "fetch_webpage"}

    # persists across restart
    await instance.shutdown()
    app2 = App(db_path=os.path.join(workdir, "test.db"),
               data_dir=os.path.join(workdir, "data"), workspace_root=workdir)
    await app2.startup()
    try:
        assert "researcher" in app2.agents
        assert "researcher" in await app2.brains.ids()
    finally:
        await app2.shutdown()


async def test_privileged_brain_requires_approval(app):
    instance, _state, _wd = app
    definition = BrainDefinition(
        brain_id="power", name="Power",
        system_prompt="You are powerful.",
        permissions={"write_file": "safe_action"})
    task = asyncio.create_task(
        instance.brains.register(definition, created_by="evolution", require_approval=True))
    pending = None
    for _ in range(100):
        all_pending = []
        for agent in ("phantom", "coded", "evolution"):
            all_pending += await instance.confirmations.pending_for_agent(agent)
        if all_pending:
            pending = all_pending[0]
            break
        await asyncio.sleep(0.02)
    assert pending is not None and pending["tool_name"] == "register_brain"
    # deny → registration refused
    await instance.confirmations.decide(pending["id"], False)
    with pytest.raises((PermissionError, ValueError)):
        await asyncio.wait_for(task, timeout=10)


async def test_brain_health_computed(app):
    instance, _state, _wd = app
    await instance.audit.record("phantom", "agent.run_completed",
                                {"status": "ok"}, latency_ms=120)
    await instance.audit.record("phantom", "agent.run_completed",
                                {"status": "failed"}, latency_ms=300)
    health = await instance.brain_health.compute("phantom", window_hours=24)
    assert health["tasks"] == 2
    assert health["succeeded"] == 1
    assert health["success_rate"] == 0.5
    assert health["avg_latency_ms"] == 210
    assert health["state"] == "degraded"


async def test_graph_engine_track_query_path(app):
    instance, _state, _wd = app
    user = await instance.graph.track("user", "alice", {"origin": "local"})
    project = await instance.graph.track("project", "Code Rx",
                                         {"tech": "FastAPI"})
    task = await instance.graph.track("task", "fix login", agent="phantom")
    await instance.graph.relate(user["node_id"], project["node_id"], "has_project")
    await instance.graph.relate(project["node_id"], task["node_id"], "has_task")

    stats = await instance.graph.stats()
    assert stats["nodes"] == 3 and stats["edges"] == 2

    neigh = await instance.graph.related(user["node_id"], depth=2)
    labels = {n["label"] for n in neigh}
    assert "Code Rx" in labels and "fix login" in labels

    path = await instance.graph.path_between(user["node_id"], task["node_id"])
    assert len(path) == 3

    found = await instance.graph.store.search("Code Rx")
    assert found and found[0]["type"] == "project"

    relevant = await instance.graph.relevant_to("work on the Code Rx login task")
    assert any("Code Rx" in n["label"] or "fix login" in n["label"] for n in relevant)


async def test_graph_auto_tracking_during_agent_run(app, workdir):
    instance, state, _wd = app
    target = os.path.join(workdir, "g.txt")
    with open(target, "w") as fh:
        fh.write("graph data")

    async def handler(body):
        messages = body["messages"]
        if messages[-1]["role"] == "tool":
            return {"content": "done reading", "tool_calls": []}
        return {"content": "", "tool_calls": [{"id": "g1", "type": "function",
                                               "function": {"name": "read_file",
                                                            "arguments": {"path": target}}}]}

    state.handler = handler
    conv = await instance.conversations.create("phantom")
    await instance.agents["phantom"].run(conv["id"], "read g.txt")
    nodes = await instance.graph.store.by_type("tool")
    assert any(n["label"] == "read_file" for n in nodes)
    conv_nodes = await instance.graph.store.by_type("conversation")
    assert any(n["label"] == conv["id"] for n in conv_nodes)


async def test_model_router(app):
    instance, _state, _wd = app
    router = ModelRouter(instance.settings)
    assert await router.classify("hi") == "fast"
    assert await router.classify("please debug this complex architecture problem and "
                                 "design a strategy to refactor the multi-component system "
                                 "while migrating the security layer") == "strong"
    assert await router.classify("show me my files") == "default"
    # per-brain mapping override
    await instance.settings.set("model.router", {"fast": "nano-model", "default": "mid-model",
                                                 "strong": "big-model"}, "phantom")
    assert await router.choose("phantom", "hi") == "nano-model"
    assert await router.choose("phantom", "analyze this deeply") == "big-model"


async def _make_loop(app, runner, verifier=None):
    return LoopEngine(
        agent_runner=runner, settings=app.settings, audit=app.audit,
        events=app.events, killswitch=app.killswitch, delegation_manager=None,
        snapshots=app.snapshots, graph=app.graph, verifier=verifier,
        task_manager=None)


async def test_loop_engine_success(app):
    instance, _state, _wd = app
    calls = []

    async def runner(**kwargs):
        calls.append(kwargs["user_text"])
        return {"status": "ok", "content": "VERDICT: PASS the objective is met",
                "usage": {"total_tokens": 100}}

    async def verifier(objective, produced):
        return {"verdict": "PASS", "score": 1.0, "issues": [], "notes": ""}

    engine = await _make_loop(instance, runner, verifier)
    result = await engine.run("phantom", "fix the test", cfg=LoopConfig(max_iterations=5))
    assert result.status == "success"
    assert result.iterations >= 1


async def test_loop_engine_strategy_switch_and_limits(app):
    instance, _state, _wd = app

    async def runner(**kwargs):
        return {"status": "ok", "content": "nothing works",
                "usage": {"total_tokens": 100}}

    async def verifier(objective, produced):
        return {"verdict": "FAIL", "score": 0.2, "issues": ["broken"], "notes": ""}

    engine = await _make_loop(instance, runner, verifier)
    cfg = LoopConfig(max_iterations=6, failure_threshold=2, escalation_threshold=99,
                     strategy_switch=True)
    result = await engine.run("phantom", "do the impossible", cfg=cfg)
    assert result.status == "failed"
    assert result.iterations == 6  # bounded — never loops forever
    assert any("strategy switched" in line for line in result.log)


async def test_loop_engine_escalation(app):
    instance, _state, _wd = app
    delegated = []

    async def runner(**kwargs):
        return {"status": "ok", "content": "fail again", "usage": {}}

    async def verifier(objective, produced):
        return {"verdict": "FAIL", "score": 0.0, "issues": ["nope"], "notes": ""}

    engine = await _make_loop(instance, runner, verifier)
    engine.delegations = instance.delegations

    async def fake_delegate(**kwargs):
        delegated.append(kwargs)
        return {"status": "completed", "summary": "rescued by the other brain"}

    engine.delegations.delegate = fake_delegate
    cfg = LoopConfig(max_iterations=10, failure_threshold=4, escalation_threshold=3,
                     escalate_to="coded", strategy_switch=True)
    result = await engine.run("phantom", "hard task", cfg=cfg)
    assert delegated, "escalation must have happened"
    assert delegated[0]["target"] == "coded"


async def test_loop_engine_rollback_on_failure(app):
    instance, _state, _wd = app

    async def runner(**kwargs):
        return {"status": "ok", "content": "utter failure", "usage": {}}

    async def verifier(objective, produced):
        return {"verdict": "FAIL", "score": 0.0, "issues": ["x"], "notes": ""}

    engine = await _make_loop(instance, runner, verifier)
    before = await instance.snapshots.list()
    cfg = LoopConfig(max_iterations=3, rollback=True, failure_threshold=2,
                     escalation_threshold=99)
    result = await engine.run("phantom", "risky task", cfg=cfg)
    assert result.status == "failed"
    after = await instance.snapshots.list()
    assert len(after) > len(before)  # pre-loop snapshot created
    restored = await instance.audit.query(event="snapshot.restored")
    assert restored, "rollback restore must be recorded"


async def test_loop_engine_cancelled_by_killswitch(app):
    instance, _state, _wd = app

    async def runner(**kwargs):
        await asyncio.sleep(30)
        return {"status": "ok", "content": "VERDICT: PASS", "usage": {}}

    engine = await _make_loop(instance, runner)
    await instance.killswitch.engage("loop test")
    cfg = LoopConfig(max_iterations=5, timeout_sec=10)
    result = await engine.run("phantom", "stop me", cfg=cfg)
    assert result.status == "cancelled"
    await instance.killswitch.disengage()


async def test_proposals_lifecycle(app):
    instance, _state, _wd = app
    proposal = await instance.proposals.create(
        title="Speed up simple tasks",
        description="Route simple chats to the fast model.",
        kind="config",
        changes={"settings": {"phantom": {"model.router": {"fast": "fast-model"}}}},
        risk="low", created_by="evolution")
    assert proposal["status"] == "proposed"

    # approve + deploy
    from phantom_ai.api.server import _apply_proposal_changes

    await instance.proposals.set_status(proposal["id"], "approved")
    assert await _apply_proposal_changes(instance, proposal) is True
    await instance.proposals.set_status(proposal["id"], "deployed")
    cfg = await instance.settings.get("model.router", "phantom", {})
    assert cfg.get("fast") == "fast-model"
    assert (await instance.proposals.get(proposal["id"]))["status"] == "deployed"


async def test_snapshots_capture_and_restore(app):
    instance, _state, _wd = app
    await instance.settings.set("model.temperature", 0.9, "phantom")
    snap = await instance.snapshots.capture("before-change", "test snapshot")
    # mutate
    await instance.settings.set("model.temperature", 0.1, "phantom")
    await instance.settings.set("some.new.key", "x", "phantom")
    # restore
    await instance.snapshots.restore(snap["id"], reason="test")
    assert await instance.settings.get("model.temperature", "phantom") == 0.9
    assert await instance.settings.get("some.new.key", "phantom", None) is None
    restored = await instance.audit.query(event="snapshot.restored")
    assert restored


async def test_analyst_metrics_and_opportunities(app):
    instance, _state, _wd = app
    for i in range(4):
        await instance.audit.record("phantom", "tool.error",
                                    {"tool": "web_search", "kind": "network",
                                     "message": "timeout"})
    for i in range(5):
        await instance.audit.record("phantom", "api.error",
                                    {"kind": "rate_limit", "message": "429"})
    await instance.audit.record("phantom", "confirmation.denied", {"tool": "delete_file"})
    await instance.audit.record("phantom", "confirmation.denied", {"tool": "delete_file"})

    metrics = await instance.analyst.metrics(since_hours=24)
    assert metrics["tool_errors"] == 4
    assert metrics["tool_error_breakdown"].get("web_search") == 4
    assert metrics["api_errors"] == 5

    opps = await instance.analyst.opportunities(since_hours=24)
    titles = " ".join(o["title"] for o in opps)
    assert "web_search" in titles
    assert "API errors" in titles


async def test_self_audit_report(app):
    instance, _state, _wd = app
    report = await instance.analyst.generate_report("daily")
    assert "Evolution Self-Audit" in report
    assert "Metrics" in report
    assert "Optimization opportunities" in report


async def test_evolution_tools_via_registry(tool_ctx):
    ctx = tool_ctx("evolution")
    # brain_status
    res = await ctx.registry_get("brain_status").run(ctx)
    assert "phantom" in res.output and "coded" in res.output and "evolution" in res.output
    # graph_track + graph_query
    await ctx.registry_get("graph_track").run(ctx, node_type="project", label="Graph Project")
    q = await ctx.registry_get("graph_query").run(ctx, query="Graph Project")
    assert "Graph Project" in q.output
    # propose_improvement
    p = await ctx.registry_get("propose_improvement").run(
        ctx, title="Test proposal", description="from test", kind="config",
        changes={"settings": {}}, risk="low")
    assert "Proposal created" in p.output
    # list_proposals
    lp = await ctx.registry_get("list_proposals").run(ctx)
    assert "Test proposal" in lp.output
    # self_audit
    sa = await ctx.registry_get("self_audit").run(ctx, period="daily")
    assert "Evolution Self-Audit" in sa.output
