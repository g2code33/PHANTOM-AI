"""Model routing — pick the right model per task instead of always using the
most expensive one.

For each task the router considers: required intelligence, reasoning
complexity, coding need, speed, cost, context length, reliability. Tasks are
classified into a bucket (fast / default / strong); each brain can map buckets
to model names via settings (model.router.<brain>), overridable without
rewriting the application.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from ..config import DEFAULT_MODELS

_COMPLEXITY_WORDS = (
    "analy", "debug", "architect", "design", "plan", "investigat", "complex",
    "refactor", "optimiz", "migrat", "security", "architecture", "strategy",
    "multi", "explain why", "root cause", "build", "implement", "test", "review",
)
_FAST_WORDS = (
    "summar", "short", "quick", "hi", "hello", "who", "what time", "list",
    "remember", "forget", "search", "status",
)


class ModelRouter:
    def __init__(self, settings: Any) -> None:
        self.settings = settings

    async def choose(self, agent_id: str, task_text: str, has_tools: bool = True) -> str:
        cfg = await self.settings.get("model.router", agent_id, {}) or {}
        default = cfg.get("default") or DEFAULT_MODELS.get(agent_id) or DEFAULT_MODELS["phantom"]
        bucket = self._classify(task_text)
        return cfg.get(bucket) or default

    async def classify(self, task_text: str) -> str:
        return self._classify(task_text)

    @staticmethod
    def _classify(task_text: str) -> str:
        text = (task_text or "").lower()
        length = len(text.split())
        if any(w in text for w in _COMPLEXITY_WORDS) or length > 80:
            return "strong"
        if any(w in text for w in _FAST_WORDS) and length < 20:
            return "fast"
        return "default"
