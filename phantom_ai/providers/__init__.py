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
    # Evolution (and any dynamically registered brain) falls back to Phantom's
    # key when it has no key of its own; models default to Phantom's defaults.
    api_key = secrets.get(KEY_ENV.get(agent_id, "")) or secrets.get(KEY_ENV["phantom"])
    model = DEFAULT_MODELS.get(agent_id, DEFAULT_MODELS["phantom"])
    base_url = _base_url_for(agent_id)

    if not api_key:
        label = {"phantom": "Phantom", "coded": "Coded", "evolution": "Evolution"}.get(
            agent_id, agent_id.capitalize())
        return OfflineProvider(model="offline", agent_label=label)

    return NVIDIAProvider(api_key=api_key, model=model, base_url=base_url)


def _base_url_for(agent_id: str) -> str:
    from ..config import NVIDIA_BASE_URL

    import os

    env_name = BASE_URL_ENV.get(agent_id)
    if not env_name:
        return NVIDIA_BASE_URL
    return os.environ.get(env_name, NVIDIA_BASE_URL)


def build_provider(agent_id: str, api_key: str, model: str,
                   base_url: Optional[str] = None) -> ModelProvider:
    """Explicit construction (used when settings change without restart)."""
    if not api_key:
        return OfflineProvider(model="offline", agent_label=agent_id.capitalize())
    return NVIDIAProvider(api_key=api_key, model=model,
                          base_url=base_url or _base_url_for(agent_id))
