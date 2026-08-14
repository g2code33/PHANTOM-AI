"""Loop Engine — continuous task-improvement orchestration.

Every significant autonomous task can pass through:
OBSERVE → UNDERSTAND → PLAN → EXECUTE → TEST → VERIFY → EVALUATE → LEARN →
IMPROVE → REPEAT

The loop NEVER runs forever: max iterations, timeout, cost budget, failure
threshold, success criteria, approval threshold, rollback, cancellation,
escalation and alternative-strategy selection are all enforced. When a strategy
fails repeatedly, the engine switches strategy instead of repeating the same
action; on repeated failure it can escalate to another brain; when rollback is
enabled a configuration snapshot taken before the loop is restored on failure.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from ..config import now_iso

PHASES = ["OBSERVE", "UNDERSTAND", "PLAN", "EXECUTE", "TEST", "VERIFY",
          "EVALUATE", "LEARN", "IMPROVE", "REPEAT"]

STRATEGIES = [
    "default",
    "decomposition: break the objective into small verifiable steps and solve each one",
    "alternative: approach the objective from a different angle (different tool order, different interpretation)",
    "minimal: reduce scope to the smallest change that satisfies the success criteria",
    "evidence-first: gather observable evidence (files, outputs, logs) before making claims",
]

LOOP_PROMPT_TEMPLATE = """You are executing a task inside a controlled improvement loop.

TASK (task {iteration}/{max_iterations}):
{objective}

CONTEXT:
{context}

CURRENT PHASE: {phase}
STRATEGY: {strategy}

SUCCESS CRITERIA:
{success_criteria}

Loop rules:
- Work within this single phase only. Use your tools for anything real.
- If the phase is VERIFY or EVALUATE: determine explicitly whether the success
  criteria are met. Reply starting with "VERDICT: PASS" or "VERDICT: FAIL".
