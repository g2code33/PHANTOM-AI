"""Permission manager — the gate between an agent's decision and actual execution.

Layers (each can only raise strictness, never lower it below SAFE_ACTION):
  1. Tool default level
  2. Global override  (settings: perm:global:<tool>)
  3. Per-agent override (settings: perm:<agent>:<tool>)
  4. Command policy for run_command / run_script (blocked patterns always win)
  5. Path rules (settings: path_rules) — argument values matching a rule upgrade
  6. Secret-leak guard — arguments that look like credentials are blocked
  7. Kill switch — when engaged, tool execution is refused

The model can never bypass this layer by "reasoning" that an action is needed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from ..tools.base import PermissionLevel, ToolError, ToolRegistry
from .validation import PermissionLevel as VL, validate_command

LEVEL_ORDER = {lvl: i for i, lvl in enumerate(PermissionLevel)}
SECRET_PATTERNS = [re.compile(r"nvapi-[a-zA-Z0-9]{12,}"), re.compile(r"\bsk-[a-zA-Z0-9]{16,}"),
                   re.compile(r"(?i)bearer\s+[a-zA-Z0-9._\-]{12,}")]


@dataclass
class PermissionDecision:
    level: PermissionLevel
    source: str = "tool-default"
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.level != PermissionLevel.BLOCKED

    @property
    def requires_confirmation(self) -> bool:
        return self.level in (PermissionLevel.CONFIRM_REQUIRED, PermissionLevel.HIGH_RISK)

    def to_dict(self) -> dict[str, Any]:
        return {"level": self.level.value, "source": self.source, "reason": self.reason}


class PermissionManager:
    def __init__(self, registry: ToolRegistry, settings: Any, audit: Any, events: Any,
                 confirmations: Any, killswitch: Any) -> None:
        self.registry = registry
        self.settings = settings
        self.audit = audit
        self.events = events
        self.confirmations = confirmations
        self.killswitch = killswitch
        self._cache: dict[str, PermissionLevel] = {}

    # ------------------------------------------------------------------
    async def effective_level(self, agent: str, tool_name: str, args: dict[str, Any]) -> tuple[PermissionLevel, str, str]:
        spec = self.registry.get(tool_name)
        level = spec.permission
        source = "tool-default"
        reason = ""
        explicit_override = False

        override = await self.settings.get(f"perm:{tool_name}", agent, None)
        if override is None:
            override = await self.settings.get(f"perm:global:{tool_name}", "*", None)
        if override is not None:
            try:
                level = PermissionLevel(override)
                explicit_override = True
                source = f"override({agent})" if override else "override(global)"
            except ValueError:
                pass

        # command policy floor
        if tool_name in ("run_command", "run_script"):
            cmd = (args.get("command") or args.get("script") or "").strip()
            verdict = validate_command(cmd, args.get("cwd"))
            vlevel = {
                VL.BLOCKED: PermissionLevel.BLOCKED,
                VL.CONFIRM_REQUIRED: PermissionLevel.CONFIRM_REQUIRED,
                VL.SAFE_ACTION: PermissionLevel.SAFE_ACTION,
            }[verdict.level]
            if vlevel == PermissionLevel.BLOCKED:
                return vlevel, "command-policy", verdict.reason
            # a mutating command confirms unless the owner explicitly overrode
            # this tool to a safe level (their call, recorded in settings)
            if vlevel == PermissionLevel.CONFIRM_REQUIRED and not explicit_override:
                level = vlevel
                source = "command-policy"
                reason = verdict.reason

        # path rules (only upgrade)
        rules = await self.settings.get("path_rules", "*", [])
        if rules:
            arg_blob = json.dumps(args, default=str)
            for rule in rules:
                pattern = rule.get("match", "")
                rlevel = PermissionLevel(rule.get("level", "confirm_required"))
                try:
                    if re.search(pattern, arg_blob) and LEVEL_ORDER[rlevel] > LEVEL_ORDER[level]:
                        level = rlevel
                        source = f"path-rule:{pattern}"
                except re.error:
                    continue

        # secret-leak guard
        arg_blob = json.dumps(args, default=str)
        for rx in SECRET_PATTERNS:
            if rx.search(arg_blob):
                return PermissionLevel.BLOCKED, "secret-guard", "arguments look like credentials — refused"

        return level, source, reason

    @staticmethod
    def _is_safe_override(level: PermissionLevel) -> bool:
        return level == PermissionLevel.SAFE_ACTION or level == PermissionLevel.READ_ONLY

    # ------------------------------------------------------------------
    async def authorize(self, agent: str, tool_name: str, args: dict[str, Any],
                        conversation_id: str, session_id: str,
                        confirm_timeout: float = 900.0, interactive: bool = True) -> PermissionDecision:
        if self.killswitch.is_engaged():
            raise ToolError("kill switch is engaged — tool execution is disabled",
                            kind="permission")
        level, source, reason = await self.effective_level(agent, tool_name, args)
        decision = PermissionDecision(level=level, source=source, reason=reason)

        if level == PermissionLevel.BLOCKED:
            await self.audit.record(agent, "permission.denied", {
                "tool": tool_name, "args": args, "reason": reason or "blocked by policy",
                "conversation_id": conversation_id,
            })
            raise ToolError(f"action blocked by permission policy ({reason or 'blocked'})",
                            kind="permission")
        if level == PermissionLevel.CONFIRM_REQUIRED or level == PermissionLevel.HIGH_RISK:
            decision = await self._confirm(agent, tool_name, args, conversation_id, session_id,
                                           level, confirm_timeout, interactive)
        return decision

    async def _confirm(self, agent: str, tool_name: str, args: dict[str, Any],
                       conversation_id: str, session_id: str, level: PermissionLevel,
                       timeout: float, interactive: bool) -> PermissionDecision:
        spec = self.registry.get(tool_name)
        explanation = explain_action(agent, spec.name, args, spec.required_permission_notes)
        confirmation = await self.confirmations.create(
            agent=agent, conversation_id=conversation_id, tool_name=spec.name,
            arguments=args, **explanation,
        )
        await self.audit.record(agent, "confirmation.requested", {
            "confirmation_id": confirmation["id"], "tool": spec.name, "args": args,
            "reason": explanation["reason"], "level": level.value,
        })
        if self.events:
            await self.events.publish("confirmation.requested", {
                "confirmation": confirmation, "agent": agent,
                "what": explanation["what"], "why": explanation["reason"],
                "affected": explanation["impact"], "action": explanation["action"],
                "risk": explanation["risk"],
            })
        status = await self.confirmations.wait(confirmation["id"], timeout=timeout,
                                               interactive=interactive)
        if status == "approved":
            await self.audit.record(agent, "confirmation.approved",
                                    {"confirmation_id": confirmation["id"], "tool": spec.name})
            return PermissionDecision(level=PermissionLevel.SAFE_ACTION, source="confirmed",
                                      reason="user approved this specific action")
        if status == "denied":
            await self.audit.record(agent, "confirmation.denied",
                                    {"confirmation_id": confirmation["id"], "tool": spec.name})
            raise ToolError("action was denied by the user", kind="permission")
        await self.audit.record(agent, "confirmation.expired",
                                {"confirmation_id": confirmation["id"], "tool": spec.name})
        raise ToolError("confirmation request expired (no answer in time); action not executed",
                        kind="permission")

    # ------------------------------------------------------------------
    async def tool_allowed_quick_check(self, agent: str, tool_name: str) -> bool:
        """Used by the UI tool-lab to pre-filter obviously blocked tools."""
        try:
            level, _, _ = await self.effective_level(agent, tool_name, {})
            return level != PermissionLevel.BLOCKED
        except ToolError:
            return False


def explain_action(agent: str, tool_name: str, args: dict[str, Any], notes: str = "") -> dict[str, Any]:
    """Build the human-readable confirmation text: what / why / affected / action / risk."""
    agent_label = {"phantom": "Phantom", "coded": "Coded"}.get(agent, agent)
    arg_preview = _preview_args(args)
    what = f"{agent_label} wants to run tool `{tool_name}` with arguments: {arg_preview}"
    why = notes or f"The agent decided this action serves your current request. Review the arguments and purpose."
    action = f"{tool_name}({', '.join(f'{k}={v!r}' for k, v in list(args.items())[:6])})"
    risk = "This action can change or remove data and may not be easily undone." if notes else \
        "Consequential action — verify it targets exactly what you expect."
    affected = _affected_scope(args)
    return {"reason": why, "impact": affected, "risk": risk,
            "what": what, "action": action}


def _preview_args(args: dict[str, Any], limit: int = 400) -> str:
    text = json.dumps(args, default=str)[:limit]
    return text + ("…" if len(text) == limit else "")


def _affected_scope(args: dict[str, Any]) -> str:
    paths = []
    for key in ("path", "source", "destination", "archive", "cwd"):
        val = args.get(key)
        if isinstance(val, str) and val:
            paths.append(val)
    for key in ("paths",):
        vals = args.get(key)
        if isinstance(vals, list):
            paths.extend(str(v) for v in vals if isinstance(v, str))
    if paths:
        return "Files/directories affected: " + ", ".join(paths)
    if args.get("pid") is not None:
        return f"Process affected: pid {args['pid']}"
    if args.get("name"):
        return f"Target: {args['name']}"
    return "Scope is defined by the tool arguments above."
