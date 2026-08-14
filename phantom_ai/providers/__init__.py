"""Provider factory: pick the right ModelProvider for an agent identity.

- NVIDIA NIM (default, when the agent's key is present).
- Offline mode (honest placeholder) when no key is configured.
- Any OpenAI-compatible base URL can be substituted via
  PHANTOM_NVIDIA_BASE_URL / CODED_NVIDIA_BASE_URL (used by tests and by
  self-hosted NIM servers).

Future providers (Anthropic, OpenAI, ...) plug in here without touching the
agent core.
"""

from __future__ import annotations

from typing import Optional

from ..config import BASE_URL_ENV, DEFAULT_MODELS, KEY_ENV, SecretsStore
from .base import ModelProvider
from .mock import OfflineProvider
from .nvidia import NVIDIAProvider


def create_provider(agent_id: str, secrets: Optional[SecretsStore] = None) -> ModelProvider:
    secrets = secrets or SecretsStore()
    api_key = secrets.get(KEY_ENV[agent_id])
    model = DEFAULT_MODELS[agent_id]
    base_url = _base_url_for(agent_id)

    if not api_key:
        return OfflineProvider(model="offline", agent_label="Phantom" if agent_id == "phantom" else "Coded")

    return NVIDIAProvider(api_key=api_key, model=model, base_url=base_url)


def _base_url_for(agent_id: str) -> str:
    from ..config import NVIDIA_BASE_URL

    env_name = BASE_URL_ENV[agent_id]
    import os

    return os.environ.get(env_name, NVIDIA_BASE_URL)


def build_provider(agent_id: str, api_key: str, model: str,
                   base_url: Optional[str] = None) -> ModelProvider:
    """Explicit construction (used when settings change without restart)."""
    if not api_key:
        return OfflineProvider(model="offline", agent_label=agent_id.capitalize())
    return NVIDIAProvider(api_key=api_key, model=model,
                          base_url=base_url or _base_url_for(agent_id))
