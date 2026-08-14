"""Task manager: tracked background work with ids, status, logs, limits and
cancellation. Prevents uncontrolled spawning (configurable concurrency caps)."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Optional

from ..storage.ops import TaskStore


class TaskManager:
    def __init__(self, store: TaskStore, settings: Any, events: Any, killswitch: Any) -> None:
        self.store = store
        self.settings = settings
        self.events = events
        self.killswitch = killswitch
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._tasks: dict[str, asyncio.Task] = {}

    async def _sem(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            limit = int(await self.settings.get("task.max_concurrent", "*", 4))
            self._semaphore = asyncio.Semaphore(max(1, limit))
        return self._semaphore

    async def launch(self, agent: str, name: str, kind: str,
                     coro_factory: Callable[[str], Awaitable[Any]]) -> dict:
        """Create a tracked task and run it in the background under the cap."""
        row = await self.store.create(agent, name, kind)
        task_id = row["id"]
        sem = await self._sem()

        async def runner():
            max_runtime = int(await self.settings.get("task.max_runtime", "*", 3600))
            async with sem:
                await self.store.set_status(task_id, "running")
                await self.store.append_log(task_id, "started")
                await self.events.publish("task.update", {"task": await self.store.get(task_id),
                                                          "agent": agent})
                started = time.monotonic()
                try:
                    result = await asyncio.wait_for(coro_factory(task_id), timeout=max_runtime)
                    await self.store.append_log(task_id, f"finished in {(time.monotonic() - started):.1f}s")
                    await self.store.set_status(task_id, "completed")
                    return result
                except asyncio.TimeoutError:
                    await self.store.append_log(task_id, f"timed out after {max_runtime}s")
                    await self.store.set_status(task_id, "failed",
                                                error=f"exceeded max runtime {max_runtime}s")
                except asyncio.CancelledError:
                    await self.store.append_log(task_id, "cancelled")
                    await self.store.set_status(task_id, "cancelled")
                except Exception as exc:  # noqa: BLE001
                    await self.store.append_log(task_id, f"error: {exc}")
                    await self.store.set_status(task_id, "failed", error=str(exc)[:500])
                finally:
                    await self.events.publish("task.update", {"task": await self.store.get(task_id),
                                                              "agent": agent})

        task = asyncio.ensure_future(runner())
        self._tasks[task_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(task_id, None))
        return await self.store.get(task_id)

    async def cancel(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if task is None:
            return False
        task.cancel()
        return True

    async def cancel_all(self) -> int:
        count = 0
        for task_id, task in list(self._tasks.items()):
            if not task.done():
                task.cancel()
                count += 1
        return count

    def active_count(self) -> int:
        return sum(1 for t in self._tasks.values() if not t.done())

    async def get(self, task_id: str) -> Optional[dict]:
        return await self.store.get(task_id)
