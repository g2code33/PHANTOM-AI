"""NVIDIA NIM-compatible provider (OpenAI-compatible chat/completions API).

Features:
- Streaming (SSE) with usage reporting
- Retry with exponential backoff for 429/5xx/network errors (only before any
  content has streamed — never mid-stream)
- Connection reuse (shared httpx.AsyncClient)
- Timeouts (connect/read/write/pool)
- Structured ProviderError for auth / rate limit / server / network / timeout /
  invalid request / unsupported tool calling
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from typing import Any, AsyncIterator, Optional

import httpx

from .base import (
    ChatMessage,
    ChatResult,
    ModelProvider,
    ProviderError,
    StreamDone,
    StreamError,
    TextChunk,
    ToolCall,
    ToolCallChunk,
    Usage,
)

DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"

# Models known to lack native function calling → the agent falls back to the
# text-based tool protocol. (Kept as a hint; we also detect via API error.)
NO_TOOL_CALLING_MODELS: frozenset[str] = frozenset(
    {"nvidia/llama-3.1-nemotron-ultra-253b-v1", "nvidia/llama-3.1-nemotron-nano-8b-v1"}
)


class NVIDIAProvider(ModelProvider):
    name = "nvidia"

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = DEFAULT_BASE_URL,
        *,
        max_retries: int = 3,
        connect_timeout: float = 10.0,
        read_timeout: float = 120.0,
        write_timeout: float = 30.0,
        pool_timeout: float = 10.0,
        request_timeout: float = 300.0,
        default_temperature: float = 0.4,
        default_max_tokens: int = 2048,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.default_temperature = default_temperature
        self.default_max_tokens = default_max_tokens
        self._request_timeout = request_timeout
        self._client: Optional[httpx.AsyncClient] = None
        self._timeout = httpx.Timeout(
            connect=connect_timeout, read=read_timeout, write=write_timeout, pool=pool_timeout
        )
        self.has_key = bool(api_key)

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                headers={"Authorization": f"Bearer {self.api_key}"},
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=8),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # chat completions
    # ------------------------------------------------------------------

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: Optional[list[dict]] = None,
        tool_choice: Optional[str] = None,
        temperature: float = 0.4,
        max_tokens: int = 2048,
    ) -> AsyncIterator[Any]:
        payload = self._build_payload(messages, tools, tool_choice, temperature, max_tokens, stream=True)
        started = time.monotonic()
        attempt = 0
        content_parts: list[str] = []
        tool_calls: dict[int, dict[str, Any]] = {}
        finish_reason = ""
        usage = Usage()

        while True:
            attempt += 1
            try:
                async with self.client.stream("POST", f"{self.base_url}/chat/completions", json=payload) as resp:
                    if resp.status_code >= 400:
                        await resp.aread()
                        err = self._error_from_response(resp)
                        if err.retryable and attempt <= self.max_retries:
                            await self._backoff(attempt, err)
                            continue
                        yield StreamError(err)
                        return
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if not data or data == "[DONE]":
                            continue
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        delta = ((chunk.get("choices") or [{}])[0]).get("delta") or {}
                        if chunk.get("usage"):
                            usage = self._parse_usage(chunk["usage"])
                        if delta.get("content"):
                            content_parts.append(delta["content"])
                            yield TextChunk(delta["content"])
                        for tc in delta.get("tool_calls") or []:
                            idx = tc.get("index", 0)
                            entry = tool_calls.setdefault(idx, {"id": "", "name": "", "args": ""})
                            if tc.get("id"):
                                entry["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                entry["name"] += fn["name"]
                            if fn.get("arguments"):
                                entry["args"] += fn["arguments"]
                        fr = ((chunk.get("choices") or [{}])[0]).get("finish_reason")
                        if fr:
                            finish_reason = fr
                    break
            except httpx.TimeoutException as exc:
                if attempt <= self.max_retries:
                    await self._backoff(attempt, ProviderError("timeout", str(exc), retryable=True))
                    continue
                yield StreamError(ProviderError("timeout", f"NVIDIA request timed out: {exc}", retryable=True))
                return
            except (httpx.NetworkError, httpx.TransportError) as exc:
                if attempt <= self.max_retries:
                    await self._backoff(attempt, ProviderError("network", str(exc), retryable=True))
                    continue
                yield StreamError(ProviderError("network", f"NVIDIA unreachable: {exc}", retryable=True))
                return
            except asyncio.CancelledError:
                yield StreamError(ProviderError("cancelled", "request cancelled"))
                return

        parsed_calls = [
            ToolCall(
                id=entry.get("id") or f"call_{i}",
                name=entry.get("name", ""),
                arguments=_parse_args(entry.get("args", "")),
            )
            for i, entry in sorted(tool_calls.items())
        ]
        for call in parsed_calls:
            yield ToolCallChunk(call)
        yield StreamDone(
            content="".join(content_parts),
            tool_calls=parsed_calls,
            usage=usage,
            model=self.model,
            finish_reason=finish_reason,
            latency_ms=(time.monotonic() - started) * 1000,
        )

    def _build_payload(
        self,
        messages: list[ChatMessage],
        tools: Optional[list[dict]],
        tool_choice: Optional[str],
        temperature: float,
        max_tokens: int,
        stream: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [m.to_dict() for m in messages],
            "stream": stream,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
        return payload

    def _error_from_response(self, resp: httpx.Response) -> ProviderError:
        status = resp.status_code
        body = resp.text[:500]
        try:
            j = resp.json()
            body = (j.get("error") or {}).get("message") or body
        except Exception:
            pass
        if status == 401 or status == 403:
            return ProviderError("auth", f"NVIDIA rejected the API key (HTTP {status}): {body}")
        if status == 429:
            return ProviderError("rate_limit", f"NVIDIA rate limited (HTTP 429): {body}", retryable=True, status_code=status)
        if status in (500, 502, 503, 504):
            return ProviderError("server", f"NVIDIA server error (HTTP {status}): {body}", retryable=True, status_code=status)
        if status == 400 and ("tool" in body.lower() or "function" in body.lower()):
            return ProviderError("unsupported_tool_calling", f"Model does not support native tool calling: {body}", status_code=status)
        return ProviderError(
            "invalid_request",
            f"NVIDIA request failed (HTTP {status}) on {self.base_url}/chat/completions "
            f"(model={self.model}): {body}", status_code=status)

    async def _backoff(self, attempt: int, err: ProviderError) -> None:
        base = min(0.5 * (2 ** (attempt - 1)), 8.0)
        jitter = random.uniform(0, 0.3 * base)
        await asyncio.sleep(base + jitter)

    @staticmethod
    def _parse_usage(u: dict[str, Any]) -> Usage:
        return Usage(
            prompt_tokens=int(u.get("prompt_tokens") or 0),
            completion_tokens=int(u.get("completion_tokens") or 0),
            total_tokens=int(u.get("total_tokens") or 0),
        )

    # ------------------------------------------------------------------
    # connectivity / metadata
    # ------------------------------------------------------------------

    async def check(self) -> dict[str, Any]:
        if not self.has_key:
            return {"provider": self.name, "model": self.model, "ok": False,
                    "detail": "no API key configured — set it in Settings"}
        try:
            started = time.monotonic()
            async with self.client.stream("GET", f"{self.base_url}/models") as resp:
                await resp.aread()
                latency = (time.monotonic() - started) * 1000
                if resp.status_code == 200:
                    return {"provider": self.name, "model": self.model, "ok": True,
                            "detail": f"connected (HTTP 200, {latency:.0f} ms)"}
                return {"provider": self.name, "model": self.model, "ok": False,
                        "detail": f"HTTP {resp.status_code}"}
        except Exception as exc:  # noqa: BLE001
            return {"provider": self.name, "model": self.model, "ok": False,
                    "detail": f"unreachable: {type(exc).__name__}"}


def _parse_args(raw: str) -> dict[str, Any]:
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}
