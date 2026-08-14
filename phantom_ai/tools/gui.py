"""GUI / desktop-interaction tools.

Real implementations (pyautogui + mss) that operate the actual desktop.
On headless machines (servers, CI, this sandbox) they return honest
"environment unavailable" errors — they never simulate clicks.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

from ..config import SCREENSHOT_DIR
from .base import PermissionLevel, ToolContext, ToolError, ToolResult, ToolSpec


def _require_display() -> None:
    if os.name == "nt":
        return
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise ToolError(
            "no graphical display is available in this environment — desktop interaction "
            "(mouse/keyboard/windows) works on a real desktop session",
            kind="unavailable",
        )


def _pyautogui():
    try:
        import pyautogui
    except ImportError:
        raise ToolError("pyautogui is not installed (pip install pyautogui)", kind="unavailable") from None
    return pyautogui


async def _take_screenshot(ctx: ToolContext, name: str = "") -> ToolResult:
    try:
        import mss
    except ImportError:
        raise ToolError("mss is not installed (pip install mss)", kind="unavailable") from None
    try:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        fname = f"{name or 'screenshot'}_{__import__('time').strftime('%Y%m%d_%H%M%S')}.png"
        path = SCREENSHOT_DIR / fname
        with mss.mss() as sct:
            sct.shot(monitor=1, output=str(path))
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"screenshot failed: {exc}", kind="unavailable") from None
    size = os.path.getsize(path)
    return ToolResult.ok(
        f"Screenshot saved: {path} ({size} bytes)",
        data={"path": str(path), "size": size},
        artifacts=[{"type": "image", "path": str(path)}],
    )


async def _screen_info(ctx: ToolContext) -> ToolResult:
    try:
        import mss
    except ImportError:
        raise ToolError("mss is not installed", kind="unavailable") from None
    try:
        with mss.mss() as sct:
            monitors = [{"left": m["left"], "top": m["top"], "width": m["width"],
                         "height": m["height"]} for m in sct.monitors[1:]]
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"cannot read screen: {exc}", kind="unavailable") from None
    if not monitors:
        raise ToolError("no monitors detected", kind="unavailable")
    return ToolResult.ok(
        "\n".join(f"monitor {i+1}: {m['width']}x{m['height']} at ({m['left']},{m['top']})"
                  for i, m in enumerate(monitors)),
        data={"monitors": monitors})


async def _mouse_move(ctx: ToolContext, x: int, y: int, duration: float = 0.2) -> ToolResult:
    _require_display()
    _pyautogui().moveTo(x, y, duration=duration)
    return ToolResult.ok(f"Mouse moved to ({x}, {y})", data={"x": x, "y": y})


async def _mouse_click(ctx: ToolContext, x: int | None = None, y: int | None = None,
                       button: str = "left", clicks: int = 1) -> ToolResult:
    _require_display()
    pg = _pyautogui()
    if x is not None and y is not None:
        pg.moveTo(x, y, duration=0.1)
    pg.click(button=button, clicks=clicks, interval=0.08 if clicks > 1 else 0.0)
    pos = pg.position()
    return ToolResult.ok(f"Mouse {button}-clicked {clicks}x at {pos}", data={"x": pos.x, "y": pos.y, "button": button})


async def _mouse_scroll(ctx: ToolContext, amount: int, x: int | None = None, y: int | None = None) -> ToolResult:
    _require_display()
    pg = _pyautogui()
    if x is not None and y is not None:
        pg.moveTo(x, y, duration=0.1)
    pg.scroll(amount)
    return ToolResult.ok(f"Scrolled {amount} clicks", data={"amount": amount})


async def _type_text(ctx: ToolContext, text: str, interval: float = 0.01) -> ToolResult:
    _require_display()
    if len(text) > 2000:
        raise ToolError("text too long to type (max 2000 chars)", kind="invalid")
    pg = _pyautogui()
    pg.write(text, interval=interval)
    return ToolResult.ok(f"Typed {len(text)} characters", data={"chars": len(text)})


async def _press_key(ctx: ToolContext, key: str, modifiers: list[str] | None = None,
                     presses: int = 1) -> ToolResult:
    _require_display()
    pg = _pyautogui()
    combo = (modifiers or []) + [key]
    for _ in range(min(presses, 10)):
        pg.hotkey(*combo)
    return ToolResult.ok(f"Pressed {'+'.join(combo)}", data={"keys": combo})


async def _window_list(ctx: ToolContext) -> ToolResult:
    _require_display()
    if os.name == "nt":
        raise ToolError("window listing on Windows needs pygetwindow (pip install pygetwindow)",
                        kind="unavailable")
    try:
        out = subprocess.run(["xdotool", "search", "--onlyvisible", "--name", "."],
                             capture_output=True, text=True, timeout=5)
        windows = []
        for wid in out.stdout.split():
            info = subprocess.run(["xdotool", "getwindowname", wid], capture_output=True, text=True)
            name = info.stdout.strip()
            if name:
                windows.append({"id": wid, "name": name})
    except FileNotFoundError:
        raise ToolError("xdotool not installed (apt install xdotool)", kind="unavailable") from None
    if not windows:
        return ToolResult.ok("No visible windows found", data={"windows": []})
    text = "\n".join(f"{w['id']}: {w['name']}" for w in windows[:100])
    return ToolResult.ok(text, data={"windows": windows[:100]})


async def _window_activate(ctx: ToolContext, name: str) -> ToolResult:
    _require_display()
    try:
        result = subprocess.run(["xdotool", "search", "--onlyvisible", "--name", name],
                                capture_output=True, text=True, timeout=5)
        wid = result.stdout.strip().splitlines()[0] if result.stdout.strip() else None
        if not wid:
            raise ToolError(f"no visible window matching '{name}'", kind="not_found")
        subprocess.run(["xdotool", "windowactivate", wid], capture_output=True, timeout=5, check=True)
        return ToolResult.ok(f"Activated window '{name}' (id {wid})", data={"window_id": wid, "name": name})
    except FileNotFoundError:
        raise ToolError("xdotool not installed (apt install xdotool)", kind="unavailable") from None
    except subprocess.CalledProcessError as exc:
        raise ToolError(f"could not activate window: {exc}", kind="error") from None


def register_gui_tools(registry) -> None:
    registry.register(ToolSpec(
        name="take_screenshot",
        description="Capture the screen to a PNG file and report its path.",
        purpose="Visual inspection of the screen", category="gui",
        parameters={"name": {"type": "string", "default": "", "description": "Optional filename prefix"}},
        handler=_take_screenshot, permission=PermissionLevel.READ_ONLY, timeout=15,
    ))
    registry.register(ToolSpec(
        name="screen_info", description="List connected monitors and their resolutions.",
        purpose="Inspect the display", category="gui",
        parameters={}, handler=_screen_info, permission=PermissionLevel.READ_ONLY, timeout=10,
    ))
    registry.register(ToolSpec(
        name="mouse_move", description="Move the mouse to an absolute screen position.",
        purpose="Mouse movement", category="gui",
        parameters={"x": {"type": "integer", "required": True}, "y": {"type": "integer", "required": True},
                    "duration": {"type": "number", "minimum": 0, "maximum": 5, "default": 0.2}},
        handler=_mouse_move, permission=PermissionLevel.SAFE_ACTION, timeout=10,
    ))
    registry.register(ToolSpec(
        name="mouse_click", description="Click (left/right/middle) at a position or the current cursor position.",
        purpose="Clicking", category="gui",
        parameters={"x": {"type": "integer"}, "y": {"type": "integer"},
                    "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
                    "clicks": {"type": "integer", "minimum": 1, "maximum": 3, "default": 1}},
        handler=_mouse_click, permission=PermissionLevel.SAFE_ACTION, timeout=10,
    ))
    registry.register(ToolSpec(
        name="mouse_scroll", description="Scroll the mouse wheel by a signed amount.",
        purpose="Scrolling", category="gui",
        parameters={"amount": {"type": "integer", "required": True},
                    "x": {"type": "integer"}, "y": {"type": "integer"}},
        handler=_mouse_scroll, permission=PermissionLevel.SAFE_ACTION, timeout=10,
    ))
    registry.register(ToolSpec(
        name="type_text", description="Type text into the focused application (real keystrokes).",
        purpose="Keyboard input", category="gui",
        parameters={"text": {"type": "string", "required": True, "maxLength": 2000},
                    "interval": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.01}},
        handler=_type_text, permission=PermissionLevel.SAFE_ACTION, timeout=60,
    ))
    registry.register(ToolSpec(
        name="press_key", description="Press keys / keyboard shortcuts, e.g. key='enter', modifiers=['ctrl','shift'].",
        purpose="Keyboard shortcuts", category="gui",
        parameters={"key": {"type": "string", "required": True},
                    "modifiers": {"type": "array", "items": {"type": "string"}},
                    "presses": {"type": "integer", "minimum": 1, "maximum": 10, "default": 1}},
        handler=_press_key, permission=PermissionLevel.SAFE_ACTION, timeout=10,
    ))
    registry.register(ToolSpec(
        name="window_list", description="List visible windows with their ids and titles.",
        purpose="Window interaction", category="gui",
        parameters={}, handler=_window_list, permission=PermissionLevel.READ_ONLY, timeout=15,
    ))
    registry.register(ToolSpec(
        name="window_activate", description="Focus a window by title substring.",
        purpose="Window interaction", category="gui",
        parameters={"name": {"type": "string", "required": True}},
        handler=_window_activate, permission=PermissionLevel.SAFE_ACTION, timeout=10,
    ))
