# PHANTOM + CODED 👻💻

**A personal AI computer operator.** Two independent AI entities — **Phantom** (general-purpose
strategist) and **Coded** (technical specialist) — living in one shared agent framework inside a
single local application. Both are real PC operators (files, applications, terminal, web,
system), not chatbot demos. They have separate identities, system prompts, NVIDIA API keys,
conversations, long-term memories and tool permissions — and they can delegate tasks to each other.

```
USER → PHANTOM → delegate() → CODED → tools → PC
               ⇄ structured results            │
               └─────────── answer ────────────┘
```

## Quick start

```bash
# 1. install (creates .venv)
./scripts/run.sh           # or: python3 -m venv .venv && .venv/bin/pip install -e ".[gui,dev]"

# 2. configure NVIDIA keys (either is fine)
export PHANTOM_NVIDIA_API_KEY="nvapi-..."      # Phantom's own key
export CODED_NVIDIA_API_KEY="nvapi-..."        # Coded's own key
#    …or set them later in the UI: Settings → API Keys (stored locally, chmod 600)

# 3. run
./scripts/run.sh           # → http://localhost:8000
```

Without keys the app runs in an honest **offline mode** — everything works except the language
model (the UI explains how to enable it).

## Verification

```bash
.venv/bin/python scripts/verify_tiers.py        # runs every tier's test suite + a live
                                                # end-to-end smoke (real HTTP + WS + tools)
.venv/bin/python -m pytest tests/ -q           # the full suite (62 tests)
```

`scripts/verify_tiers.py` prints a per-tier PASS/FAIL table (see `docs/TIERS.md`).

## What's inside

| Area | Details |
|---|---|
| **Agents** | Shared `Agent` core; Phantom & Coded = two identities (system prompt, model, key, memory + conversation namespaces). Streaming, retries, timeouts, cancellation, text-protocol fallback for models without native tool calling. |
| **Model provider** | `providers/` abstraction; **NVIDIA NIM-compatible** client (SSE streaming, usage, retries on 429/5xx, structured errors). Future providers plug in without touching the agent core. |
| **Tools (55)** | files (read/write/search/grep/archive…), terminal (bash/PowerShell with capture), processes & apps, system info, web (search/page/HTTP with SSRF guard), GUI (mouse/keyboard/windows/screenshots — real, honest when headless), clipboard, browser automation (Playwright), memory tools, delegation tools, notifications. |
| **Permissions** | `read_only → safe_action → confirm_required → high_risk → blocked`; per-agent overrides, path rules, command classifier (dangerous commands blocked, mutating commands confirmed), secret-leak guard. Sits **between** the AI's decision and execution. |
| **Confirmations** | What / why / affected / exact action / risk, then explicit per-action approve or deny. Never blanket approval. |
| **Kill switch** | Prominent red button. Stops heartbeat, cancels tasks and in-flight runs; app stays usable; disarm anytime. |
| **Memory** | Short-term window + rolling summaries; long-term facts/preferences/projects per agent + shared; full searchable conversation archive (FTS5). Memory is **data, never instructions**. |
| **Delegation** | Phantom ⇄ Coded with task IDs, timeouts, permission handover, loop guard, concurrency caps. |
| **Voice** | Abstraction layer (STT/TTS providers); browser push-to-talk + speech synthesis ship by default; server providers plug in. Text is the foundation. |
| **Heartbeat** | Schedules (`every 30m`, `hourly`, `daily at 09:00`, cron), quiet hours, catch-up after restart, dismissible notifications, kill-switch integration. |
| **Tasks** | Tracked background work: ids, status, logs, cancellation, concurrency/runtime limits. |
| **Observability** | Full audit log (tool calls, latency, tokens, confirmations, delegations, API errors) with a UI. |
| **UI** | Local web app: agent switcher, conversations, live streaming chat, tool cards, confirmation dialogs, memory center, task manager, audit viewer, permissions editor, settings, tool lab, notifications, kill switch, provider status. |

## Layout

```
phantom_ai/
├── agents/        identities.py · core.py (the loop) · delegation.py
├── providers/     base.py · nvidia.py · mock.py (offline mode)
├── tools/         registry + real implementations
├── permissions/   levels · policy · confirmation · command validation
├── memory/        retrieval & context packaging
├── storage/       SQLite (conversations+FTS, memories, audit, tasks, …)
├── heartbeat/     scheduler
├── tasks/         task manager
├── voice/         STT/TTS abstractions
├── security/      secrets store · redaction · injection defense
├── core/          kill switch · event bus
└── api/           FastAPI app + WebSocket + static UI
```

See `docs/ARCHITECTURE.md` (design), `docs/TIERS.md` (build order + verification),
`docs/USER_GUIDE.md` (how to use it).
