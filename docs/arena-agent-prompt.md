# Arena Agent Prompt — PHANTOM + CODED handoff

> Repo: **g2code33/PHANTOM-AI** · Clone: `/home/user/PHANTOM-AI` · Branch: work on
> `arena/<session-branch>`; activate releases by pushing to `main`.

## What this project is

**PHANTOM + CODED** is a personal AI computer operator — a local desktop application that runs
two independent AI entities (👻 Phantom, general-purpose strategist; 💻 Coded, technical
specialist) on one shared agent framework:

- **Backend**: Python 3.10+ / asyncio / FastAPI. Everything lives under `phantom_ai/`
  (agents, providers, tools, permissions, memory, storage, heartbeat, tasks, kill switch).
  Run: `.venv/bin/python -m phantom_ai.main` → serves the UI + REST + WebSocket.
- **Frontend**: static, dependency-free web UI in `ui/` (`index.html`, `app.js`,
  `styles.css`). Talks to `/api/*` and `/ws`.
- **Models**: NVIDIA NIM-compatible API, per-agent keys
  (`PHANTOM_NVIDIA_API_KEY` / `CODED_NVIDIA_API_KEY`) — never hard-coded.
- **Storage**: single SQLite file (`data/phantom_coded.db`) with FTS5 search.
- **Tests**: `tests/` (pytest, 62 tests) + `scripts/verify_tiers.py` tier harness.

## The desktop/mobile packaging (already scaffolded in this repo)

Electron (Windows `.exe` + Linux `.deb`/`.AppImage`, self-update via `latest.yml`) and
Capacitor 8 (Android `.apk`) wrap the web UI; GitHub Actions builds all three on every push
to `main` and publishes them to a GitHub Release. See
`docs/arena-agent-prompt-build-structure.md` for the full spec, and
`.github/workflows/build-desktop.yml` (editable copy: `docs/workflow-build-desktop.yml`).

## Typical tasks

- **Desktop shell**: `electron/main.ts` spawns the Python backend with
  `--port-file`, waits for the port, opens `http://127.0.0.1:<port>`.
  `package.json` holds the `electron-builder` config (`appId`, `productName`, `files`,
  `asarUnpack`, nsis/deb/AppImage targets).
- **Android**: `capacitor.config.ts` (`webDir: "dist"`), committed `android/` project,
  signing via `ANDROID_KEYSTORE_*` secrets with debug-key fallback.
- **CI**: `.github/workflows/build-desktop.yml` — `build` job (non-main, upload artifacts),
  `release` job (main, `--publish always` with `GH_TOKEN` + `EP_GH_IGNORE_TIME: 'true'`),
  ubuntu leg uploads `phantom-coded-<ver>.apk` to the same release.
- **Versioning**: bump `package.json` version → push `main` → new release.
  `npm run release:patch` does version bump + commit + push.

## Rules

1. Never hard-code or log API keys/secrets. Keys go in Settings (chmod-600 secrets file) or env.
2. Keep the Python backend untouched by frontend changes unless required; run
   `.venv/bin/python -m pytest tests/ -q` before shipping.
3. If your token lacks Workflows permission, keep the workflow at
   `docs/workflow-build-desktop.yml` and have the user copy it into `.github/workflows/`.
4. Don't fake verification: if Electron/gradle binaries can't download in the sandbox,
   state it and verify everything that can be verified locally.
