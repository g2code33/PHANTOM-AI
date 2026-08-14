"""Evolution analysis: continuously analyze the system's performance and
produce optimization opportunities + daily/weekly self-audit reports.

Tracked signals: successful/failed tasks, repeated failures, completion time,
API latency/failures, token usage, estimated cost, tool failures, handoff
failures, user corrections, approvals/rejections, verification failures,
frequently repeated tasks, unused capabilities.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import now_iso


class EvolutionAnalyst:
    def __init__(self, audit: Any, tasks: Any, delegations: Any,
                 confirmations: Any, tool_registry: Any, cost_per_1k: dict[str, float] | None = None) -> None:
        self.audit = audit
        self.tasks = tasks
        self.delegations = delegations
        self.confirmations = confirmations
        self.tool_registry = tool_registry
        self.cost_per_1k = cost_per_1k or {"default": 0.0005}

    async def metrics(self, since_hours: int = 24) -> dict[str, Any]:
        since = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
        runs = await self.audit.query(event="agent.run_completed", limit=1000)
        runs = [r for r in runs if (r.get("ts") or "") >= since]
        tool_events = await self.audit.query(event="tool.executed", limit=2000)
        tool_events = [t for t in tool_events if (t.get("ts") or "") >= since]
        tool_errors = await self.audit.query(event="tool.error", limit=1000)
        tool_errors = [t for t in tool_errors if (t.get("ts") or "") >= since]
        api_errors = await self.audit.query(event="api.error", limit=500)
        api_errors = [a for a in api_errors if (a.get("ts") or "") >= since]
        delegations = await self.delegations.list(limit=1000)
        delegations = [d for d in delegations if (d.get("created_at") or "") >= since]
        approvals = await self.audit.query(event="confirmation.approved", limit=500)
        denials = await self.audit.query(event="confirmation.denied", limit=500)
        approvals = [a for a in approvals if (a.get("ts") or "") >= since]
        denials = [d for d in denials if (d.get("ts") or "") >= since]

        ok = sum(1 for r in runs if (r.get("detail") or {}).get("status") == "ok")
        failed = len(runs) - ok
        latencies = [r.get("latency_ms") for r in runs if r.get("latency_ms")]
        tool_latencies = [t.get("latency_ms") for t in tool_events if t.get("latency_ms")]

        tool_error_counter: Counter = Counter()
        for e in tool_errors:
            tool_error_counter[(e.get("detail") or {}).get("tool", "?")] += 1
        api_kinds: Counter = Counter()
        for e in api_errors:
            api_kinds[(e.get("detail") or {}).get("kind", "?")] += 1
        handoff_failures = sum(1 for d in delegations
                               if d.get("status") in ("failed", "timed_out", "blocked"))

        tokens = 0
        for r in runs:
            usage = (r.get("detail") or {}).get("usage") or (r.get("tokens") or {})
            tokens += int(usage.get("total_tokens") or 0) if isinstance(usage, dict) else 0
        cost = tokens / 1000 * self.cost_per_1k.get("default", 0.0005)

        return {
            "window_hours": since_hours,
            "runs": len(runs), "succeeded": ok, "failed": failed,
            "success_rate": round(ok / len(runs), 3) if runs else None,
            "avg_run_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else None,
            "avg_tool_latency_ms": round(sum(tool_latencies) / len(tool_latencies), 1) if tool_latencies else None,
            "api_errors": len(api_errors), "api_error_kinds": dict(api_kinds),
            "tool_errors": len(tool_errors), "tool_error_breakdown": dict(tool_error_counter),
            "handoff_failures": handoff_failures,
            "approvals": len(approvals), "denials": len(denials),
            "token_usage": tokens, "estimated_cost_usd": round(cost, 4),
            "computed_at": now_iso(),
        }

    async def opportunities(self, since_hours: int = 24, limit: int = 10) -> list[dict[str, Any]]:
        m = await self.metrics(since_hours)
        out: list[dict[str, Any]] = []

        for tool, count in m["tool_error_breakdown"].items():
            if count >= 3:
                out.append({
                    "kind": "tool_reliability",
                    "title": f"Tool `{tool}` failed {count}x in {since_hours}h",
                    "suggestion": "Add retry/fallback handling or verify arguments; consider "
                                  "model guidance to avoid this tool when inappropriate.",
                    "priority": "high" if count >= 8 else "medium",
                    "evidence": {"tool": tool, "failures": count},
                })
        if m["avg_tool_latency_ms"] and m["avg_tool_latency_ms"] > 3000:
            out.append({
                "kind": "latency",
                "title": f"Average tool latency {m['avg_tool_latency_ms']:.0f} ms",
                "suggestion": "Check for slow tools/network calls; cache or parallelize.",
                "priority": "medium", "evidence": m,
            })
        if m["api_errors"] >= 5:
            out.append({
                "kind": "api_reliability",
                "title": f"{m['api_errors']} API errors in {since_hours}h "
                         f"({m['api_error_kinds']})",
                "suggestion": "Check provider key/quota; verify network; consider retry tuning.",
                "priority": "high", "evidence": m,
            })
        if m["handoff_failures"] >= 3:
            out.append({
                "kind": "handoff",
                "title": f"{m['handoff_failures']} delegation/handoff failures",
                "suggestion": "Review escalation routes; add fallback agents.",
                "priority": "medium", "evidence": m,
            })
        if m["denials"] >= 5:
            out.append({
                "kind": "permissions",
                "title": f"{m['denials']} user denials — permission friction",
                "suggestion": "Review permission defaults for tools being denied repeatedly.",
                "priority": "medium", "evidence": m,
            })
        if m["estimated_cost_usd"] and m["estimated_cost_usd"] > 1.0:
            out.append({
                "kind": "cost",
                "title": f"Estimated spend ${m['estimated_cost_usd']:.2f} in {since_hours}h",
                "suggestion": "Enable model routing (fast model for simple tasks) to cut cost.",
                "priority": "low", "evidence": m,
            })
        # unused capabilities
        used = { (e.get("detail") or {}).get("tool") for e in await self.audit.query(
            event="tool.executed", limit=2000) }
        available = {t.name for t in self.tool_registry.list()}
        unused = sorted(available - used - {"run_command", "read_file"})[:5]
        if unused:
            out.append({
                "kind": "unused_capabilities",
                "title": "Unused capabilities",
                "suggestion": "Tools never used recently: " + ", ".join(unused),
                "priority": "low", "evidence": {"unused": unused},
            })
        return out[:limit]

    async def generate_report(self, period: str = "daily") -> str:
        hours = 24 if period == "daily" else 168
        m = await self.metrics(since_hours=hours)
        opps = await self.opportunities(since_hours=hours, limit=8)
        lines = [
            f"# Evolution Self-Audit ({period})",
            f"Generated: {now_iso()}\n",
            "## Metrics",
            f"- Runs: {m['runs']} ({m['succeeded']} ok / {m['failed']} failed) — "
            f"success rate {m['success_rate'] or 'n/a'}",
            f"- Avg run latency: {m['avg_run_latency_ms'] or 'n/a'} ms · "
            f"avg tool latency: {m['avg_tool_latency_ms'] or 'n/a'} ms",
            f"- API errors: {m['api_errors']} ({m['api_error_kinds']})",
            f"- Tool errors: {m['tool_errors']} → {m['tool_error_breakdown']}",
            f"- Handoff failures: {m['handoff_failures']} · approvals {m['approvals']} / "
            f"denials {m['denials']}",
            f"- Tokens: {m['token_usage']} · est. cost ${m['estimated_cost_usd']}",
        ]
        if opps:
            lines.append("\n## Optimization opportunities")
            for o in opps:
                lines.append(f"- [{o['priority']}] {o['title']} — {o['suggestion']}")
        else:
            lines.append("\n## Optimization opportunities\n- None detected. System is healthy.")
        lines.append("\n> Reports are advisory. High-risk changes require explicit approval.")
        return "\n".join(lines)
