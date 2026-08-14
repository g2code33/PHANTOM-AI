"""Web tools: HTTP requests, web search, page reading, URL opening.

Safety:
- SSRF guard: connections to private/loopback/link-local addresses are refused.
- External content is returned as DATA (the agent core wraps it as data).
- Response size caps.
"""

from __future__ import annotations

import html
import ipaddress
import re
import socket
import urllib.parse
import webbrowser
from html.parser import HTMLParser
from typing import Any, Optional

import httpx

from .base import PermissionLevel, ToolContext, ToolError, ToolResult, ToolSpec

MAX_BODY = 512 * 1024
MAX_EXTRACT = 120 * 1024
HTTP_TIMEOUT = 25.0


class _LinkTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self._in_title = False
        self.text_parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._in_a = False
        self._href = ""
        self._a_text = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "title":
            self._in_title = True
        if tag == "a":
            self._in_a = True
            self._href = attrs.get("href", "")
            self._a_text = ""
        if tag in ("script", "style", "noscript", "svg", "iframe"):
            self._skip_depth = getattr(self, "_skip_depth", 0) + 1 if tag in ("script", "style", "noscript", "svg", "iframe") else 0
        if tag in ("script", "style", "noscript", "svg", "iframe"):
            self._skip = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag == "a" and self._in_a:
            if self._href and not self._href.startswith(("javascript:", "mailto:")):
                self.links.append((self._href, " ".join(self._a_text.split())[:120]))
            self._in_a = False
        if tag in ("script", "style", "noscript", "svg", "iframe"):
            self._skip = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._in_a:
            self._a_text += data
        if getattr(self, "_skip", False):
            return
        text = " ".join(data.split())
        if text:
            self.text_parts.append(text)


def _extract_page(raw: str) -> tuple[str, list[tuple[str, str]]]:
    parser = _LinkTextParser()
    try:
        parser.feed(raw[: 2 * MAX_BODY])
    except Exception:  # noqa: BLE001
        pass
    body = " ".join(parser.text_parts)
    if len(body) > MAX_EXTRACT:
        body = body[:MAX_EXTRACT] + " …(truncated)"
    return parser.title.strip(), parser.links[:100]