- If the phase is LEARN: state what was learned and what to improve.
- If the phase is IMPROVE: propose the next iteration's concrete changes.
- Never claim success without evidence.
"""


@dataclass
class LoopConfig:
    max_iterations: int = 6
    timeout_sec: int = 600
    cost_budget: float = 5.0              # estimated $ budget
    failure_threshold: int = 3            # consecutive failures before strategy switch
    escalation_threshold: int = 5         # failures before escalating to another brain
    escalate_to: str = ""                 # brain id (e.g. "coded")
    confidence_threshold: float = 0.7
    success_criteria: str = "The objective is achieved and verifiable."
    approval_threshold: int = 0           # iterations before requiring user approval (0=never)
    rollback: bool = True                 # restore pre-loop snapshot on failure
    strategy_switch: bool = True
    verify: bool = True                   # run independent verification at TEST/VERIFY


@dataclass
class LoopResult:
    status: str = "running"               # success | failed | timeout | cancelled | approval_required
    objective: str = ""
    iterations: int = 0
    strategy: str = "default"
    output: str = ""
    verification: Optional[dict[str, Any]] = None
    error: str = ""
    log: list[str] = field(default_factory=list)
    estimated_cost: float = 0.0
    elapsed_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class LoopEngine:
    def __init__(self, agent_runner: Callable[..., Awaitable[dict]],
                 settings: Any, audit: Any, events: Any, killswitch: Any,
                 delegation_manager: Any, snapshots: Any, graph: Any = None,
                 verifier: Any = None, task_manager: Any = None,
                 cost_per_1k: dict[str, float] | None = None) -> None:
        self.agent_runner = agent_runner
        self.settings = settings
        self.audit = audit
        self.events = events
        self.killswitch = killswitch
        self.delegations = delegation_manager
        self.snapshots = snapshots
        self.graph = graph
        self.verifier = verifier          # async (objective, produced) -> dict
        self.task_manager = task_manager
        self.cost_per_1k = cost_per_1k or {"default": 0.0005}

    async def run(self, agent_id: str, objective: str, context: str = "",
                  cfg: LoopConfig | None = None) -> LoopResult:
        cfg = cfg or LoopConfig()
        started = time.monotonic()
        result = LoopResult(objective=objective)
        result.log.append(f"[loop] started by {agent_id} at {now_iso()}")

        # pre-loop snapshot for rollback
        snapshot = None
        if cfg.rollback and self.snapshots is not None:
            try:
                snapshot = await self.snapshots.create(
                    label=f"pre-loop:{objective[:60]}",
                    description="automatic snapshot before loop execution")
                result.log.append(f"[loop] snapshot {snapshot['id']} created")
            except Exception:  # noqa: BLE001
                snapshot = None

        strategy = "default"
        failures = 0
        total_cost = 0.0
        iteration = 0

        try:
            while iteration < cfg.max_iterations:
                if self.killswitch.is_engaged():
                    result.status = "cancelled"
                    result.error = "kill switch engaged"
                    break
                iteration += 1
                result.iterations = iteration
                phase = PHASES[(iteration - 1) % len(PHASES)]

                prompt = LOOP_PROMPT_TEMPLATE.format(
                    iteration=iteration, max_iterations=cfg.max_iterations,
                    objective=objective, context=context or "(none)",
                    phase=phase, strategy=strategy,
                    success_criteria=cfg.success_criteria)

                run = None
                try:
                    run = await asyncio.wait_for(
                        self.agent_runner(agent_id=agent_id, conversation_id=_loop_conv_id(),
                                          user_text=prompt, session_id=f"loop:{id(self)}",
                                          mode="loop"),
                        timeout=min(cfg.timeout_sec, 300))
                except asyncio.TimeoutError:
                    result.error = f"iteration {iteration} timed out"
                    result.log.append(result.error)
                    failures += 1
                    if failures >= cfg.failure_threshold and cfg.strategy_switch:
                        strategy = _next_strategy(strategy)
                        failures = 0
                        result.log.append(f"[loop] strategy switched to: {strategy}")
                    continue
                except Exception as exc:  # noqa: BLE001
                    result.error = f"iteration {iteration} failed: {exc}"
                    result.log.append(result.error)
                    failures += 1
                    continue

                output = (run or {}).get("content") or ""
                result.output = output
                result.estimated_cost = total_cost
                usage = (run or {}).get("usage") or {}
                total_cost += self._estimate_cost(agent_id, usage)
                result.log.append(f"[loop] iter {iteration} phase {phase} done "
                                  f"(cost ≈ ${total_cost:.4f})")

                # cost budget
                if total_cost > cfg.cost_budget:
                    result.status = "failed"
                    result.error = f"cost budget exceeded (${total_cost:.2f} > ${cfg.cost_budget:.2f})"
                    break

                # approval gate
                if cfg.approval_threshold and iteration >= cfg.approval_threshold:
                    # approval flow handled by caller-side confirmation; treat
                    # expiry as approval_required so nothing runs unattended
                    result.status = "approval_required"
                    result.error = f"approval required after {iteration} iterations"
                    break

                # verification at TEST/VERIFY phases
                verdict = "FAIL"
                if cfg.verify and phase in ("TEST", "VERIFY") and self.verifier is not None:
                    try:
                        verification = await self.verifier(objective, output)
                        result.verification = verification
                        result.log.append(f"[loop] verification: {verification.get('verdict')} "
                                          f"score={verification.get('score')}")
                        verdict = verification.get("verdict", "FAIL")
                    except Exception as exc:  # noqa: BLE001
                        result.log.append(f"[loop] verification error: {exc}")
                        verdict = "FAIL"
                elif "VERDICT: PASS" in output.upper():
                    verdict = "PASS"

                if verdict == "PASS":
                    result.status = "success"
                    result.strategy = strategy
                    result.log.append("[loop] SUCCESS — recording outcome")
                    await self._record_success(agent_id, objective, result)
                    break

                failures += 1
                # strategy switch on repeated failure
                if failures >= cfg.failure_threshold and cfg.strategy_switch:
                    strategy = _next_strategy(strategy)
                    failures = 0
                    result.log.append(f"[loop] strategy switched to: {strategy}")
                # escalation
                if failures >= cfg.escalation_threshold and cfg.escalate_to:
                    result.log.append(f"[loop] escalating to {cfg.escalate_to}")
                    if self.delegations is not None:
                        try:
                            esc = await self.delegations.delegate(
                                origin=agent_id, target=cfg.escalate_to,
                                objective=objective, context=result.output[:3000],
                                timeout_sec=min(cfg.timeout_sec, 600))
                            result.log.append(f"[loop] escalation result: {esc.get('status')}")
                            if esc.get("status") == "completed":
                                result.status = "success"
                                break
                        except Exception as exc:  # noqa: BLE001
                            result.log.append(f"[loop] escalation error: {exc}")
                    break  # escalation ends the loop
            else:
                result.status = "failed"
                result.error = f"max iterations ({cfg.max_iterations}) reached without success"
        finally:
            if result.status in ("failed", "timeout", "cancelled") and cfg.rollback and snapshot:
                try:
                    await self.snapshots.restore(snapshot["id"],
                                                 reason=f"loop rollback after {result.status}")
                    result.log.append(f"[loop] rolled back to snapshot {snapshot['id']}")
                except Exception as exc:  # noqa: BLE001
                    result.log.append(f"[loop] rollback failed: {exc}")

        result.elapsed_s = round(time.monotonic() - started, 2)
        result.estimated_cost = total_cost
        result.log.append(f"[loop] finished: {result.status} in {result.elapsed_s}s "
                          f"(cost ≈ ${total_cost:.4f})")
        await self.audit.record(agent_id, "loop.completed", {
            "status": result.status, "iterations": result.iterations,
            "strategy": result.strategy, "cost": total_cost,
            "objective": objective[:300], "error": result.error},
            latency_ms=result.elapsed_s * 1000)
        return result

    async def _record_success(self, agent_id: str, objective: str, result: LoopResult) -> None:
        if self.graph is not None:
            try:
                task_node = await self.graph.store.upsert_node(
                    "task", objective[:120], {"status": "success", "agent": agent_id}, agent_id)
                outcome = await self.graph.store.upsert_node(
                    "result", f"loop:{objective[:60]}",
                    {"status": "success", "iterations": result.iterations}, agent_id)
                await self.graph.store.upsert_edge(
                    task_node, outcome, "produces", {"agent": agent_id})
            except Exception:  # noqa: BLE001
                pass

    def _estimate_cost(self, agent_id: str, usage: dict) -> float:
        tokens = int((usage or {}).get("total_tokens") or 0)
        rate = self.cost_per_1k.get(agent_id) or self.cost_per_1k.get("default", 0.0005)
        return tokens / 1000 * rate


def _next_strategy(current: str) -> str:
    names = [s.split(":")[0].strip() for s in STRATEGIES]
    try:
        idx = names.index(current)
        return names[(idx + 1) % len(names)]
    except ValueError:
        return "alternative"


def _loop_conv_id() -> str:
    import uuid

    return f"loop-{uuid.uuid4().hex[:12]}"
