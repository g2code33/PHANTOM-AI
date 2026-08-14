"""Offline provider — used ONLY when no NVIDIA API key is configured.

This is not a fake AI. It is an honest "offline mode" that tells the user the
system is fully functional but unconfigured, and how to enable the real model.
It performs no tool calling and simulates nothing.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, AsyncIterator, Optional

from .base import ChatMessage, ModelProvider, ProviderError, StreamDone, TextChunk, ToolCallChunk, Usage

HELP_TEXT = """I'm running in **offline demo mode**.

No NVIDIA API key is configured yet, so I can't use a real language model. Everything else — tools, memory, permissions, delegations, audit — is live and functional.

To enable the real AI:
1. Open **Settings → API Keys**.
2. Paste your NVIDIA API key for Phantom and/or Coded.
   (or set the `PHANTOM_NVIDIA_API_KEY` / `CODED_NVIDIA_API_KEY` environment variables)
3. Return here and send a message.

You can still try the system now:
- Use the **Tool Lab** (right panel) to execute real tools like file search or system info.
- Open the **Memory center**, **Task manager**, and **Audit log** to see real activity.
"""


class OfflineProvider(ModelProvider):
    """Deterministic, clearly-labeled placeholder used when a key is absent."""

    name = "offline"

    def __init__(self, model: str = "offline", agent_label: str = "agent") -> None:
        self.model = model
        self.base_url = ""
        self.has_key = False
        self.agent_label = agent_label

    def _reply(self, messages: list[ChatMessage]) -> str:
        last_user = next(
            (m.content for m in reversed(messages) if m.role == "user" and m.content), ""
        )
        lowered = last_user.strip().lower()
        if lowered in ("help", "help?", "?"):
            return HELP_TEXT
        if lowered in ("hi", "hello", "hey"):
            return f"Hello! I'm **{self.agent_label}** in offline demo mode — no NVIDIA API key is configured yet, so I can't think for real. Send `help` to see how to enable the model."
        return HELP_TEXT

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: Optional[list[dict]] = None,
        tool_choice: Optional[str] = None,
        temperature: float = 0.4,
        max_tokens: int = 2048,
    ) -> AsyncIterator[Any]:
        reply = self._reply(messages)
        started = time.monotonic()
        # Stream in small pieces so the UI demonstrates real streaming.
        for i in range(0, len(reply), 24):
            await asyncio.sleep(0.008)
            yield TextChunk(reply[i : i + 24])
        yield StreamDone(
            content=reply, tool_calls=[], usage=Usage(), model=self.model,
            finish_reason="stop", latency_ms=(time.monotonic() - started) * 1000,
        )

    async def check(self) -> dict[str, Any]:
        return {"provider": self.name, "model": self.model, "ok": False,
                "detail": "offline demo mode — no API key configured"}
