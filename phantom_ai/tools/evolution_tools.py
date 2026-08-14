"""Evolution & System Intelligence tools.

These tools give the Evolution brain (and, where useful, Phantom) real access
to: the capability registry, the graph engine, improvement proposals,
configuration snapshots, the loop engine, independent verification, and
self-audit reports. Everything is real and bounded — no simulation.
"""

from __future__ import annotations

from typing import Any

from ..loops.engine import LoopConfig, LoopEngine
from .base import PermissionLevel, ToolContext, ToolError, ToolResult, ToolSpec


async def _brain_status(ctx: ToolContext) -> ToolResult:
    registry = ctx.brain_registry
    if registry is None:
        raise ToolError("brain registry unavailable", kind="unavailable")
    rows = []
    for brain in await registry.list():
        s = registry.summary(brain)
        rows.append(
            f"{s['id']:12} {s['status']:8} model={s['model']} tools={s['tools']} "
            f"success={s['success_rate']} lat={s['avg_latency_ms']}ms "
            f"err={s['error_rate']} tasks={s['tasks']}")
    return ToolResult.ok("\n".join(rows) or "(no brains registered)",
                         data={"brains": [registry.summary(b) for b in await registry.list()]})


async def _graph_query(ctx: ToolContext, query: str = "", node_type: str = "",
                       node_id: str = "", depth: int = 2, source: str = "",
                       target: str = "") -> ToolResult:
    graph = ctx.graph
    if graph is None:
        raise ToolError("graph engine unavailable", kind="unavailable")
    if node_id:
        rows = await graph.related(node_id, depth=min(depth, 5))
        return ToolResult.ok(
            "\n".join(f"{n['type']}:{n['label']} (dist {n.get('distance')})" for n in rows),
            data={"nodes": rows})
    if source and target:
        path_ids = await graph.path_between(source, target)
        nodes = []
        for nid in path_ids:
            node = await graph.store.get_node(nid)
            if node:
                nodes.append(node)
        return ToolResult.ok(" → ".join(f"{n['type']}:{n['label']}" for n in nodes),
                             data={"path": nodes})
    if query:
        rows = await graph.store.search(query, node_type or None)
        return ToolResult.ok(
            "\n".join(f"{n['type']}:{n['label']}" for n in rows),
            data={"nodes": rows})
    if node_type:
        rows = await graph.store.by_type(node_type)
        return ToolResult.ok(
            "\n".join(f"{n['type']}:{n['label']}" for n in rows[:50]),
            data={"nodes": rows[:50]})
    stats = await graph.stats()
    return ToolResult.ok(f"graph: {stats['nodes']} nodes, {stats['edges']} edges "
                         f"({stats['by_type']})", data={"stats": stats})


async def _graph_track(ctx: ToolContext, node_type: str, label: str,
                       relation: str = "", target: str = "",
                       properties: dict | None = None) -> ToolResult:
    graph = ctx.graph
    if graph is None:
        raise ToolError("graph engine unavailable", kind="unavailable")
    result = await graph.track(node_type, label, properties or {},
                               agent=ctx.agent_id, relation=relation, target=target)
    return ToolResult.ok(f"tracked {node_type}:{label} → {result['node_id']}", data=result)


async def _propose_improvement(ctx: ToolContext, title: str, description: str,
                               kind: str = "config", changes: dict | None = None,
                               risk: str = "low") -> ToolResult:
    if ctx.proposals is None:
        raise ToolError("proposal store unavailable", kind="unavailable")
    snapshot_id = ""
    if ctx.snapshots is not None and kind != "workflow":
        try:
            snap = await ctx.snapshots.create(
                label=f"proposal:{title[:60]}",
                description="snapshot taken at proposal time")
            snapshot_id = snap["id"]
        except Exception:  # noqa: BLE001
            snapshot_id = ""
    proposal = await ctx.proposals.create(
        title=title, description=description, kind=kind, changes=changes or {},
        risk=risk, created_by=ctx.agent_id, snapshot_id=snapshot_id)
    await ctx.audit.record(ctx.agent_id, "proposal.created",
                           {"proposal_id": proposal["id"], "title": title[:200]})
    return ToolResult.ok(
        f"Proposal created: {title} (id {proposal['id']}, risk {risk}). "
        "It is advisory until approved/deployed.",
        data={"proposal": proposal})


