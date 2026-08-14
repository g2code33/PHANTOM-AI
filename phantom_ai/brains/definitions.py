"""Brain Definition — the standardized format for describing any intelligence
in the system. New brains can be added dynamically without rebuilding the app."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ..config import DEFAULT_MODELS


@dataclass
class BrainDefinition:
    brain_id: str
    name: str = ""
    role: str = "general"
    description: str = ""
    system_prompt: str = ""
    model: str = ""
    provider: str = "nvidia"
    key_env: str = ""
    tools: Optional[list[str]] = None          # None = all default tools
    memory_scope: str = "own"                  # own | shared | global
    permissions: dict[str, Any] = field(default_factory=dict)
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    routing_rules: str = ""
    dependencies: list[str] = field(default_factory=list)
    verification: str = ""                     # verification requirements
    version: int = 1
    status: str = "active"

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.brain_id or not self.brain_id.replace("_", "").isalnum():
            errors.append("brain_id must be alphanumeric (underscores allowed)")
        if not self.system_prompt.strip():
            errors.append("system_prompt is required")
        return errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.brain_id,
            "name": self.name or self.brain_id.title(),
            "role": self.role,
            "description": self.description,
            "system_prompt": self.system_prompt,
            "model": self.model or DEFAULT_MODELS.get("phantom", ""),
            "provider": self.provider,
            "key_env": self.key_env,
            "tools": self.tools,
            "memory_scope": self.memory_scope,
            "permissions": self.permissions,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "routing_rules": self.routing_rules,
            "dependencies": self.dependencies,
            "verification": self.verification,
            "version": self.version,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BrainDefinition":
        return cls(
            brain_id=d.get("id") or d.get("brain_id", ""),
            name=d.get("name", ""),
            role=d.get("role", "general"),
            description=d.get("description", ""),
            system_prompt=d.get("system_prompt", ""),
            model=d.get("model", ""),
            provider=d.get("provider", "nvidia"),
            key_env=d.get("key_env", ""),
            tools=d.get("tools"),
            memory_scope=d.get("memory_scope", "own"),
            permissions=d.get("permissions", {}),
            input_schema=d.get("input_schema", {}),
            output_schema=d.get("output_schema", {}),
            routing_rules=d.get("routing_rules", ""),
            dependencies=d.get("dependencies", []),
            verification=d.get("verification", ""),
            version=int(d.get("version", 1)),
            status=d.get("status", "active"),
        )

    @classmethod
    def from_identity(cls, agent_id: str, system_prompt: str) -> "BrainDefinition":
        """Build a brain definition from a built-in agent identity."""
        from ..agents.identities import identity  # local import avoids cycles

        meta = identity(agent_id)
        return cls(
            brain_id=agent_id,
            name=meta["display_name"],
            role="general" if agent_id == "phantom" else "technical",
            description=meta["tagline"],
            system_prompt=system_prompt,
            model=meta["default_model"],
            key_env=meta["key_env"],
            memory_scope=agent_id,
            tools=None,
            dependencies=[],
            verification="",
        )
