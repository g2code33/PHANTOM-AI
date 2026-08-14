"""Tool registry core: specs, results, errors, argument validation.

Every capability is a ToolSpec with a typed input schema, a default
permission level, a timeout, an async handler and audit hooks. Adding a
capability = registering a tool, never rewriting the agent core.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from jsonschema import Draft202012Validator, ValidationError

# Re-exported for backward compatibility; canonical definition lives in
# permissions.levels so the permissions layer can import it without cycles.
from ..permissions.levels import PermissionLevel


class ToolError(Exception):
    """Structured tool failure surfaced to the agent."""

    def __init__(self, message: str, kind: str = "error", retryable: bool = False,
                 data: dict[str, Any] | None = None):
        super().__init__(message)
        self.kind = kind          # error | not_found | permission | timeout | unavailable | invalid
        self.retryable = retryable
        self.data = data or {}

    @property
    def message(self) -> str:
        return str(self.args[0]) if self.args else ""

    def to_agent_text(self) -> str:
        return f"[tool error {self.kind}] {self.message}"


@dataclass
class ToolResult:
    """What a tool returns. `output` goes to the model; `data` is structured
    and shown in the UI; `artifacts` may reference files/URLs for the UI."""

    success: bool = True
    output: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def ok(cls, output: str, data: dict[str, Any] | None = None,
           artifacts: list[dict[str, Any]] | None = None) -> "ToolResult":
        return cls(success=True, output=output, data=data or {}, artifacts=artifacts or [])

    @classmethod
    def fail(cls, error: str, data: dict[str, Any] | None = None) -> "ToolResult":
        return cls(success=False, output=f"[tool error] {error}", data=data or {})


@dataclass
class ToolContext:
    """Everything a tool may need: identity, config, workspace, services."""

    agent_id: str
    conversation_id: str = ""
    session_id: str = ""
    data_dir: str = ""
    workspace_root: str = ""
    db: Any = None
    registry: Any = None
    settings: Any = None
    permission_manager: Any = None
    audit: Any = None
    events: Any = None
    memory_store: Any = None
    conversation_store: Any = None
    delegation_manager: Any = None
    secrets: Any = None
    confirmations: Any = None
    notifications: Any = None
    task_token: Any = None  # asyncio.Event — cancelled when the run is stopped
    # --- Evolution & System Intelligence services ---
    brain_registry: Any = None
    graph: Any = None
    proposals: Any = None
    snapshots: Any = None
    loop_engine: Any = None
    verifier: Any = None
    analyst: Any = None
    agent_ids: tuple = ("phantom", "coded")


async def _run_with_timeout(coro: Awaitable, timeout: float, ctx: ToolContext) -> Any:
    """Run a tool handler with a deadline; honours run cancellation."""
    main_task = asyncio.ensure_future(coro)
    if timeout and timeout > 0:
        try:
            return await asyncio.wait_for(asyncio.shield(main_task), timeout=timeout)
        except asyncio.TimeoutError:
            main_task.cancel()
            raise ToolError(f"tool timed out after {timeout:g}s", kind="timeout", retryable=True) from None
    return await main_task


class ToolSpec:
    def __init__(
        self,
        name: str,
        description: str,
        purpose: str,
        parameters: dict[str, Any],
        handler: Callable[..., Awaitable[ToolResult]],
        *,
        permission: PermissionLevel = PermissionLevel.SAFE_ACTION,
        timeout: float = 30.0,
        category: str = "general",
        agents: tuple[str, ...] = ("phantom", "coded"),
        parallel_safe: bool = True,
        required_permission_notes: str = "",
    ) -> None:
        self.name = name
        self.description = description
        self.purpose = purpose
        self.parameters = parameters
        self.handler = handler
        self.permission = permission
        self.timeout = timeout
        self.category = category
        self.agents = agents
        self.parallel_safe = parallel_safe
        self.required_permission_notes = required_permission_notes
        self._validator = Draft202012Validator({"type": "object", "properties": parameters,
                                                "required": [k for k, v in parameters.items()
                                                             if v.get("required")]})

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.parameters,
                    "required": [k for k, v in self.parameters.items() if v.get("required")],
                },
            },
        }

    def validate_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        errors = sorted(self._validator.iter_errors(arguments), key=lambda e: list(e.path))
        if errors:
            raise ToolError(
                "invalid arguments: " + "; ".join(_describe_error(e) for e in errors[:5]),
                kind="invalid",
            )
        return arguments

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        self.validate_arguments(kwargs)
        sig = inspect.signature(self.handler)
        params = {}
        for name, p in sig.parameters.items():
            if name == "ctx":
                params[name] = ctx
            elif name in kwargs:
                params[name] = kwargs[name]
            elif p.default is inspect.Parameter.empty:
                raise ToolError(f"missing required argument: {name}", kind="invalid")
        try:
            return await _run_with_timeout(self.handler(**params), self.timeout, ctx)
        except ToolError:
            raise
        except asyncio.CancelledError:
            raise ToolError("tool execution cancelled", kind="cancelled") from None
        except Exception as exc:  # noqa: BLE001
            raise ToolError(f"{type(exc).__name__}: {exc}") from None


def _describe_error(e: ValidationError) -> str:
    path = "/".join(str(p) for p in e.path) or "(root)"
    return f"{path}: {e.message}"


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"duplicate tool registration: {spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError:
            raise ToolError(f"unknown tool: {name}", kind="not_found") from None

    def has(self, name: str) -> bool:
        return name in self._tools

    def list(self) -> list[ToolSpec]:
        return sorted(self._tools.values(), key=lambda t: t.name)

    def schemas(self, agent_id: str) -> list[dict[str, Any]]:
        return [t.schema() for t in self._tools.values() if agent_id in t.agents]

    def names(self) -> list[str]:
        return sorted(self._tools)

    def summary(self, agent_id: str) -> list[dict[str, Any]]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "purpose": t.purpose,
                "category": t.category,
                "permission": t.permission.value,
                "timeout": t.timeout,
            }
            for t in self._tools.values()
            if agent_id in t.agents
        ]


# ---------------------------------------------------------------------------
# Path safety helpers (used by many tools)
# ---------------------------------------------------------------------------

DEFAULT_ALLOWED_ROOTS = (".", "~")  # workspace root + home


def resolve_safe_path(path: str, extra_roots: list[str] | None = None,
                      must_exist: bool = False, allow_write: bool = False) -> str:
    """Resolve `path` and ensure it stays inside allowed roots (anti-traversal).

    Allowed roots: the app workspace root and the user's home directory
    (plus any extra roots passed, e.g. a project dir). Absolute paths outside
    these roots are rejected.
    """
    import os

    if not path or not isinstance(path, str):
        raise ToolError("path must be a non-empty string", kind="invalid")
    home = os.path.expanduser("~")
    expanded = os.path.expanduser(path)
    candidate = os.path.abspath(expanded)

    roots = [os.path.abspath(os.path.expanduser(r))
             for r in (DEFAULT_ALLOWED_ROOTS + tuple(extra_roots or []))]
    resolved_roots = []
    for r in roots:
        rr = os.path.realpath(r)
        resolved_roots.append(rr)
        resolved_roots.append(rr + os.sep)

    if candidate.startswith("/proc") or candidate.startswith("/sys") or candidate.startswith("/dev"):
        raise ToolError(f"access to {candidate} is not permitted", kind="permission")

    real = os.path.realpath(candidate)
    inside = any(real == r.rstrip(os.sep) or real.startswith(r) for r in resolved_roots)
    if not inside:
        raise ToolError(
            f"path outside allowed roots ({home} and workspace): {path}",
            kind="permission",
        )
    if must_exist and not os.path.exists(real):
        raise ToolError(f"path does not exist: {path}", kind="not_found")
    if allow_write and os.path.exists(real) and not os.path.isfile(real) and not os.path.isdir(real):
        raise ToolError(f"refusing to touch special file: {path}", kind="permission")
    return real
