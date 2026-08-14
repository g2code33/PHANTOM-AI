"""Browser automation tools via Playwright (real browser control).

Playwright is an optional dependency (`pip install playwright && playwright
install chromium`). Without it, tools fail with a clear structured error —
actions are never simulated.
"""

from __future__ import annotations

from typing import Any

from .base import PermissionLevel, ToolContext, ToolError, ToolResult, ToolSpec

_playwright = None
_browser_pool: dict[str, Any] = {}


def _ensure_playwright():
    global _playwright
    if _playwright is None:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise ToolError(
                "Playwright is not installed. Run: pip install playwright && "
                "playwright install chromium — then browser automation works.",
                kind="unavailable",
            ) from None
        _playwright = async_playwright
    return _playwright


async def _browser_open(ctx: ToolContext, url: str = "about:blank",
                        headless: bool = False) -> ToolResult:
    pw = _ensure_playwright()
    p = await pw().start()
    try:
        browser = await p.chromium.launch(headless=headless)
        page = await browser.new_page()
        _browser_pool[ctx.session_id or ctx.agent_id] = {"browser": browser, "page": page}
        if url and url != "about:blank":
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            await page.goto(url, timeout=30_000, wait_until="domcontentloaded")
        return ToolResult.ok(f"Browser opened at {url}", data={"url": url})
    except Exception as exc:  # noqa: BLE001
        await p.stop()
        raise ToolError(f"browser launch failed: {exc}", kind="error") from None


def _session(ctx: ToolContext) -> tuple[Any, Any]:
    entry = _browser_pool.get(ctx.session_id or ctx.agent_id)
    if not entry:
        raise ToolError("no browser session open — call browser_open first", kind="not_found")
    return entry["browser"], entry["page"]


async def _browser_navigate(ctx: ToolContext, url: str) -> ToolResult:
    _, page = _session(ctx)
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        await page.goto(url, timeout=30_000, wait_until="domcontentloaded")
        return ToolResult.ok(f"Navigated to {url} — title: {await page.title()}",
                             data={"url": url, "title": await page.title()})
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"navigation failed: {exc}", kind="error") from None


async def _browser_read_text(ctx: ToolContext) -> ToolResult:
    _, page = _session(ctx)
    try:
        text = await page.inner_text("body")
        text = " ".join(text.split())[:50_000]
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"read failed: {exc}", kind="error") from None
    return ToolResult.ok(text[:20_000], data={"chars": len(text)})


async def _browser_click(ctx: ToolContext, selector: str) -> ToolResult:
    _, page = _session(ctx)
    try:
        await page.click(selector, timeout=10_000)
        return ToolResult.ok(f"Clicked {selector}", data={"selector": selector})
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"click failed: {exc}", kind="error") from None


async def _browser_type(ctx: ToolContext, selector: str, text: str) -> ToolResult:
    _, page = _session(ctx)
    try:
        await page.fill(selector, text)
        return ToolResult.ok(f"Filled {selector} with {len(text)} chars", data={"selector": selector})
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"fill failed: {exc}", kind="error") from None


async def _browser_screenshot(ctx: ToolContext, name: str = "browser") -> ToolResult:
    from ..config import SCREENSHOT_DIR

    _, page = _session(ctx)
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    import time as _t

    path = SCREENSHOT_DIR / f"{name}_{_t.strftime('%Y%m%d_%H%M%S')}.png"
    try:
        await page.screenshot(path=str(path))
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"screenshot failed: {exc}", kind="error") from None
    return ToolResult.ok(f"Browser screenshot: {path}", data={"path": str(path)},
                         artifacts=[{"type": "image", "path": str(path)}])


async def _browser_close(ctx: ToolContext) -> ToolResult:
    browser, _ = _session(ctx)
    try:
        await browser.close()
    except Exception:  # noqa: BLE001
        pass
    _browser_pool.pop(ctx.session_id or ctx.agent_id, None)
    return ToolResult.ok("Browser closed", data={})


def register_browser_tools(registry) -> None:
    registry.register(ToolSpec(
        name="browser_open", description="Open an automated browser (Chromium via Playwright).",
        purpose="Open browser", category="browser",
        parameters={"url": {"type": "string", "default": "about:blank"},
                    "headless": {"type": "boolean", "default": False}},
        handler=_browser_open, permission=PermissionLevel.SAFE_ACTION, timeout=45,
    ))
    registry.register(ToolSpec(
        name="browser_navigate", description="Navigate the open browser to a URL.",
        purpose="Navigate to URLs", category="browser",
        parameters={"url": {"type": "string", "required": True}},
        handler=_browser_navigate, permission=PermissionLevel.SAFE_ACTION, timeout=45,
    ))
    registry.register(ToolSpec(
        name="browser_read_text", description="Read the visible text of the current browser page.",
        purpose="Extract information", category="browser",
        parameters={}, handler=_browser_read_text, permission=PermissionLevel.READ_ONLY, timeout=20,
    ))
    registry.register(ToolSpec(
        name="browser_click", description="Click an element by CSS selector in the browser.",
        purpose="Click elements", category="browser",
        parameters={"selector": {"type": "string", "required": True}},
        handler=_browser_click, permission=PermissionLevel.SAFE_ACTION, timeout=20,
    ))
    registry.register(ToolSpec(
        name="browser_type", description="Fill a form field (CSS selector) with text in the browser.",
        purpose="Fill forms", category="browser",
        parameters={"selector": {"type": "string", "required": True}, "text": {"type": "string", "required": True}},
        handler=_browser_type, permission=PermissionLevel.SAFE_ACTION, timeout=20,
    ))
    registry.register(ToolSpec(
        name="browser_screenshot", description="Screenshot the current browser page.",
        purpose="Take screenshots", category="browser",
        parameters={"name": {"type": "string", "default": "browser"}},
        handler=_browser_screenshot, permission=PermissionLevel.READ_ONLY, timeout=20,
    ))
    registry.register(ToolSpec(
        name="browser_close", description="Close the automated browser session.",
        purpose="Close browser", category="browser",
        parameters={}, handler=_browser_close, permission=PermissionLevel.SAFE_ACTION, timeout=15,
    ))
