"""Application configuration.

Design rules:
- API keys are NEVER hard-coded. They come from environment variables first,
  then from a chmod-600 local secrets file managed through the Settings UI.
- Agent settings are stored per-agent (Phantom vs Coded) so both entities are
  fully independent (model, permissions, personality knobs, ...).
- Sensitive values are masked before they can reach logs or the UI.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("PHAI_DATA_DIR", ROOT_DIR / "data"))
SCREENSHOT_DIR = DATA_DIR / "screenshots"
DB_PATH = Path(os.environ.get("PHAI_DB_PATH", DATA_DIR / "phantom_coded.db"))
SECRETS_PATH = Path(os.environ.get("PHAI_SECRETS_PATH", DATA_DIR / "secrets.json"))
UI_DIR = ROOT_DIR / "ui"

# ---------------------------------------------------------------------------
# Env keys for NVIDIA credentials (per agent)
# ---------------------------------------------------------------------------

KEY_ENV = {
    "phantom": "PHANTOM_NVIDIA_API_KEY",
    "coded": "CODED_NVIDIA_API_KEY",
    "evolution": "EVOLUTION_NVIDIA_API_KEY",
}

# Default NVIDIA NIM (OpenAI-compatible) endpoint
NVIDIA_BASE_URL = os.environ.get("PHAI_NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
# Per-agent overrides (used by tests to point at a local mock)
BASE_URL_ENV = {
    "phantom": "PHANTOM_NVIDIA_BASE_URL",
    "coded": "CODED_NVIDIA_BASE_URL",
    "evolution": "EVOLUTION_NVIDIA_BASE_URL",
}

DEFAULT_MODELS = {
    "phantom": os.environ.get("PHANTOM_DEFAULT_MODEL", "nvidia/llama-3.3-70b-instruct"),
    "coded": os.environ.get("CODED_DEFAULT_MODEL", "nvidia/llama-3.3-70b-instruct"),
    "evolution": os.environ.get("EVOLUTION_DEFAULT_MODEL", "nvidia/llama-3.3-70b-instruct"),
}

AGENTS = ("phantom", "coded")


class SecretRedactor:
    """Masks API keys / bearer tokens in arbitrary text (logs, audit, context)."""

    _patterns = (
        (r"(?i)\b(nvapi-|sk-|nvidia-)[a-z0-9_\-]{8,}\b", r"\1****"),
        (r"(?i)(bearer\s+)[a-z0-9._\-]{8,}", r"\1****"),
        (r'(?i)("(?:api[_-]?key|token|secret|authorization)"\s*[:=]\s*")[^"]+', r"\1****"),
    )

    @classmethod
    def redact(cls, text: str) -> str:
        import re

        if not text:
            return text
        for pattern, repl in cls._patterns:
            text = re.sub(pattern, repl, text)
        return text


def mask_key(value: Optional[str]) -> str:
    """Mask a key for display: show last 4 chars only."""
    if not value:
        return ""
    if len(value) <= 8:
        return "****"
    return f"****{value[-4:]}"


# ---------------------------------------------------------------------------
# Secrets store
# ---------------------------------------------------------------------------


class SecretsStore:
    """Stores API keys outside the conversation DB.

    Priority: environment variable > local secrets file.
    The secrets file is created with 0600 permissions and never logged.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or SECRETS_PATH

    def _load_file(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_file(self, data: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)
        os.chmod(self.path, 0o600)

    def get(self, key: str) -> Optional[str]:
        env_val = os.environ.get(key)
        if env_val:
            return env_val
        return self._load_file().get(key)

    def set(self, key: str, value: str) -> None:
        value = value.strip()
        if not value:
            return
        data = self._load_file()
        data[key] = value
        self._save_file(data)

    def delete(self, key: str) -> None:
        data = self._load_file()
        if key in data:
            del data[key]
            self._save_file(data)

    def has(self, key: str) -> bool:
        return bool(self.get(key))

    def masked(self, key: str) -> str:
        return mask_key(self.get(key))


# ---------------------------------------------------------------------------
# Settings store (per-agent + global)
# ---------------------------------------------------------------------------


class SettingsStore:
    """Persistent key/value settings, scoped per agent or global ('*')."""

    def __init__(self, db: Any) -> None:
        self.db = db

    async def get(self, key: str, agent: str = "*", default: Any = None) -> Any:
        row = await self.db.fetchone(
            "SELECT value FROM settings WHERE agent = ? AND key = ?", (agent, key)
        )
        if row is None and agent != "*":
            row = await self.db.fetchone(
                "SELECT value FROM settings WHERE agent = ? AND key = ?", ("*", key)
            )
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return row["value"]

    async def set(self, key: str, value: Any, agent: str = "*") -> None:
        await self.db.execute(
            "INSERT INTO settings (agent, key, value, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(agent, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (agent, key, json.dumps(value), now_iso()),
        )

    async def delete(self, key: str, agent: str = "*") -> None:
        await self.db.execute("DELETE FROM settings WHERE agent = ? AND key = ?", (agent, key))

    async def all(self) -> dict[str, dict[str, Any]]:
        """Return {agent: {key: value}} for the UI."""
        rows = await self.db.fetchall("SELECT agent, key, value FROM settings")
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            try:
                out.setdefault(r["agent"], {})[r["key"]] = json.loads(r["value"])
            except (json.JSONDecodeError, TypeError):
                out.setdefault(r["agent"], {})[r["key"]] = r["value"]
        return out


def now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