async def _list_proposals(ctx: ToolContext, status: str = "") -> ToolResult:
    rows = await ctx.proposals.list(status or None)
    if not rows:
        return ToolResult.ok("No proposals.", data={"proposals": []})
    lines = [
        f"{p['id'][:8]} [{p['status']:10}] {p['title']} (risk {p['risk']}, by {p['created_by']})"
        for p in rows[:50]]
    return ToolResult.ok("\n".join(lines), data={"proposals": rows[:50]})


async def _create_snapshot(ctx: ToolContext, label: str = "",
                           description: str = "") -> ToolResult:
    snap = await ctx.snapshots.create(
        label=label or f"snapshot:{ctx.agent_id}",
        description=description or "manual snapshot")
    return ToolResult.ok(f"Snapshot created: {snap['id']} ({snap['label']})",
                         data={"snapshot": snap})


async def _rollback_snapshot(ctx: ToolContext, snapshot_id: str = "",
                             latest: bool = False) -> ToolResult:
    if latest:
        snaps = await ctx.snapshots.list()
        if not snaps:
            raise ToolError("no snapshots available to restore", kind="not_found")
        snapshot_id = snaps[0]["id"]
    await ctx.snapshots.restore(snapshot_id, reason=f"tool rollback by {ctx.agent_id}")
    return ToolResult.ok(f"Restored configuration from snapshot {snapshot_id}",
                         data={"snapshot_id": snapshot_id})


async def _run_loop_task(ctx: ToolContext, objective: str, context: str = "",
                         agent: str = "phantom", max_iterations: int = 5,
                         timeout_sec: int = 300, failure_threshold: int = 3,
                         escalate_to: str = "", rollback: bool = True) -> ToolResult:
    if ctx.loop_engine is None:
        raise ToolError("loop engine unavailable", kind="unavailable")
    if agent not in ctx.agent_ids:
        raise ToolError(f"unknown agent/brain: {agent}", kind="invalid")
    cfg = LoopConfig(
        max_iterations=max(1, min(max_iterations, 20)),
        timeout_sec=max(30, min(timeout_sec, 1800)),
        failure_threshold=max(1, min(failure_threshold, 10)),
        escalation_threshold=failure_threshold + 2,
        escalate_to=escalate_to if escalate_to in ("coded", "phantom") else "",
        rollback=rollback,
    )
    result = await ctx.loop_engine.run(ctx.agent_id, objective, context, cfg)
    if result.status != "success":
        return ToolResult(
            success=False,
            output=f"Loop {result.status}: {result.error or 'no success'} "
                   f"after {result.iterations} iterations (strategy {result.strategy})",
            data=result.to_dict())
    return ToolResult.ok(
        f"Loop succeeded after {result.iterations} iteration(s) "
        f"(strategy {result.strategy}, ~${result.estimated_cost:.4f}).\n\n{result.output[:3000]}",
        data=result.to_dict())


async def _verify_result(ctx: ToolContext, objective: str, produced: str) -> ToolResult:
    if ctx.verifier is None:
        raise ToolError("verifier unavailable", kind="unavailable")
    verification = await ctx.verifier(objective, produced)
    verdict = verification.get("verdict", "FAIL")
    return ToolResult(
        success=verdict == "PASS",
        output=(f"Verification: {verdict} (score {verification.get('score')})\n"
                f"Issues: {verification.get('issues') or 'none'}\n"
                f"Notes: {verification.get('notes') or ''}"),
        data=verification)


async def _self_audit(ctx: ToolContext, period: str = "daily") -> ToolResult:
    if ctx.analyst is None:
        raise ToolError("evolution analyst unavailable", kind="unavailable")
    report = await ctx.analyst.generate_report(period)
    return ToolResult.ok(report, data={"period": period, "report": report[:12000]})


