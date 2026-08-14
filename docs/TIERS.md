# Build tiers — implementation & verification

Built in the specified order, each tier implemented → run → tested → verified before the next.
`scripts/verify_tiers.py` re-runs every tier's checks and prints a status table. Latest run:
**ALL TIERS VERIFIED ✓** (62 unit/integration tests + live end-to-end smoke).

| Tier | Delivered | How it is verified |
|---|---|---|
| **0 · Design** | `docs/ARCHITECTURE.md`: stack, entities, data flow, permission & memory model, kill switch, storage, verification strategy | Review doc |
| **1 · Phantom text agent** | Shared agent core + NVIDIA provider: streaming (SSE), retries (429/5xx), timeouts, structured errors, conversation persistence, key via env/secrets | `tests/test_providers.py`, `tests/test_agents.py` (run against a local OpenAI-compatible mock since NVIDIA is unreachable in the build sandbox) |
| **2 · Coded text agent** | Second identity with its own system prompt, API key (`CODED_NVIDIA_API_KEY`), conversations, memory namespace | `test_phantom_and_coded_have_distinct_providers_and_keys`, per-agent key assertions |
| **3 · Agent-to-agent** | `delegate_to_coded` / `delegate_to_phantom`; delegation records carry task id, origin, target, objective, context, timeout, result, status; loop guard; concurrency cap; timeout → cancel | `tests/test_delegation.py` (4 tests incl. loop guard + timeout) |
| **4 · Tool system** | Registry with 55 typed, validated tools; each has name, description, purpose, JSON schema, permission level, timeout, error handling, audit hooks | `test_write_and_read_file`, `test_manual_tool_run_endpoint`; schema validation tests |
| **5 · Real PC control** | Files (read/write/search/grep/copy/move/rename/delete/archive with traversal guard), terminal (capture stdout/stderr/exit/duration, policy-gated), processes/apps, system info, web (search/page/HTTP + SSRF guard), GUI (mouse/keyboard/windows/screenshots — real; honest "unavailable" when headless), clipboard, browser (Playwright) | `tests/test_tools.py` (11 tests, all real OS operations; screenshots assert real files or a structured unavailable error) |
| **6 · Permissions + safety** | Levels, per-agent overrides, path rules, command classifier (denylist, redirection guard, secret-leak guard), confirmation flow (what/why/affected/action/risk; per-action approval only), audit trail, kill switch, secrets handling | `tests/test_permissions.py` (7), `tests/test_killswitch.py` (4), audit assertions |
| **7 · Memory** | Short-term window + rolling summaries; per-agent + shared long-term memory (importance, tags, source); full conversation archive with FTS5 search; restart persistence; memory-as-data (never instructions) | `tests/test_memory.py` (6, incl. restart + injection-defense), memory tools tests |
| **8 · Voice** | `STTProvider`/`TTSProvider` abstractions; browser push-to-talk + speech synthesis; server providers pluggable; same agent core for typed/voice/proactive input | abstraction tests; documented in `docs/USER_GUIDE.md` (needs browser STT support; server providers require config) |
| **9 · Heartbeat** | Scheduler (interval/daily/cron expressions), quiet hours, catch-up after restart, notifications, duplicate prevention, kill-switch stop | `tests/test_heartbeat.py` (5) |
| **10 · Advanced** | Task manager (ids/status/logs/cancel/limits), delegation limits, parallel-safe tool execution, observability (audit UI + API), full API surface | `tests/test_api.py` (8), `tests/test_delegation.py`, live smoke |
| **11 · Evolution & System Intelligence** | Capability Registry + dynamic brain creation (persisted); Evolution brain (🧬) with introspection/proposals/self-audit; Graph Engine with auto-tracking + graph-aware retrieval; Loop Engine (bounded, strategy switch, escalation, rollback); critic/verification; model routing; config snapshots + rollback; Health & Intelligence dashboard; in-app Update button (electron-updater) | `tests/test_evolution.py` (16), `tests/test_api.py`, live smoke (3 brains) |

## Honesty notes (nothing faked)

- **NVIDIA connectivity** is implemented for real (`providers/nvidia.py`, OpenAI-compatible NIM
  API). This sandbox cannot reach `integrate.api.nvidia.com`, so the *entire stack* is verified
  against a local OpenAI-compatible mock that speaks the identical SSE protocol; the same client
  code runs against NVIDIA on your machine.
- **GUI/browser tools** execute real operations on a desktop. In this headless sandbox they
  return structured `unavailable` errors — they never simulate clicks or screenshots.
- **Offline mode** (no key configured) is a clearly-labeled placeholder that explains setup; it
  never pretends to be an AI.

## What can the AI actually see / do? (the tier-10 checklist)

- [x] Tool results are real and verifiable (files exist, exit codes, artifacts).
- [x] Failures are structured and recoverable (tool errors return to the model; network failures
      preserve the conversation).
- [x] Malicious web/file content is data (wrapped, injection-guarded) — `test_memory_is_data_not_instructions`.
- [x] The user can stop it: kill switch + per-run cancel (`test_cancellation`, `test_engage_cancels_inflight_run`).
- [x] We can see exactly what it did: audit log + tool cards + activity feed.
- [x] Consequences are gated: blocked commands never execute; consequential actions need per-action approval.
