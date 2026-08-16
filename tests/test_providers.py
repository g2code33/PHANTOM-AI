"""TIER 1 — provider tests: auth, streaming, retries, timeouts, tool calls,
usage reporting, connectivity, offline fallback."""

from __future__ import annotations

import asyncio

import pytest

from phantom_ai.providers.base import (
    ChatMessage,
    ProviderError,
    StreamDone,
    StreamError,
    TextChunk,
    ToolCallChunk,
)
from phantom_ai.providers.mock import OfflineProvider
from phantom_ai.providers.nvidia import NVIDIAProvider


@pytest.fixture
def provider(mock_server):
    state, base_url = mock_server
    prov = NVIDIAProvider(api_key="nvapi-test-key", model="meta/llama-3.3-70b-instruct",
                          base_url=base_url)
    yield prov, state
    asyncio.get_event_loop().run_until_complete(prov.aclose())


async def test_streaming_concatenates_content(mock_server):
    state, base_url = mock_server

    async def handler(body):
        return {"content": "Hello from NVIDIA mock!", "tool_calls": []}

    state.handler = handler
    prov = NVIDIAProvider(api_key="nvapi-test-key", model="meta/llama-3.3-70b-instruct",
                          base_url=base_url)
    try:
        parts = []
        done = None
        async for ev in prov.stream([ChatMessage(role="user", content="hi")]):
            if isinstance(ev, TextChunk):
                parts.append(ev.text)
            elif isinstance(ev, StreamDone):
                done = ev
        assert "".join(parts) == "Hello from NVIDIA mock!"
        assert done is not None
        assert done.usage.total_tokens == 59
        assert done.model == "meta/llama-3.3-70b-instruct"
    finally:
        await prov.aclose()


async def test_tool_call_streaming(mock_server):
    state, base_url = mock_server

    async def handler(body):
        return {"content": "", "tool_calls": [{"id": "call_abc", "type": "function",
                                               "function": {"name": "read_file",
                                                            "arguments": {"path": "/tmp/x"}}}]}

    state.handler = handler
    prov = NVIDIAProvider(api_key="nvapi-test-key", model="meta/llama-3.3-70b-instruct",
                          base_url=base_url)
    try:
        calls = []
        async for ev in prov.stream([ChatMessage(role="user", content="read a file")],
                                    tools=[{"type": "function", "function": {"name": "read_file"}}]):
            if isinstance(ev, ToolCallChunk):
                calls.append(ev.tool_call)
        assert len(calls) == 1
        assert calls[0].name == "read_file"
        assert calls[0].arguments == {"path": "/tmp/x"}
        assert calls[0].id == "call_abc"
    finally:
        await prov.aclose()


async def test_auth_error(mock_server):
    state, base_url = mock_server
    state.handler = _coro((401, {"error": {"message": "invalid api key"}}))
    prov = NVIDIAProvider(api_key="wrong", model="m", base_url=base_url)
    try:
        with pytest.raises(ProviderError) as ei:
            async for ev in prov.stream([ChatMessage(role="user", content="hi")]):
                if isinstance(ev, StreamError):
                    raise ev.error
        assert ei.value.kind == "auth"
    finally:
        await prov.aclose()


async def test_retry_on_rate_limit_then_success(mock_server):
    state, base_url = mock_server
    attempts = {"n": 0}

    async def handler(body):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return 429, {"error": {"message": "rate limited"}}
        return {"content": "ok after retry", "tool_calls": []}

    state.handler = handler
    prov = NVIDIAProvider(api_key="nvapi-test-key", model="m", base_url=base_url,
                          max_retries=3)
    try:
        done = None
        async for ev in prov.stream([ChatMessage(role="user", content="hi")]):
            if isinstance(ev, StreamDone):
                done = ev
        assert done is not None and "retry" in done.content
        assert attempts["n"] == 2
    finally:
        await prov.aclose()


async def test_timeout(mock_server):
    state, base_url = mock_server

    async def handler(body):
        await asyncio.sleep(3.0)
        return {"content": "late", "tool_calls": []}

    state.handler = handler
    prov = NVIDIAProvider(api_key="nvapi-test-key", model="m", base_url=base_url,
                          connect_timeout=1.0, read_timeout=0.5, max_retries=1)
    try:
        with pytest.raises(ProviderError) as ei:
            async for ev in prov.stream([ChatMessage(role="user", content="hi")]):
                if isinstance(ev, StreamError):
                    raise ev.error
        assert ei.value.kind == "timeout"
    finally:
        await prov.aclose()


async def test_check_connection(mock_server):
    state, base_url = mock_server
    prov = NVIDIAProvider(api_key="nvapi-test-key", model="m", base_url=base_url)
    try:
        status = await prov.check()
        assert status["ok"] is True
    finally:
        await prov.aclose()


async def test_offline_provider_is_honest():
    prov = OfflineProvider()
    assert prov.has_key is False
    done = None
    async for ev in prov.stream([ChatMessage(role="user", content="hello")]):
        if isinstance(ev, StreamDone):
            done = ev
    assert "offline" in done.content.lower()
    status = await prov.check()
    assert status["ok"] is False


def _coro(result):
    async def _inner(body):
        return result

    return _inner
