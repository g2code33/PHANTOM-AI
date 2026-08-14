"""Emergency kill switch.

Engaging it immediately:
- refuses new agent runs / tool executions
- cancels in-flight agent runs (they poll is_engaged() each step)
- stops the heartbeat scheduler
- marks pending tasks as cancelled

The user can still browse the app, logs, settings — and disarm the switch.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class KillSwitchState:
    engaged: bool = False
    reason: str = ""
    engaged_at: str | None = None


class KillSwitch:
    def __init__(self, audit: object | None = None) -> None:
        self.state = KillSwitchState()
        self.audit = audit
        self._changed = asyncio.Event()

    def is_engaged(self) -> bool:
        return self.state.engaged

    async def engage(self, reason: str = "manual") -> None:
        if self.state.engaged:
            return
        self.state = KillSwitchState(
            engaged=True,
            reason=reason,
            engaged_at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        )
        self._changed.set()
        self._changed = asyncio.Event()
        if self.audit is not None:
            await self.audit.record("*", "killswitch.engaged", {"reason": reason})

    async def disengage(self) -> None:
        if not self.state.engaged:
            return
        self.state = KillSwitchState()
        self._changed.set()
        self._changed = asyncio.Event()
        if self.audit is not None:
            await self.audit.record("*", "killswitch.disengaged", {})

    def changed_event(self) -> asyncio.Event:
        return self._changed

    def to_dict(self) -> dict:
        return {"engaged": self.state.engaged, "reason": self.state.reason,
                "engaged_at": self.state.engaged_at}
