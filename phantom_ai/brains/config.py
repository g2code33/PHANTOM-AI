"""Per-brain provider configuration resolution.

Every brain can carry its OWN API key, model and provider base URL so each
specialist can be routed to the most efficient model/API for its job — while
sharing the platform provider by default.

Precedence (highest first):
  API key:   env BRAIN_<ID>_NVIDIA_API_KEY  >  secrets brain.<id>.api_key
             > definition key_env  >  phantom's key
  model:     settings brain.<id>.model  >  definition model  >  phantom default
  base URL:  env BRAIN_<ID>_NVIDIA_BASE_URL  >  settings brain.<id>.base_url
             >  platform default

All keys are stored via the chmod-600 secrets store (never in the DB, never in
logs, masked in the API/UI).
"""

from __future__ import annotations

import os
from typing import Any, Optional

from ..config import DEFAULT_MODELS, KEY_ENV, NVIDIA_BASE_URL, SecretsStore


def _env_key_name(brain_id: str) -> str:
    return f"BRAIN_{brain_id.upper().replace('-', '_')}_NVIDIA_API_KEY"


def _env_base_name(brain_id: str) -> str:
    return f"BRAIN_{brain_id.upper().replace('-', '_')}_NVIDIA_BASE_URL"


async def resolve_brain_config(brain_id: str, definition: dict[str, Any],
                               secrets: SecretsStore, settings: Any) -> dict[str, str]:
    """Return {'api_key', 'model', 'base_url'} for a brain (all non-empty)."""
    # ---- api key ----
    api_key = os.environ.get(_env_key_name(brain_id), "").strip()
    if not api_key:
        api_key = secrets.get(f"brain.{brain_id}.api_key") or ""
    if not api_key:
        key_env = definition.get("key_env") or KEY_ENV.get(brain_id, "")
        api_key = secrets.get(key_env) or ""
    if not api_key:
        api_key = secrets.get(KEY_ENV["phantom"]) or ""

    # ---- model ----
    model = ""
    try:
        model = await settings.get(f"brain.{brain_id}.model", "*", "") or \
                definition.get("model") or ""
    except Exception:  # noqa: BLE001
        model = definition.get("model") or ""
    if not model:
        model = DEFAULT_MODELS.get(brain_id, DEFAULT_MODELS["phantom"])

    # ---- base url ----
    base_url = os.environ.get(_env_base_name(brain_id), "").strip()
    if not base_url:
        try:
            base_url = await settings.get(f"brain.{brain_id}.base_url", "*", "") or ""
        except Exception:  # noqa: BLE001
            base_url = ""
    if not base_url:
        # fall back to the platform's per-agent env (PHANTOM_NVIDIA_BASE_URL …)
        from ..providers import _base_url_for

        base_url = _base_url_for(brain_id)
    if not base_url:
        base_url = NVIDIA_BASE_URL

    base_url = _normalize_base_url(base_url)
    return {"api_key": api_key, "model": model, "base_url": base_url}


def _normalize_base_url(base_url: str) -> str:
    """Sanitize a (possibly user-stored) OpenAI-compatible base URL.

    Fixes the failure that produced 'NVIDIA request failed (HTTP 404):
    404 page not found' — a base_url without the /v1 path (or with the full
    /chat/completions endpoint stored) makes the provider hit a web-server
    root and 404. Normalizing here heals it at resolve time."""
    import re

    url = (base_url or "").strip()
    if not url:
        return NVIDIA_BASE_URL
    # strip a trailing full endpoint if the user stored it
    url = re.sub(r"/chat/completions/?$", "", url)
    url = url.rstrip("/")
    # ensure a scheme (OpenAI-compatible endpoints are https)
    if "://" not in url:
        url = "https://" + url
    # the known NVIDIA NIM host is only correct WITH /v1
    if url.rstrip("/").endswith("integrate.api.nvidia.com"):
        url = url + "/v1"
    return url


def brain_key_envs(brain_id: str) -> dict[str, str]:
    return {"api_key_env": _env_key_name(brain_id),
            "base_url_env": _env_base_name(brain_id)}
