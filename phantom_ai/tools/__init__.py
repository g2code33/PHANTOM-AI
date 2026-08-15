"""Tool registry assembly: register every capability exactly once.

Adding a capability = writing a ToolSpec + registering it here. The agent core
does not change.
"""

from __future__ import annotations

from .base import ToolContext, ToolError, ToolRegistry, ToolResult, ToolSpec
from .browser import register_browser_tools
from .clipboard import register_clipboard_tools
from .delegation_tools import register_delegation_tools
from .evolution_tools import register_evolution_tools
from .files import register_file_tools
from .gui import register_gui_tools
from .health_tools import register_health_tools
from .memory_tools import register_memory_tools
from .notify import register_notify_tools
from .profile_tools import register_profile_tools
from .processes import register_process_tools
from .system import register_system_tools
from .terminal import register_terminal_tools
from .web import register_web_tools


def build_registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_system_tools(registry)
    register_file_tools(registry)
    register_process_tools(registry)
    register_terminal_tools(registry)
    register_web_tools(registry)
    register_gui_tools(registry)
    register_clipboard_tools(registry)
    register_browser_tools(registry)
    register_memory_tools(registry)
    register_delegation_tools(registry)
    register_notify_tools(registry)
    register_evolution_tools(registry)
    register_health_tools(registry)
    register_profile_tools(registry)
    return registry


__all__ = ["build_registry", "ToolContext", "ToolError", "ToolRegistry", "ToolResult", "ToolSpec"]
