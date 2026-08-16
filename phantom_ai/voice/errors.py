"""Shared voice-engine error types. Every provider raises VoiceProviderError
with a machine-readable `kind` (for health/backoff) and a `friendly` message
(safe to show in the UI — never contains keys or raw stack traces)."""

from __future__ import annotations


class VoiceProviderError(Exception):
    kinds = ("invalid_key", "rate_limited", "no_credits", "timeout", "network",
             "no_speech", "not_installed", "provider_error", "canceled")

    def __init__(self, kind: str, friendly: str, provider: str = "") -> None:
        if kind not in self.kinds:
            kind = "provider_error"
        super().__init__(friendly)
        self.kind = kind
        self.friendly = friendly
        self.provider = provider

    def __str__(self) -> str:  # friendly text wins over raw args
        return self.friendly


def classify_http_error(provider: str, status: int, text: str = "") -> VoiceProviderError:
    """Map an HTTP status to a friendly, categorized voice error."""
    snippet = (text or "")[:120].strip()
    if status in (401, 403):
        return VoiceProviderError(
            "invalid_key",
            f"{provider} rejected the API key (unauthorized) — check it in Settings",
            provider)
    if status == 402:
        return VoiceProviderError(
            "no_credits", f"{provider} is out of credits — top up or switch provider", provider)
    if status == 429:
        return VoiceProviderError(
            "rate_limited", f"{provider} is rate-limited — switching to the next provider", provider)
    if status >= 500:
        return VoiceProviderError(
            "provider_error", f"{provider} had a server error — switching to the next provider",
            provider)
    if snippet:
        return VoiceProviderError(
            "provider_error", f"{provider} failed: {snippet}", provider)
    return VoiceProviderError(
        "provider_error", f"{provider} failed (HTTP {status}) — switching provider", provider)
