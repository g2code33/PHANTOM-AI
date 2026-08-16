"""Local OpenAI-compatible mock server used to verify the NVIDIA client and
the full agent stack end-to-end without internet access.

The mock speaks the same SSE chat/completions protocol as NVIDIA NIM, so the
real NVIDIAProvider code path is exercised against it. A test sets a handler
that decides what the "model" returns per request.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

Handler = Callable[[dict[str, Any]], Awaitable[Any]]


class MockState:
    def __init__(self) -> None:
        self.handler: Optional[Handler] = None
        self.requests: list[dict[str, Any]] = []
        self.models: list[str] = ["meta/llama-3.3-70b-instruct", "nvidia/deepseek-r1"]
        self.fail_next: Optional[tuple[int, str]] = None  # (status, body)


def make_mock_app(state: MockState) -> FastAPI:
    app = FastAPI()

    @app.get("/v1/models")
    async def models():
        return {"object": "list", "data": [{"id": m} for m in state.models]}

    @app.post("/v1/chat/completions")
    async def completions(request: Request):
        body = await request.json()
        state.requests.append(body)
        if state.fail_next:
            status, text = state.fail_next
            state.fail_next = None
            return JSONResponse(status_code=status, content={"error": {"message": text}})
        handler = state.handler or _default_handler
        result = await handler(body)
        if isinstance(result, tuple):
            status, payload = result
            return JSONResponse(status_code=status, content=payload)
        stream = bool(body.get("stream"))
        if stream:
            return StreamingResponse(_sse(result, body.get("model", "")),
                                     media_type="text/event-stream")
        return JSONResponse(content=result)

    return app


async def _default_handler(body: dict[str, Any]) -> dict[str, Any]:
    messages = body.get("messages", [])
    last = messages[-1] if messages else {}
    return {"content": f"echo: {(last.get('content') or '')[:200]}", "tool_calls": []}


async def _sse(result: dict[str, Any], model: str):
    content = result.get("content", "")
    tool_calls = result.get("tool_calls", []) or []
    finish = result.get("finish_reason", "tool_calls" if tool_calls else "stop")
    if content:
        for i in range(0, len(content), 7):
            payload = {
                "id": "chatcmpl-mock", "object": "chat.completion.chunk", "model": model,
                "choices": [{"index": 0, "delta": {"content": content[i:i + 7]},
                             "finish_reason": None}],
            }
            yield f"data: {json.dumps(payload)}\n\n"
            await asyncio.sleep(0)
    for idx, tc in enumerate(tool_calls):
        fn = tc.get("function", {})
        delta_tc = {
            "index": idx,
            "id": tc.get("id", f"call_{idx}"),
            "type": "function",
            "function": {"name": fn.get("name", ""),
                         "arguments": json.dumps(fn.get("arguments", {}))},
        }
        payload = {
            "id": "chatcmpl-mock", "object": "chat.completion.chunk", "model": model,
            "choices": [{"index": 0, "delta": {"tool_calls": [delta_tc]}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(payload)}\n\n"
        await asyncio.sleep(0)
    usage = {"prompt_tokens": 42, "completion_tokens": 17, "total_tokens": 59}
    payload = {
        "id": "chatcmpl-mock", "object": "chat.completion.chunk", "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
        "usage": usage,
    }
    yield f"data: {json.dumps(payload)}\n\n"
    yield "data: [DONE]\n\n"
