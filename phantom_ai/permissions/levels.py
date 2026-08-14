"""Permission levels — defined here so both tools and permissions can import
them without circular dependencies."""

from __future__ import annotations

from enum import Enum


class PermissionLevel(str, Enum):
    READ_ONLY = "read_only"
    SAFE_ACTION = "safe_action"
    CONFIRM_REQUIRED = "confirm_required"
    HIGH_RISK = "high_risk"
    BLOCKED = "blocked"

    def index(self) -> int:
        return list(PermissionLevel).index(self)
