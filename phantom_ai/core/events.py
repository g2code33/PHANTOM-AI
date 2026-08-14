"""Event bus: pushes real-time events (streamed chunks, tool activity,
confirmations, task updates, notifications) to every connected UI.

A subscriber is an asyncio.Queue of JSON-serializable dicts. WebSocket
connections subscribe/unsubscribe through the API layer.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

MAX_QUEUE = 500


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, asyncio.Queue] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, subscriber_id: str) -> asyncio.Queue:
        async with self._lock:
            q: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE)
            self._subscribers[subscriber_id] = q
            return q

    async def unsubscribe(self, subscriber_id: str) -> None:
        async with self._lock:
            self._subscribers.pop(subscriber_id, None)

    async def publish(self, event: str, data: Optional[dict[str, Any]] = None,
                      agent: str = "", run_id: str = "") -> None:
        payload = {"event": event, "data": data or {}, "agent": agent, "run_id": run_id}
        async with self._lock:
            subs = list(self._subscribers.items())
        for sid, q in subs:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # Drop oldest event for slow subscribers; never block the agent.
                try:
                    q.get_nowait()
                    q.put_nowait(payload)
                except asyncio.QueueEmpty:
                    pass

    async def publish_run(self, run_id: str, event: str, data: dict[str, Any],
                          agent: str = "") -> None:
        await self.publish(event, data, agent=agent, run_id=run_id)

    def subscriber_count(self) -> int:
        return len(self._subscribers)
