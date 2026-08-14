"""Model provider abstraction.

The agent core talks only to this interface, so the model can be swapped
(NVIDIA NIM today, other providers later) without touching agent logic.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChatMessage:
    role: str  # system | user | assistant | tool
    content: Optional[str] = None
    tool_calls: Optional[list[ToolCall]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"role": self.role}
        if self.content is not None:
            out["content"] = self.content
        if self.tool_calls:
            out["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": _dump_args(tc.arguments)},
                }
                for tc in self.tool_calls
            ]
        if self.tool_call_id:
            out["tool_call_id"] = self.tool_call_id
        if self.name:
            out["name"] = self.name
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChatMessage":
        tcs = None
        if d.get("tool_calls"):
            tcs = [
                ToolCall(
                    id=tc.get("id", ""),
                    name=(tc.get("function") or {}).get("name", ""),
                    arguments=_parse_args((tc.get("function") or {}).get("arguments", "{}")),
                )
                for tc in d["tool_calls"]
            ]
        return cls(
            role=d.get("role", "user"),
            content=d.get("content"),
            tool_calls=tcs,
            tool_call_id=d.get("tool_call_id"),
            name=d.get("name"),
        )


def _dump_args(args: dict[str, Any]) -> str:
    import json

    try:
        return json.dumps(args)
    except (TypeError, ValueError):
        return "{}"


def _parse_args(raw: str) -> dict[str, Any]:
    import json

    try:
        return json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    except (json.JSONDecodeError, TypeError):
        return {}


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass
class ChatResult:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    latency_ms: float = 0.0
    finish_reason: str = ""


# --- streaming events ------------------------------------------------------

@dataclass
class TextChunk:
    text: str


@dataclass
class ToolCallChunk:
    tool_call: ToolCall


@dataclass
class StreamDone:
    content: str
    tool_calls: list[ToolCall]
    usage: Usage
    model: str
    finish_reason: str
    latency_ms: float


@dataclass
class StreamError:
    error: "ProviderError"


class ProviderError(Exception):
    """Structured provider failure.

    kind: auth | rate_limit | server | network | timeout | invalid_request
          | unsupported_tool_calling | cancelled
    """

    def __init__(self, kind: str, message: str, retryable: bool = False,
                 status_code: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retryable = retryable
        self.status_code = status_code

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "message": self.message, "status_code": self.status_code}


class ModelProvider(abc.ABC):
    """Abstract model provider interface."""

    name: str = "abstract"
    model: str = ""
    base_url: str = ""
    has_key: bool = False

    @abc.abstractmethod
    async def stream(
        self,
        messages: list[ChatMessage],
        tools: Optional[list[dict]] = None,
        tool_choice: Optional[str] = None,
        temperature: float = 0.4,
        max_tokens: int = 2048,
    ) -> AsyncIterator[Any]:
        """Yield TextChunk / ToolCallChunk / StreamDone / StreamError."""
        raise NotImplementedError

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: Optional[list[dict]] = None,
        tool_choice: Optional[str] = None,
        temperature: float = 0.4,
        max_tokens: int = 2048,
    ) -> ChatResult:
        content_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        usage = Usage()
        model = self.model
        latency = 0.0
        finish = ""
        async for event in self.stream(messages, tools, tool_choice, temperature, max_tokens):
            if isinstance(event, TextChunk):
                content_parts.append(event.text)
            elif isinstance(event, ToolCallChunk):
                tool_calls.append(event.tool_call)
            elif isinstance(event, StreamDone):
                usage, model, latency, finish = event.usage, event.model, event.latency_ms, event.finish_reason
            elif isinstance(event, StreamError):
                raise event.error
        return ChatResult(
            content="".join(content_parts), tool_calls=tool_calls, usage=usage,
            model=model, latency_ms=latency, finish_reason=finish,
        )

    async def check(self) -> dict[str, Any]:
        """Return a connectivity status dict for the UI."""
        return {"provider": self.name, "model": self.model, "ok": self.has_key,
                "detail": "no API key configured" if not self.has_key else "unknown"}
