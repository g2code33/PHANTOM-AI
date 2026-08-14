"""The shared Agent core.

One engine runs BOTH entities (Phantom and Coded); everything that makes them
distinct — system prompt, model, API key, memory namespace, conversation
namespace, tool permissions — comes from the identity + configuration.

The loop:
  context package (system prompt + retrieved memories + windowed history)
   → provider stream (NVIDIA, SSE)
   → tool calls? → validate → permission gate → confirm? → execute (parallel
     where safe) → structured results back → repeat → final answer.

Robustness: tool errors never crash the agent (they come back as structured
data); provider errors are classified; retries happen at the provider layer;
network failure preserves conversation state in the DB; runs are cancellable
and stop when the kill switch engages.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from ..core.events import EventBus
from ..permissions.confirm import ConfirmationManager
from ..permissions.policy import PermissionManager
from ..providers.base import (
    ChatMessage,
    ProviderError,
    StreamDone,
    StreamError,
    TextChunk,
    ToolCall,
    ToolCallChunk,
)
from ..tools.base import ToolContext, ToolError, ToolRegistry, ToolResult
from .identities import generic_identity, identity

MAX_ITERATIONS = 12
DEFAULT_WINDOW = 24
MAX_TOOL_RESULT_TO_MODEL = 8000
TEXT_PROTOCOL_TOOLS_NOTE = """
TOOL SELECTION PROTOCOL (text mode):
This model build does not support native function calling, so you select tools by
emitting an XML block exactly like this when you want to call a tool:

<tool_call>{"name": "tool_name", "arguments": {"arg": "value"}}</tool_call>

Available tools:
{tools}

