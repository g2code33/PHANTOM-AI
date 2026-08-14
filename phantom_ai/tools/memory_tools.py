"""Memory tools: durable facts, preferences, projects, conversation search.

These give the agents agency over their own long-term memory. Memory is DATA —
stored facts are wrapped and presented as data, never as instructions.
"""

from __future__ import annotations

from ..config import SecretRedactor
from .base import PermissionLevel, ToolContext, ToolError, ToolResult, ToolSpec


async def _remember(ctx: ToolContext, content: str, kind: str = "fact",
                    importance: float = 0.5, tags: list[str] | None = None) -> ToolResult:
    content = SecretRedactor.redact(content.strip())[:4000]
    if not content:
        raise ToolError("memory content is empty", kind="invalid")
    memory = await ctx.memory_store.add(
        agent=ctx.agent_id, content=content, kind=kind, importance=importance,
        tags=tags, source_conversation=ctx.conversation_id or None,
    )
    return ToolResult.ok(
        f"Stored memory [{kind}] (importance {importance:.1f}): {content[:200]}",
        data={"memory_id": memory["id"]},
    )


async def _remember_shared(ctx: ToolContext, content: str, kind: str = "fact",
                           importance: float = 0.5) -> ToolResult:
    content = SecretRedactor.redact(content.strip())[:4000]
    memory = await ctx.memory_store.add(
        agent="shared", content=content, kind=kind, importance=importance,
        source_conversation=ctx.conversation_id or None,
    )
    return ToolResult.ok(
        f"Stored SHARED memory (visible to both Phantom and Coded): {content[:200]}",
        data={"memory_id": memory["id"], "shared": True},
    )


async def _search_memories(ctx: ToolContext, query: str, limit: int = 10) -> ToolResult:
    rows = await ctx.memory_store.search(ctx.agent_id, query, limit=min(limit, 25))
    if not rows:
        return ToolResult.ok(f"No memories matching '{query}'", data={"memories": []})
    lines = []
    for m in rows:
        scope = "SHARED" if m["agent"] == "shared" else m["agent"].upper()
        lines.append(f"[{m['kind']} · {scope} · imp {m['importance']:.1f}] {m['content']}")
        await ctx.memory_store.touch(m["id"])
    return ToolResult.ok("\n".join(lines), data={"memories": rows})


async def _list_memories(ctx: ToolContext, kind: str = "", limit: int = 30) -> ToolResult:
    rows = await ctx.memory_store.list(agent=ctx.agent_id, kind=kind or None, limit=min(limit, 100))
    if not rows:
        return ToolResult.ok("No memories stored yet.", data={"memories": []})
    lines = []
    for m in rows:
        scope = "SHARED" if m["agent"] == "shared" else m["agent"].upper()
        lines.append(f"{m['id'][:8]} [{m['kind']} · {scope} · imp {m['importance']:.1f}] {m['content'][:160]}")
    return ToolResult.ok("\n".join(lines), data={"memories": rows})


async def _forget_memory(ctx: ToolContext, memory_id: str = "", content: str = "") -> ToolResult:
    if memory_id:
        memory = await ctx.memory_store.get(memory_id)
        if not memory:
            raise ToolError(f"no memory with id {memory_id}", kind="not_found")
        await ctx.memory_store.deactivate(memory_id)
        return ToolResult.ok(f"Forgotten memory {memory_id[:8]}: {memory['content'][:120]}",
                             data={"memory_id": memory_id})
    if content:
        rows = await ctx.memory_store.search(ctx.agent_id, content, limit=5)
        if not rows:
            raise ToolError("no matching memory found to forget", kind="not_found")
        for r in rows:
            await ctx.memory_store.deactivate(r["id"])
        return ToolResult.ok(f"Forgotten {len(rows)} matching memor{'y' if len(rows) == 1 else 'ies'}",
                             data={"memory_ids": [r["id"] for r in rows]})
    raise ToolError("provide memory_id or content to forget", kind="invalid")


async def _search_conversations(ctx: ToolContext, query: str, limit: int = 6) -> ToolResult:
    rows = await ctx.conversation_store.search(ctx.agent_id, query, limit=min(limit, 15))
    if not rows:
        return ToolResult.ok(f"No past conversations match '{query}'", data={"results": []})
    lines = []
    for r in rows:
        date = (r.get("created_at") or "")[:16].replace("T", " ")
        lines.append(f"• {r['conversation_title']} ({date})\n  {r['snippet']}")
    return ToolResult.ok("\n\n".join(lines), data={"results": rows})


def register_memory_tools(registry) -> None:
    registry.register(ToolSpec(
        name="remember",
        description="Store a durable fact/preference/project note in this agent's long-term memory.",
        purpose="Remember things", category="memory",
        parameters={"content": {"type": "string", "required": True},
                    "kind": {"type": "string", "enum": ["fact", "preference", "project", "decision", "technical"], "default": "fact"},
                    "importance": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.5},
                    "tags": {"type": "array", "items": {"type": "string"}}},
        handler=_remember, permission=PermissionLevel.SAFE_ACTION, timeout=10,
    ))
    registry.register(ToolSpec(
        name="remember_shared",
        description="Store a fact into SHARED memory, visible to both Phantom and Coded.",
        purpose="Share knowledge between agents", category="memory",
        parameters={"content": {"type": "string", "required": True},
                    "kind": {"type": "string", "enum": ["fact", "project", "decision"], "default": "fact"},
                    "importance": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.5}},
        handler=_remember_shared, permission=PermissionLevel.SAFE_ACTION, timeout=10,
    ))
    registry.register(ToolSpec(
        name="search_memories",
        description="Search this agent's long-term memory (own + shared facts).",
        purpose="Recall facts", category="memory",
        parameters={"query": {"type": "string", "required": True},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 25, "default": 10}},
        handler=_search_memories, permission=PermissionLevel.READ_ONLY, timeout=10,
    ))
    registry.register(ToolSpec(
        name="list_memories",
        description="List stored memories for this agent.",
        purpose="Inspect memory", category="memory",
        parameters={"kind": {"type": "string", "enum": ["fact", "preference", "project", "decision", "technical", ""], "default": ""},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 30}},
        handler=_list_memories, permission=PermissionLevel.READ_ONLY, timeout=10,
    ))
    registry.register(ToolSpec(
        name="forget_memory",
        description="Remove a memory by id or by matching content.",
        purpose="Delete memories", category="memory",
        parameters={"memory_id": {"type": "string", "default": ""},
                    "content": {"type": "string", "default": ""}},
        handler=_forget_memory, permission=PermissionLevel.CONFIRM_REQUIRED, timeout=10,
        required_permission_notes="Deleting a memory is permanent.",
    ))
    registry.register(ToolSpec(
        name="search_conversations",
        description="Search this agent's archived conversation history (full-text) and return relevant snippets.",
        purpose="Retrieve past discussions", category="memory",
        parameters={"query": {"type": "string", "required": True},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 15, "default": 6}},
        handler=_search_conversations, permission=PermissionLevel.READ_ONLY, timeout=15,
    ))