async def _register_brain(ctx: ToolContext, brain_id: str, name: str = "",
                          role: str = "general", description: str = "",
                          system_prompt: str = "", tools: list[str] | None = None,
                          permissions: dict | None = None) -> ToolResult:
    from ..brains.definitions import BrainDefinition

    if ctx.brain_registry is None:
        raise ToolError("brain registry unavailable", kind="unavailable")
    definition = BrainDefinition(
        brain_id=brain_id, name=name, role=role, description=description,
        system_prompt=system_prompt, tools=tools, permissions=permissions or {})
    try:
        result = await ctx.brain_registry.register(
            definition, created_by=ctx.agent_id,
            require_approval=bool(permissions))
    except (ValueError, PermissionError) as exc:
        raise ToolError(str(exc), kind="permission" if isinstance(exc, PermissionError)
                        else "invalid") from exc
    return ToolResult.ok(
        f"Brain registered: {brain_id} (agent created: {result['agent_created']})",
        data=result)


def register_evolution_tools(registry) -> None:
    registry.register(ToolSpec(
        name="brain_status",
        description="List every registered brain with its health, model, tools and performance.",
        purpose="Inspect the capability registry", category="evolution",
        parameters={}, handler=_brain_status, permission=PermissionLevel.READ_ONLY, timeout=15,
        agents=("phantom", "coded", "evolution"),
    ))
    registry.register(ToolSpec(
        name="graph_query",
        description="Query the persistent knowledge graph: by label, type, neighbors of a node, "
                    "a path between two nodes, or overall stats.",
        purpose="Relationship discovery", category="evolution",
        parameters={"query": {"type": "string", "default": ""},
                    "node_type": {"type": "string", "default": ""},
                    "node_id": {"type": "string", "default": ""},
                    "depth": {"type": "integer", "minimum": 1, "maximum": 5, "default": 2},
                    "source": {"type": "string", "default": ""},
                    "target": {"type": "string", "default": ""}},
        handler=_graph_query, permission=PermissionLevel.READ_ONLY, timeout=15,
        agents=("phantom", "coded", "evolution"),
    ))
    registry.register(ToolSpec(
        name="graph_track",
        description="Record a node (and optional edge) in the knowledge graph.",
        purpose="Build the personal knowledge graph", category="evolution",
        parameters={"node_type": {"type": "string", "required": True},
                    "label": {"type": "string", "required": True},
                    "relation": {"type": "string", "default": ""},
                    "target": {"type": "string", "default": ""},
                    "properties": {"type": "object", "default": {}}},
        handler=_graph_track, permission=PermissionLevel.SAFE_ACTION, timeout=10,
        agents=("phantom", "coded", "evolution"),
    ))
    registry.register(ToolSpec(
        name="propose_improvement",
        description="Create an improvement proposal for the system (advisory; deployed only "
                    "after approval).",
        purpose="Self-improvement", category="evolution",
        parameters={"title": {"type": "string", "required": True},
                    "description": {"type": "string", "required": True},
                    "kind": {"type": "string", "enum": ["config", "workflow", "routing", "prompt"], "default": "config"},
                    "changes": {"type": "object", "default": {}},
                    "risk": {"type": "string", "enum": ["low", "medium", "high"], "default": "low"}},
        handler=_propose_improvement, permission=PermissionLevel.SAFE_ACTION, timeout=15,
        agents=("evolution", "phantom"),
    ))
    registry.register(ToolSpec(
        name="list_proposals",
        description="List improvement proposals (optionally by status).",
        purpose="Review proposed improvements", category="evolution",
        parameters={"status": {"type": "string", "enum": ["proposed", "approved", "deployed",
                                                          "rejected", "rolled_back", ""], "default": ""}},
        handler=_list_proposals, permission=PermissionLevel.READ_ONLY, timeout=10,
        agents=("phantom", "coded", "evolution"),
    ))
    registry.register(ToolSpec(
        name="create_snapshot",
        description="Snapshot the current system configuration (settings, permissions, brains) "
                    "so it can be restored later.",
        purpose="Version control for the system", category="evolution",
        parameters={"label": {"type": "string", "default": ""},
                    "description": {"type": "string", "default": ""}},
        handler=_create_snapshot, permission=PermissionLevel.CONFIRM_REQUIRED, timeout=15,
        agents=("evolution", "phantom"),
        required_permission_notes="Snapshots capture live configuration; confirmation required.",
    ))
    registry.register(ToolSpec(
        name="rollback_snapshot",
        description="Restore the system configuration from a snapshot (or the latest one).",
        purpose="Rollback", category="evolution",
        parameters={"snapshot_id": {"type": "string", "default": ""},
                    "latest": {"type": "boolean", "default": False}},
        handler=_rollback_snapshot, permission=PermissionLevel.HIGH_RISK, timeout=15,
        agents=("evolution",),
        required_permission_notes="Rollback rewrites configuration — high risk, requires confirmation.",
    ))
    registry.register(ToolSpec(
        name="run_loop_task",
        description="Run an autonomous task through the improvement loop "
                    "(observe/understand/plan/execute/test/verify/evaluate/learn/improve/repeat) "
                    "with strict iteration/timeout/cost/failure limits, strategy switching, "
                    "escalation and rollback.",
        purpose="Continuous task improvement", category="evolution",
        parameters={"objective": {"type": "string", "required": True},
                    "context": {"type": "string", "default": ""},
                    "agent": {"type": "string", "enum": ["phantom", "coded", "evolution"], "default": "phantom"},
                    "max_iterations": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
                    "timeout_sec": {"type": "integer", "minimum": 30, "maximum": 1800, "default": 300},
                    "failure_threshold": {"type": "integer", "minimum": 1, "maximum": 10, "default": 3},
                    "escalate_to": {"type": "string", "enum": ["", "coded", "phantom"], "default": ""},
                    "rollback": {"type": "boolean", "default": True}},
        handler=_run_loop_task, permission=PermissionLevel.CONFIRM_REQUIRED, timeout=1850,
        parallel_safe=False, agents=("evolution", "phantom"),
        required_permission_notes="Autonomous loops may use tools; confirmation required by default.",
    ))
    registry.register(ToolSpec(
        name="verify_result",
        description="Independently verify whether produced work satisfies an objective "
                    "(critic-style review).",
        purpose="Independent verification", category="evolution",
        parameters={"objective": {"type": "string", "required": True},
                    "produced": {"type": "string", "required": True}},
        handler=_verify_result, permission=PermissionLevel.READ_ONLY, timeout=120,
        agents=("phantom", "coded", "evolution"),
    ))
    registry.register(ToolSpec(
        name="self_audit",
        description="Generate the daily or weekly internal system self-audit report "
                    "(metrics + optimization opportunities).",
        purpose="Self-audit", category="evolution",
        parameters={"period": {"type": "string", "enum": ["daily", "weekly"], "default": "daily"}},
        handler=_self_audit, permission=PermissionLevel.READ_ONLY, timeout=30,
        agents=("evolution", "phantom"),
    ))
    registry.register(ToolSpec(
        name="register_brain",
        description="Register a new brain dynamically from a definition (id, name, role, system "
                    "prompt, tools). Privileged brains (with permission overrides) require "
                    "user authorization.",
        purpose="Brain creation system", category="evolution",
        parameters={"brain_id": {"type": "string", "required": True},
                    "name": {"type": "string", "default": ""},
                    "role": {"type": "string", "default": "general"},
                    "description": {"type": "string", "default": ""},
                    "system_prompt": {"type": "string", "required": True},
                    "tools": {"type": "array", "items": {"type": "string"}},
                    "permissions": {"type": "object", "default": {}}},
        handler=_register_brain, permission=PermissionLevel.CONFIRM_REQUIRED, timeout=60,
        agents=("evolution", "phantom"),
        required_permission_notes="Registering a brain activates a new agent; confirmation required.",
    ))