You may emit several <tool_call> blocks in one message (they run, and their results
are returned to you). When you have your final answer, just write it normally
without any <tool_call> block.
"""

TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)


@dataclass
class AgentRunResult:
    run_id: str = ""
    conversation_id: str = ""
    agent: str = ""
    content: str = ""
    tool_calls_made: int = 0
    status: str = "ok"          # ok | error | cancelled
    error: str = ""
    latency_ms: float = 0.0
    model: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    memory_package_chars: int = 0

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class CancellationRegistry:
    def __init__(self) -> None:
        self._events: dict[str, asyncio.Event] = {}

    def create(self, run_id: str) -> asyncio.Event:
        ev = asyncio.Event()
        self._events[run_id] = ev
        return ev

    def cancel(self, run_id: str) -> bool:
        ev = self._events.get(run_id)
        if ev is None:
            return False
        ev.set()
        return True

    def active(self) -> list[str]:
        return list(self._events)

    def clear(self, run_id: str) -> None:
        self._events.pop(run_id, None)


class Agent:
    def __init__(
        self,
        agent_id: str,
        provider: Any,
        registry: ToolRegistry,
        permission_manager: PermissionManager,
        confirmations: ConfirmationManager,
        conversation_store: Any,
        memory_store: Any,
        retriever: Any,
        audit: Any,
        events: EventBus,
        settings: Any,
        killswitch: Any,
        secrets: Any,
        notifications: Any,
        delegation_manager: Any = None,
    ) -> None:
        self.agent_id = agent_id
        if agent_id in ("phantom", "coded", "evolution", "health"):
            self.meta = identity(agent_id)
        else:
            self.meta = generic_identity(agent_id)
        self.provider = provider
        self.registry = registry
        self.permissions = permission_manager
        self.confirmations = confirmations
        self.conversations = conversation_store
        self.memories = memory_store
        self.retriever = retriever
        self.audit = audit
        self.events = events
        self.settings = settings
        self.killswitch = killswitch
        self.secrets = secrets
        self.notifications = notifications
        self.delegation_manager = delegation_manager
        self.cancellations = CancellationRegistry()
        self._run_semaphore: Optional[asyncio.Semaphore] = None
        self._active_runs: dict[str, dict[str, Any]] = {}
        # Evolution & System Intelligence hooks (optional; set by the container)
        self.graph: Any = None
        self.router: Any = None
        self.verifier: Any = None
        self.brain_registry: Any = None
        self.proposals: Any = None
        self.snapshots: Any = None
        self.loop_engine: Any = None
        self.analyst: Any = None
        self.tool_allowlist: Optional[set] = None

    async def ensure_model(self, task_text: str, mode: str = "chat") -> str:
        """Intelligent model selection: pick a model for this task via the
        router and rebuild the provider if the chosen model differs."""
        if self.router is None:
            return self.provider.model
        chosen = await self.router.choose(self.agent_id, task_text,
                                          has_tools=mode != "delegation")
        if chosen and chosen != self.provider.model:
            from ..providers import build_provider
            from ..config import KEY_ENV

            key = self.secrets.get(KEY_ENV.get(self.agent_id, "")) or \
                self.secrets.get(KEY_ENV.get("phantom", ""))
            provider = build_provider(self.agent_id, api_key=key or "", model=chosen)
            if provider.has_key:
                old = self.provider
                self.provider = provider
                close = getattr(old, "aclose", None)
                if close:
                    try:
                        await close()
                    except Exception:  # noqa: BLE001
                        pass
        return self.provider.model

    async def verify(self, objective: str, produced: str) -> dict[str, Any]:
        """Independent verification (critic-style) using this brain's provider."""
        if self.verifier is not None:
            return await self.verifier(objective, produced)
        try:
            result = await self.provider.complete(
                [ChatMessage(role="system", content=CRITIC_SYSTEM_PROMPT),
                 ChatMessage(role="user", content=f"OBJECTIVE:\n{objective}\n\n"
                                                  f"PRODUCED RESULT:\n{produced[:6000]}")],
                max_tokens=800,
            )
            return _parse_verdict(result.content)
        except Exception as exc:  # noqa: BLE001
            return {"verdict": "FAIL", "score": 0.0, "issues": [f"verification error: {exc}"],
                    "notes": ""}

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(
        self,
        conversation_id: str,
        user_text: str,
        session_id: str = "",
        mode: str = "chat",          # chat | delegation | proactive
        run_id: str | None = None,
        permissions_override: Optional[dict] = None,
    ) -> AgentRunResult:
        run_id = run_id or uuid.uuid4().hex
        started = time.monotonic()
        cancel_event = self.cancellations.create(run_id)

        async def kill_watcher():
            """Kill switch cancels in-flight runs immediately."""
            while True:
                if self.killswitch.is_engaged():
                    cancel_event.set()
                    return
                changed = self.killswitch.changed_event()
                await changed.wait()

        kill_task = asyncio.ensure_future(kill_watcher())
        try:
            sem = await self._semaphore()
            async with sem:
                return await self._run_locked(
                    conversation_id, user_text, session_id, mode, run_id,
                    cancel_event, permissions_override, started,
                )
        finally:
            kill_task.cancel()
            try:
                await kill_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self.cancellations.clear(run_id)

    async def _semaphore(self) -> asyncio.Semaphore:
        if self._run_semaphore is None:
            limit = int(await self.settings.get("agent.max_concurrent_runs", self.agent_id, 2))
            self._run_semaphore = asyncio.Semaphore(max(1, limit))
        return self._run_semaphore

    async def _run_locked(self, conversation_id, user_text, session_id, mode, run_id,
                          cancel_event, permissions_override, started) -> AgentRunResult:
        agent = self.agent_id
        if self.killswitch.is_engaged():
            return AgentRunResult(run_id=run_id, conversation_id=conversation_id, agent=agent,
                                  status="cancelled", error="kill switch is engaged",
                                  latency_ms=0)
        window = int(await self.settings.get("context.window", agent, DEFAULT_WINDOW))
        max_iterations = int(await self.settings.get("agent.max_iterations", agent, MAX_ITERATIONS))
        self._active_runs[run_id] = {"conversation_id": conversation_id, "mode": mode}

        # persist user message
        user_row = await self.conversations.append_message(conversation_id, agent, "user",
                                                           user_text, session_id=session_id)
        await self._auto_title(conversation_id, user_text)

        # graph auto-tracking (never breaks the run)
        if self.graph is not None:
            try:
                await self.graph.track("user", "user", {"origin": "local"}, agent=agent)
                await self.graph.track("conversation", conversation_id,
                                       {"agent": agent}, agent=agent)
                await self.graph.relate("user", conversation_id, "mentions",
                                        properties={"agent": agent})
                await self.graph.track("task", user_text[:120], {"agent": agent},
                                       agent=agent, relation="occurred_in",
                                       target=conversation_id)
            except Exception:  # noqa: BLE001
                pass

        # intelligent model selection for this task
        try:
            await self.ensure_model(user_text, mode)
        except Exception:  # noqa: BLE001 — model switching failure must not block
            pass
        await self.events.publish_run(run_id, "agent.run_started", {
            "agent": agent, "conversation_id": conversation_id, "mode": mode}, agent=agent)

        # ---- build context package -----------------------------------
        memory_package = ""
        try:
            memory_package = await self.retriever.build_context_package(agent, user_text,
                                                                        conversation_id)
        except Exception:  # noqa: BLE001 — memory failure must not break the run
            memory_package = ""

        system_prompt = self.meta["system_prompt"]
        if memory_package:
            system_prompt += "\n\nRETRIEVED CONTEXT FOR THIS CONVERSATION:\n" + memory_package

        history = await self._history_messages(conversation_id, window, user_row["id"])
        protocol: str = "native"
        if mode == "delegation":
            pass  # user message already carries the task text

        messages = [ChatMessage(role="system", content=system_prompt), *history]
        tools = self.registry.schemas(agent)
        if self.tool_allowlist:
            tools = [t for t in tools if t["function"]["name"] in self.tool_allowlist]
        if permissions_override:
            tools = [t for t in tools if t["function"]["name"] in permissions_override]
        tool_calls_made = 0
        content_out = ""
        final = None
        last_error: Optional[str] = None

        for iteration in range(max_iterations):
            if self.killswitch.is_engaged():
                final = AgentRunResult(run_id=run_id, conversation_id=conversation_id, agent=agent,
                                       status="cancelled", error="kill switch engaged while running",
                                       tool_calls_made=tool_calls_made, latency_ms=0)
                break
            if cancel_event.is_set():
                final = AgentRunResult(run_id=run_id, conversation_id=conversation_id, agent=agent,
                                       status="cancelled", error="cancelled by user",
                                       tool_calls_made=tool_calls_made, latency_ms=0)
                break

            stream_tools = tools if protocol == "native" else None
            if protocol == "text":
                stream_tools = None
                messages[0].content = (system_prompt
                                       + TEXT_PROTOCOL_TOOLS_NOTE.replace("{tools}",
                                                                          _text_tool_descriptions(tools)))

            try:
                done, tool_calls, provider_model, usage = await self._stream_once(
                    messages, stream_tools, run_id, agent, iteration, cancel_event)
            except ProviderError as exc:
                if exc.kind == "unsupported_tool_calling" and protocol == "native":
                    protocol = "text"
                    await self.audit.record(agent, "api.fallback_to_text_protocol",
                                            {"reason": exc.message[:200]})
                    continue
                if exc.kind == "cancelled":
                    final = AgentRunResult(run_id=run_id, conversation_id=conversation_id,
                                           agent=agent, status="cancelled",
                                           error="cancelled by user",
                                           tool_calls_made=tool_calls_made, latency_ms=0)
                    break
                last_error = self._provider_error_text(exc)
                await self.audit.record(agent, "api.error", {
                    "kind": exc.kind, "message": exc.message[:300]}, model=self.provider.model)
                final = AgentRunResult(run_id=run_id, conversation_id=conversation_id, agent=agent,
                                       status="error", error=last_error,
                                       tool_calls_made=tool_calls_made, latency_ms=0)
                break

            assistant_content = done.content if done else content_out
            if not tool_calls and protocol == "text":
                tool_calls = self._extract_text_tool_calls(assistant_content)

            # persist assistant message
            await self.conversations.append_message(
                conversation_id, agent, "assistant", assistant_content or None,
                tool_calls=[tc.__dict__ for tc in tool_calls] if tool_calls else None,
                session_id=session_id,
            )
            content_out = assistant_content

            if not tool_calls:
                final = AgentRunResult(
                    run_id=run_id, conversation_id=conversation_id, agent=agent,
                    content=assistant_content or "", tool_calls_made=tool_calls_made,
                    status="ok", latency_ms=(time.monotonic() - started) * 1000,
                    model=provider_model or self.provider.model, usage=usage,
                    memory_package_chars=len(memory_package))
                break

            # ---- execute tools ----
            results = await self._execute_tools(
                conversation_id, session_id, tool_calls, run_id, agent, mode,
                cancel_event, permissions_override, max_iterations - iteration)
            tool_calls_made += len(results)

            messages.append(ChatMessage(role="assistant", content=assistant_content,
                                        tool_calls=tool_calls if protocol == "native" else None))
            for tc, result in results:
                content = (result.output or "")[:MAX_TOOL_RESULT_TO_MODEL]
                if protocol == "native":
                    messages.append(ChatMessage(role="tool", content=content,
                                                tool_call_id=tc.id, name=tc.name))
                else:
                    messages.append(ChatMessage(
                        role="user",
                        content=f"[tool result for '{tc.name}' (call {tc.id})]\n"
                                f"<external_data>\n{content}\n</external_data>"))

        if final is None:
            final = AgentRunResult(run_id=run_id, conversation_id=conversation_id, agent=agent,
                                   content=content_out or "", tool_calls_made=tool_calls_made,
                                   status="ok",
                                   latency_ms=(time.monotonic() - started) * 1000,
                                   model=self.provider.model)
        elif final.latency_ms == 0:
            final.latency_ms = (time.monotonic() - started) * 1000

        # ---- roll summary when history grows past 2× window ------------
        if final.status == "ok":
            try:
                count = await self.conversations.count_messages(conversation_id)
                if count > window * 2:
                    await self._refresh_summary(conversation_id, window)
            except Exception:  # noqa: BLE001
                pass

        await self.audit.record(agent, "agent.run_completed", {
            "run_id": run_id, "status": final.status, "mode": mode,
            "tool_calls": tool_calls_made, "error": final.error,
            "memory_package_chars": len(memory_package)}, latency_ms=final.latency_ms,
            model=final.model, tokens=final.usage or None)
        await self.events.publish_run(run_id, "agent.run_completed", final.to_dict(), agent=agent)
        self._active_runs.pop(run_id, None)
        return final

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def _stream_once(self, messages, tools, run_id, agent, iteration, cancel_event):
        """Stream from the provider, racing against user cancellation so a slow
        network response can be interrupted immediately."""

        async def consume():
            content_parts: list[str] = []
            tool_calls: dict[int, dict[str, str]] = {}
            done: Optional[StreamDone] = None
            async for event in self.provider.stream(
                messages,
                tools=tools,
                temperature=float(await self.settings.get("model.temperature", agent, 0.4)),
                max_tokens=int(await self.settings.get("model.max_tokens", agent, 2048)),
            ):
                if cancel_event.is_set():
                    raise ProviderError("cancelled", "cancelled by user")
                if isinstance(event, TextChunk):
                    content_parts.append(event.text)
                    await self.events.publish_run(run_id, "agent.chunk",
                                                  {"text": event.text, "iteration": iteration},
                                                  agent=agent)
                elif isinstance(event, ToolCallChunk):
                    idx = len(tool_calls)
                    tool_calls[idx] = {"id": event.tool_call.id, "name": event.tool_call.name,
                                       "args": json.dumps(event.tool_call.arguments)}
                elif isinstance(event, StreamDone):
                    done = event
                elif isinstance(event, StreamError):
                    raise event.error
            calls = [ToolCall(id=v["id"], name=v["name"],
                              arguments=_safe_loads(v["args"])) for v in tool_calls.values()]
            return done, calls, (done.model if done else ""), (done.usage.to_dict() if done else {})

        consume_task = asyncio.ensure_future(consume())
        cancel_task = asyncio.ensure_future(cancel_event.wait())
        done_set, _ = await asyncio.wait({consume_task, cancel_task},
                                         return_when=asyncio.FIRST_COMPLETED)
        if cancel_task in done_set:
            consume_task.cancel()
            try:
                await consume_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            raise ProviderError("cancelled", "cancelled by user")
        try:
            return await consume_task
        except ProviderError:
            raise
        except asyncio.CancelledError:
            raise ProviderError("cancelled", "cancelled by user") from None

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    async def _execute_tools(self, conversation_id, session_id, tool_calls, run_id, agent,
                             mode, cancel_event, permissions_override, remaining_iterations):
        ctx = self._tool_context(conversation_id, session_id, run_id, cancel_event,
                                 permissions_override)
        confirm_timeout = float(await self.settings.get("confirmation.timeout", agent, 900))
        interactive = mode == "chat"

        # group: parallel-safe tools may run together; others run sequentially
        ordered: list[list[ToolCall]] = []
        skipped: list[tuple[ToolCall, ToolResult]] = []
        for tc in tool_calls:
            try:
                spec = self.registry.get(tc.name)
            except ToolError as exc:
                skipped.append((tc, ToolResult(
                    success=False,
                    output=f"{exc.to_agent_text()} (unknown tool — do not retry)")))
                continue
            if not ordered or not spec.parallel_safe:
                ordered.append([tc])
            else:
                ordered[-1].append(tc)

        executed: list[tuple[ToolCall, ToolResult]] = list(skipped)
        for group in ordered:
            group_results = await self._run_tool_group(
                ctx, group, agent, run_id, confirm_timeout, interactive,
                remaining_iterations)
            executed.extend(group_results)
            # persist tool result messages
            for tc, result in group_results:
                await self.conversations.append_message(
                    conversation_id, agent, "tool", result.output or "",
                    tool_results=[{"tool_call_id": tc.id, "name": tc.name,
                                   "success": result.success}],
                    session_id=session_id,
                )
                if self.graph is not None:
                    try:
                        await self.graph.track(
                            "tool", tc.name, {"success": result.success, "agent": agent},
                            agent=agent, relation="produces",
                            target=conversation_id,
                            weight=1.0 if result.success else 0.2)
                    except Exception:  # noqa: BLE001
                        pass
        return executed

    async def _run_tool_group(self, ctx, group, agent, run_id, confirm_timeout,
                              interactive, remaining_iterations):
        if len(group) == 1:
            tc = group[0]
            return [(tc, await self._run_single_tool(ctx, tc, agent, run_id,
                                                     confirm_timeout, interactive,
                                                     remaining_iterations))]

        max_parallel = int(await self.settings.get("tools.max_parallel", agent, 4))
        sem = asyncio.Semaphore(max_parallel)

        async def one(tc):
            async with sem:
                return await self._run_single_tool(ctx, tc, agent, run_id,
                                                   confirm_timeout, interactive,
                                                   remaining_iterations)

        results = await asyncio.gather(*(one(tc) for tc in group), return_exceptions=True)
        return [(tc, r if isinstance(r, ToolResult) else _error_result(r)) for tc, r in zip(group, results)]

    async def _run_single_tool(self, ctx, tc: ToolCall, agent, run_id, confirm_timeout,
                               interactive, remaining_iterations) -> ToolResult:
        started = time.monotonic()
        try:
            spec = self.registry.get(tc.name)
            spec.validate_arguments(tc.arguments)
            await self.events.publish_run(run_id, "tool.started", {
                "name": tc.name, "arguments": tc.arguments, "tool_call_id": tc.id}, agent=agent)
            decision = await self.permissions.authorize(
                agent, tc.name, tc.arguments, ctx.conversation_id, ctx.session_id,
                confirm_timeout=confirm_timeout, interactive=interactive)
            if decision.requires_confirmation:
                await self.events.publish_run(run_id, "tool.confirmed", {
                    "name": tc.name, "tool_call_id": tc.id, "decision": decision.to_dict()},
                    agent=agent)
            result = await spec.run(ctx, **tc.arguments)
            latency = (time.monotonic() - started) * 1000
            await self.audit.record(agent, "tool.executed", {
                "tool": tc.name, "arguments": tc.arguments, "success": result.success,
                "tool_call_id": tc.id, "decision_level": decision.level.value,
            }, latency_ms=latency)
            await self.events.publish_run(run_id, "tool.completed", {
                "name": tc.name, "tool_call_id": tc.id, "success": result.success,
                "output": (result.output or "")[:500], "latency_ms": round(latency, 1),
                "data": result.data, "artifacts": result.artifacts}, agent=agent)
            return result
        except ToolError as exc:
            latency = (time.monotonic() - started) * 1000
            await self.audit.record(agent, "tool.error", {
                "tool": tc.name, "arguments": tc.arguments, "kind": exc.kind,
                "message": exc.message, "tool_call_id": tc.id}, latency_ms=latency)
            await self.events.publish_run(run_id, "tool.error", {
                "name": tc.name, "kind": exc.kind, "message": exc.message,
                "tool_call_id": tc.id}, agent=agent)
            hint = ""
            if exc.kind == "permission":
                hint = " (the user or policy denied/blocked this — do not retry; explain or ask)"
            elif exc.kind == "invalid":
                hint = " (fix the arguments and retry once if you can)"
            elif exc.retryable and remaining_iterations > 1:
                hint = " (transient failure — you may retry once)"
            return ToolResult(success=False,
                              output=exc.to_agent_text() + hint,
                              data={"error_kind": exc.kind, "error": exc.message})
        except asyncio.CancelledError:
            raise ToolError("cancelled", kind="cancelled") from None
        except Exception as exc:  # noqa: BLE001
            await self.audit.record(agent, "tool.error", {
                "tool": tc.name, "kind": "internal", "message": str(exc)[:500],
                "tool_call_id": tc.id})
            return ToolResult(success=False, output=f"[tool internal error] {exc}")

    def _tool_context(self, conversation_id, session_id, run_id, cancel_event,
                      permissions_override) -> ToolContext:
        import os

        return ToolContext(
            agent_id=self.agent_id,
            conversation_id=conversation_id,
            session_id=session_id,
            data_dir=str(self.secrets.path.parent),
            workspace_root=os.path.expanduser("~"),
            db=self.conversations.db,
            settings=self.settings,
            permission_manager=self.permissions,
            audit=self.audit,
            events=self.events,
            memory_store=self.memories,
            conversation_store=self.conversations,
            delegation_manager=self.delegation_manager,
            secrets=self.secrets,
            confirmations=self.confirmations,
            notifications=self.notifications,
            task_token=cancel_event,
            brain_registry=self.brain_registry,
            graph=self.graph,
            proposals=self.proposals,
            snapshots=self.snapshots,
            loop_engine=self.loop_engine,
            verifier=self.verifier,
            analyst=self.analyst,
            agent_ids=tuple(self.brain_registry.ids_sync()) if self.brain_registry
            else ("phantom", "coded"),
        )

    # ------------------------------------------------------------------
    # History & summaries
    # ------------------------------------------------------------------

    async def _history_messages(self, conversation_id: str, window: int,
                                skip_uid: str = "") -> list[ChatMessage]:
        msgs = await self.conversations.get_messages(conversation_id, limit=100000)
        out: list[ChatMessage] = []
        for m in msgs:
            if m["id"] == skip_uid:
                continue  # the current user turn is not history yet
            role = m["role"]
            if role == "tool":
                out.append(ChatMessage(role="tool", content=m["content"] or "",
                                       tool_call_id=_first_tool_call_id(m["tool_results"])))
                continue
            tcs = _parse_tool_calls(m["tool_calls"])
            out.append(ChatMessage(role=role, content=m["content"],
                                   tool_calls=tcs if tcs else None))
        # window: keep last `window` messages (plus system built elsewhere)
        if len(out) > window:
            out = out[-window:]
        return out

    async def _refresh_summary(self, conversation_id: str, window: int) -> None:
        msgs = await self.conversations.get_messages(conversation_id, limit=100000)
        old = msgs[:-window]
        text = "\n".join(
            f"{m['role']}: {m['content'][:400] if m['content'] else ''}" for m in old
        )[:12000]
        if not text:
            return
        try:
            summary = await self.provider.complete(
                [ChatMessage(role="system", content="You summarize conversations concisely (max 200 words), "
                                                    "keeping key facts, decisions, preferences and open questions."),
                 ChatMessage(role="user", content=f"Summarize this conversation excerpt:\n\n{text}")],
                max_tokens=400,
            )
            if summary.content:
                await self.conversations.set_summary(conversation_id, summary.content[:4000])
        except Exception:  # noqa: BLE001
            # heuristic fallback — keep the first user line so context is not lost
            first = next((m["content"] for m in msgs if m["role"] == "user" and m["content"]), "")
            if first:
                await self.conversations.set_summary(
                    conversation_id, f"Conversation began: {first[:300]}")

    async def _auto_title(self, conversation_id: str, user_text: str) -> None:
        conv = await self.conversations.get(conversation_id)
        if conv and conv.get("title", "").startswith("New conversation"):
            title = " ".join(user_text.split())[:70]
            await self.conversations.set_title(conversation_id, title or "Untitled")

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_text_tool_calls(content: str) -> list[ToolCall]:
        calls = []
        for match in TOOL_CALL_RE.finditer(content or ""):
            try:
                obj = json.loads(match.group(1))
                name = str(obj.get("name", ""))
                if not name:
                    continue
                args = obj.get("arguments") or {}
                if not isinstance(args, dict):
                    args = {}
                calls.append(ToolCall(id=f"text_{len(calls)}", name=name, arguments=args))
            except (json.JSONDecodeError, AttributeError):
                continue
        return calls

    @staticmethod
    def _provider_error_text(exc: ProviderError) -> str:
        if exc.kind == "auth":
            return ("⚠️ NVIDIA rejected the API key. Check Settings → API Keys for "
                    + ("Phantom" if "Phantom" in str(exc) else "this agent") + ".")
        if exc.kind == "rate_limit":
            return "⚠️ NVIDIA rate limit reached. Waiting a moment and retrying is possible — or reduce usage."
        if exc.kind == "network":
            return "⚠️ Cannot reach NVIDIA (network problem). Your conversation was saved locally."
        if exc.kind == "timeout":
            return "⚠️ NVIDIA request timed out. Try again."
        return f"⚠️ Model provider error ({exc.kind}): {exc.message}"

    def cancel(self, run_id: str) -> bool:
        return self.cancellations.cancel(run_id)

    def active_runs(self) -> list[str]:
        return list(self._active_runs)