def _assert_public_host(url: str) -> None:
    """SSRF guard: refuse private/loopback/reserved destinations."""
    try:
        host = urllib.parse.urlparse(url).hostname
    except ValueError:
        raise ToolError(f"invalid URL: {url}", kind="invalid") from None
    if not host:
        raise ToolError("URL has no host", kind="invalid")
    try:
        addrs = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise ToolError(f"cannot resolve host: {host}", kind="network") from None
    for addr in addrs:
        ip = ipaddress.ip_address(addr[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise ToolError(f"refusing connection to non-public address {ip} ({host}) — SSRF guard",
                            kind="permission")


async def _http_request(ctx: ToolContext, url: str, method: str = "GET",
                        headers: dict[str, str] | None = None,
                        data: Any = None, timeout: float = HTTP_TIMEOUT) -> ToolResult:
    if not url.startswith(("http://", "https://")):
        raise ToolError("URL must start with http:// or https://", kind="invalid")
    _assert_public_host(url)
    method = method.upper()
    hdrs = {"User-Agent": "PHANTOM-AI/0.1 (personal assistant; contact owner)"}
    hdrs.update(headers or {})
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                 limits=httpx.Limits(max_connections=4)) as client:
        try:
            resp = await client.request(method, url, headers=hdrs, data=data)
            body = resp.content[:MAX_BODY]
        except httpx.TimeoutException:
            raise ToolError(f"request timed out after {timeout:g}s", kind="timeout", retryable=True) from None
        except httpx.HTTPError as exc:
            raise ToolError(f"request failed: {exc}", kind="network", retryable=True) from None
    content_type = resp.headers.get("content-type", "")
    text = ""
    if "text" in content_type or "json" in content_type or "xml" in content_type or "html" in content_type:
        text = body.decode("utf-8", errors="replace")
    elif body.startswith(b"\xef\xbb\xbf"):
        text = body.decode("utf-8-sig", errors="replace")
    else:
        text = f"(binary response: {len(body)} bytes, content-type: {content_type})"
    result = {
        "status": resp.status_code, "url": str(resp.url), "content_type": content_type,
        "bytes": len(body), "headers": dict(resp.headers),
    }
    output = f"HTTP {resp.status_code}  {resp.url}\n"
    if text and len(text) < MAX_EXTRACT:
        output += text
    else:
        output += text[:MAX_EXTRACT] + "\n…(truncated)"
    return ToolResult.ok(output, data=result)


async def _web_search(ctx: ToolContext, query: str, max_results: int = 8) -> ToolResult:
    """Real web search via DuckDuckGo's HTML endpoint (no API key needed)."""
    params = {"q": query, "kl": "us-en"}
    try:
        _assert_public_host("https://html.duckduckgo.com")
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get("https://html.duckduckgo.com/html/", params=params,
                                    headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) PHANTOM-AI"})
            resp.raise_for_status()
            raw = resp.text
    except httpx.HTTPError as exc:
        raise ToolError(f"web search failed: {exc}", kind="network", retryable=True) from None

    results: list[dict[str, str]] = []
    for m in re.finditer(
        r'<a rel="nofollow" class="result__a" href="([^"]+)"[^>]*>(.*?)</a>.*?'
        r'class="result__snippet"[^>]*>(.*?)</a>',
        raw, re.S,
    ):
        url = html.unescape(m.group(1))
        if url.startswith("//duckduckgo.com/l/?uddg="):
            url = urllib.parse.unquote(url.split("uddg=")[1].split("&")[0])
        title = html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        snippet = html.unescape(re.sub(r"<[^>]+>", "", m.group(3))).strip()
        results.append({"title": title, "url": url, "snippet": snippet[:400]})
        if len(results) >= max_results:
            break
    if not results:
        return ToolResult.ok(f"No search results for: {query}", data={"query": query, "results": []})
    text = "\n\n".join(f"{i+1}. {r['title']}\n   {r['url']}\n   {r['snippet']}"
                       for i, r in enumerate(results))
    return ToolResult.ok(text, data={"query": query, "results": results})


async def _fetch_webpage(ctx: ToolContext, url: str) -> ToolResult:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    _assert_public_host(url)
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) PHANTOM-AI"}) as client:
            resp = await client.get(url)
            raw = resp.text[: 2 * MAX_BODY]
    except httpx.TimeoutException:
        raise ToolError(f"page fetch timed out: {url}", kind="timeout", retryable=True) from None
    except httpx.HTTPError as exc:
        raise ToolError(f"page fetch failed: {exc}", kind="network", retryable=True) from None
    title, links = _extract_page(raw)
    body = f"URL: {resp.url}\nTITLE: {title or '(no title)'}\n\n{_extract_page(raw)[0]}"
    # re-extract cleanly
    title, links = _extract_page(raw)
    text_parts = []
    parser = _LinkTextParser()
    try:
        parser.feed(raw)
    except Exception:  # noqa: BLE001
        pass
    body_text = " ".join(parser.text_parts)[:MAX_EXTRACT]
    out = f"URL: {resp.url}\nTITLE: {title or '(no title)'}\n\n{body_text}"
    if len(parser.text_parts) > MAX_EXTRACT:
        out += "\n…(truncated)"
    data = {"url": str(resp.url), "title": title, "links": links[:50],
            "chars": len(body_text)}
    return ToolResult.ok(out, data=data)


async def _open_url(ctx: ToolContext, url: str) -> ToolResult:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    opened = webbrowser.open(url, new=2)
    if not opened:
        raise ToolError("could not open a browser on this machine", kind="unavailable")
    return ToolResult.ok(f"Opened {url} in the default browser", data={"url": url})


def register_web_tools(registry) -> None:
    registry.register(ToolSpec(
        name="http_request",
        description="Make an HTTP request to a public URL (GET/POST/PUT/DELETE). Refuses private-network targets.",
        purpose="Network/API requests", category="web",
        parameters={"url": {"type": "string", "required": True},
                    "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"], "default": "GET"},
                    "headers": {"type": "object", "additionalProperties": {"type": "string"}},
                    "data": {"type": ["string", "object", "null"]},
                    "timeout": {"type": "number", "minimum": 1, "maximum": 60, "default": HTTP_TIMEOUT}},
        handler=_http_request, permission=PermissionLevel.SAFE_ACTION, timeout=70,
        required_permission_notes="POST/PUT/DELETE change remote state — confirmation required.",
        parallel_safe=False,
    ))
    registry.register(ToolSpec(
        name="web_search",
        description="Search the web (DuckDuckGo HTML endpoint, no API key) and return top results with URLs.",
        purpose="Search the web", category="web",
        parameters={"query": {"type": "string", "required": True},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8}},
        handler=_web_search, permission=PermissionLevel.READ_ONLY, timeout=40,
    ))
    registry.register(ToolSpec(
        name="fetch_webpage",
        description="Fetch a web page and extract its readable text, title and links.",
        purpose="Read webpages", category="web",
        parameters={"url": {"type": "string", "required": True}},
        handler=_fetch_webpage, permission=PermissionLevel.READ_ONLY, timeout=40,
    ))
    registry.register(ToolSpec(
        name="open_url", description="Open a URL in the system default browser.",
        purpose="Open URLs", category="web",
        parameters={"url": {"type": "string", "required": True}},
        handler=_open_url, permission=PermissionLevel.SAFE_ACTION, timeout=15,
    ))
