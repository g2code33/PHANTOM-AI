# Phantom 👻

**A personal AI computer companion — voice-first.** Phantom lives inside your computer: it
listens, understands, reasons, speaks, remembers, plans, acts (with permission), monitors
tasks and can speak to you proactively when enabled. The interface is an immersive presence
environment, not a chat dashboard.

Inside the platform: **Coded** (💻 coding/development persona), **Health** (🩺 private wellness,
encrypted vault), **Evolution** (🧬 system intelligence) and a full catalogue of **specialist
brains** (planner, tutor, research, security, critic, …) — all separate entities on one shared
agent framework, with their own identities, configurations, memories and tool permissions,
able to delegate to each other.

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
./scripts/run.sh           # → http://localhost:8000  (launches the Phantom environment)
```

The first launch walks you through a short setup (name, voice, mode). Without keys the app runs
in an honest **local mode** — all local capabilities work; external AI/voice are optional,
configurable services (NVIDIA keys, Deepgram voice). Voice defaults to the browser's built-in
streaming speech recognition + synthesis (works offline); Deepgram adds low-latency streaming
voice when you configure `DEEPGRAM_API_KEY` (server-side; the UI only ever receives a
short-lived token).

## Verification

```bash
.venv/bin/python scripts/verify_tiers.py        # runs every tier's test suite + a live
                                                # end-to-end smoke (real HTTP + WS + tools)
.venv/bin/python -m pytest tests/ -q           # the full suite (105 tests)
npm run typecheck && npm run web:build          # Electron main (tsc) + web bundle
```

`scripts/verify_tiers.py` prints a per-tier PASS/FAIL table (see `docs/TIERS.md`).

## Desktop & mobile packaging (release on push to `main`)

The same build-and-release structure as `g2code33/CLINICAL-RX-`:

- **Windows installer** — `Phantom-Setup-<ver>.exe` (+ `latest.yml`) via electron-builder/nsis.
- **Linux** — `phantom_<ver>_amd64.deb` + `Phantom-<ver>.AppImage` (+ `latest-linux.yml`).
- **Android** — `phantom-<ver>.apk` via Capacitor 8 (`capacitor.config.ts`, committed
  `android/` project, icons/splash generated from `resources/icon.png`).
- **CI** — `.github/workflows/build-desktop.yml` (editable copy:
  `docs/workflow-build-desktop.yml`): push to `main` → matrix (ubuntu+windows) builds and
  auto-publishes everything to a GitHub Release with `GH_TOKEN` + `EP_GH_IGNORE_TIME`.
  Android release signing uses `ANDROID_KEYSTORE_BASE64/PASSWORD/ALIAS/KEY_PASSWORD`
  secrets with automatic fallback to the debug key so builds never break.
- **Versioning** — bump `version` in `package.json` → push → new release.
  `npm run release:patch` does bump + commit + push in one step.

The Electron shell (`electron/main.ts`) spawns the backend with `--port-file` and loads the UI
from `http://127.0.0.1:<port>` (backend stays local-only). The Android app connects to a
backend you configure under Settings → Backend URL.

Since **v0.1.2** the desktop installers bundle the Python backend as a standalone
PyInstaller executable (`resources/backend/phantom-backend[.exe]`, built in CI) — no system
Python, uvicorn or pip installs needed on the user's machine. The Electron shell prefers the
bundled backend, then `PHAI_BACKEND`, then a system Python (dev fallback). User data (DB,
secrets, screenshots) lives in the app's userData dir (`~/.local/share/phantom-coded` on
Linux, `%LOCALAPPDATA%\phantom-coded` on Windows) when packaged. Linux installs also fix the
Chromium SUID sandbox via a deb post-install hook, with an automatic `--no-sandbox` fallback.

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
| **Evolution & System Intelligence** | Capability registry + dynamic brain creation; 🧬 Evolution brain (analysis, self-audit, proposals); graph engine with relationship discovery; improvement loop engine (bounded, strategy-switching, escalation, rollback); critic/verification; model routing; config snapshots & rollback; Health & Intelligence dashboard; in-app Update button (electron-updater, works when the version is bumped). |
| **Health Brain** | 🩺 Independent specialist: encrypted Health Memory Vault (Fernet), daily health management & today overview, medication/appointment/goal tracking, measurement trends (never diagnoses), symptom tracking, red-flag safety layer (emergency/warning, safety over conversation), privacy controls (no sharing by default, non-sensitive briefing, audit), personalized routines → schedules, study cooperation, Health Center UI, disable anytime. |
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