def _error_result(exc: Exception) -> ToolResult:
    if isinstance(exc, ToolError):
        return ToolResult(success=False, output=exc.to_agent_text())
    return ToolResult(success=False, output=f"[tool internal error] {exc}")


def _safe_loads(raw: str) -> dict[str, Any]:
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}


def _parse_tool_calls(raw: Optional[str]) -> Optional[list[ToolCall]]:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return [ToolCall(id=t.get("id", ""), name=t.get("name", ""),
                     arguments=t.get("arguments") or {}) for t in data if t.get("name")]


def _first_tool_call_id(raw: Optional[str]) -> str:
    if not raw:
        return ""
    try:
        data = json.loads(raw)
        return data[0].get("tool_call_id", "") if data else ""
    except (json.JSONDecodeError, TypeError, IndexError, AttributeError):
        return ""


def _text_tool_descriptions(tools: list[dict]) -> str:
    lines = []
    for t in tools:
        fn = t["function"]
        lines.append(f"- {fn['name']}: {fn['description']}")
    return "\n".join(lines)


CRITIC_SYSTEM_PROMPT = """You are an independent CRITIC/VERIFICATION system.

You review work produced by another system against its original objective.
You do not trust the producer; you evaluate evidence.

Respond with a JSON object exactly like this:
{"verdict": "PASS" | "FAIL", "score": 0.0-1.0, "issues": ["..."], "notes": "..."}

- PASS only when the objective is actually satisfied by the produced result.
- List concrete issues when failing.
- Be strict but fair; short outputs that fully satisfy the objective can PASS.
"""


def _parse_verdict(content: str) -> dict[str, Any]:
    import json as _json

    try:
        match = re.search(r"\{.*\}", content or "", re.S)
        if not match:
            raise ValueError("no json")
        data = _json.loads(match.group(0))
        return {
            "verdict": str(data.get("verdict", "FAIL")).upper(),
            "score": float(data.get("score", 0.0)),
            "issues": list(data.get("issues", []) or []),
            "notes": str(data.get("notes", "") or ""),
        }
    except Exception:  # noqa: BLE001
        upper = (content or "").upper()
        return {"verdict": "PASS" if "VERDICT: PASS" in upper or '"PASS"' in upper else "FAIL",
                "score": 1.0 if "PASS" in upper else 0.0,
                "issues": ["unparseable verification output"] if "PASS" not in upper else [],
                "notes": (content or "")[:500]}
