# PHANTOM + CODED — Architecture (TIER 0 · Design)

A personal AI computer operator: **two independent AI entities** — **Phantom** (general-purpose
strategist) and **Coded** (technical specialist) — living in **one shared agent framework** inside a
single local application. Both are general-purpose PC assistants, not chatbot demos.

---

## 1. Confirmed decisions

| Decision | Choice | Rationale |
|---|---|---|
| Operating system | **Linux first** (Windows/macOS support kept behind the same tool interfaces; UI is a local web app so the OS layer only matters inside tools) | Desktop automation libraries and subprocess control are uniform and testable here |
| Language / runtime | **Python 3.10+**, asyncio | Best ecosystem for AI clients, subprocess/file control, and async I/O; no compile step; one runtime for agents + tools + API |
| UI | Local **single-page web app** served by the same process (FastAPI + WebSocket) | No Electron/native build toolchain; works on any OS; the "desktop interface" runs at `localhost` |
| Storage | **SQLite** (single file) via `aiosqlite`, with FTS5 full-text search | Zero-config, transactional, survives restarts, searchable, laptop-first; can be moved to a server later without rewriting the app |
| AI provider | **NVIDIA NIM-compatible API** (OpenAI-compatible `chat/completions`) behind a `ModelProvider` abstraction | Spec requirement; the abstraction allows future providers without touching the agent core |
| API keys | Environment variables `PHANTOM_NVIDIA_API_KEY` / `CODED_NVIDIA_API_KEY`, with an encrypted-at-rest local secrets file fallback (chmod 600) | Never hard-coded, never logged, masked in the UI |
| Voice | Abstraction layer only (`STTProvider` / `TTSProvider`); text agent is the foundation | Spec: "Voice is an interface layer, NOT the foundation" |
| Agent orchestration | Shared `Agent` core parameterized by an **identity** (system prompt, model, key, memory namespace, tool permissions, conversation namespace) | Phantom and Coded are the same machinery with different identities — one framework, two entities |

## 2. Two entities

- **Phantom** 👻 — primary interface. Strategic, calm, fast, general-purpose. Plans multi-step work,
  uses PC/internet tools, and **delegates deep technical work to Coded**.
- **Coded** 💻 — separate identity with its **own** system prompt, NVIDIA key, conversations,
  long-term memory, configuration, tool permissions and state. Expert at programming, debugging,
  terminals, git, APIs, databases, web development, sysadmin — and also capable of general PC tasks.
  Coded may delegate research/planning back to Phantom.

Delegation contract (every delegation carries): task ID, originating agent, receiving agent, clear
objective, context, effective tool permissions, timeout, result, status. Mutual-delegation loop
guard prevents infinite agent-to-agent ping-pong.

## 3. Module layout

```
phantom_ai/
├── config.py            # Settings store (per-agent), env loading, masking, limits
├── storage/             # SQLite: conversations, messages(+FTS), memories, audit, tasks,
│                        #   delegations, schedules, notifications, confirmations
├── providers/           # ModelProvider abstraction → NVIDIA client (+ offline mock, dev/test only)
├── agents/              # Agent core (context assembly, loop, tool dispatch, error recovery)
│   ├── identities.py    # Phantom & Coded definitions
│   └── delegation.py    # DelegationManager (task ids, timeouts, loop guard)
├── tools/               # Tool registry + real implementations (files, terminal, processes,
│                        #   system, web, browser, gui, clipboard, memory, delegation, notify)
├── permissions/         # Permission levels, per-agent rules, path/command validation
│   └── confirm.py       # Confirmation manager (what/why/affected/action/risk → approve/deny)
├── memory/              # Retrieval: durable facts, preferences, projects, conversation search,
│                        #   summaries, compact context packaging
├── security/            # Secrets store, prompt-injection defense, log redaction
├── tasks/               # Task manager: ids, status, logs, cancellation, concurrency limits
├── heartbeat/           # Scheduler: reminders, quiet hours, notifications, catch-up after restart
├── voice/               # STT/TTS provider abstractions (browser providers ship; server providers pluggable)
├── core/                # Kill switch, event bus
└── api/                 # FastAPI app, REST + WebSocket, static UI
```

## 4. Data flow

```
USER ──chat──▶ PHANTOM ──delegate()──▶ CODED ──tools──▶ PC
                ▲                                            │
                └──────────── structured result ◀────────────┘
                └──▶ USER (streamed answer)

Agent loop (shared):
  context package (system prompt + retrieved memories + windowed history + tool schemas)
   → provider stream (NVIDIA)
   → tool calls? → validate args → permission gate → (confirmation?) → execute tools (parallel where safe)
   → structured results back to model → repeat until final answer (max iterations, cancellable).
```

## 5. Permission & confirmation model

Levels: `READ_ONLY` → `SAFE_ACTION` → `CONFIRM_REQUIRED` → `HIGH_RISK` → `BLOCKED`.
- Per-tool default levels; per-agent overrides (settings); path rules; command classifier
  (denylist → BLOCKED, mutating/system commands → CONFIRM_REQUIRED).
- The permission layer sits **between the AI's decision and actual execution**. Model output can
  never bypass it.
- Confirmations present: what / why / affected scope / exact action / risk, then wait for explicit
  approval. **Approval is per-action, never blanket.**

## 6. Memory model

```
MEMORY
├── Phantom    (conversations, memories/facts, preferences)
├── Coded      (conversations, memories, technical context)
└── Shared     (projects, decisions, approved shared facts)
```
- Short-term: current conversation window (+ rolling summary when trimmed).
- Long-term: durable facts/preferences/projects/decisions with importance, tags, source.
- Archive: every message stored with metadata (agent, conversation, message id, timestamp, role,
  content, tool calls/results, session) + FTS5 searchable. "What did Phantom and I discuss about X
  last month?" → real search, real retrieval.
- Memories are **data, not instructions**: external/web/file content is wrapped as data and the
  system prompt forbids treating it as commands; stored facts can never override safety rules.
- No secrets in conversation records (API keys live in the secrets store only; content is redacted
  on ingest).

## 7. Kill switch

A prominent, always-visible control (UI button, never relies on the AI): engaging it stops the
heartbeat, cancels pending tasks, and cancels in-flight agent runs where safely cancellable; the
user can still browse logs/settings and disarm. Agent runs and scheduler loops check it each step.

## 8. Evolution & System Intelligence (Tier 11, additive)

The system is self-monitoring, multi-brain, graph-aware and continuously improving — built as
additive modules on top of the existing architecture:

- **Capability Registry** (`brains/`) — every brain (Phantom, Coded, Evolution, + dynamically
  registered brains) has a machine-readable definition: id, name, role, system prompt, model,
  provider, tools, memory scope, permissions, dependencies, version, status, health/performance
  stats. New brains register at runtime (validate → capabilities → permissions → activate);
  privileged brains require authorization. Persisted, so they survive restarts.
- **Evolution Brain** (`evolution` identity) — system-level intelligence: introspection tools
  (brain_status, graph_query, self_audit), improvement proposals, snapshots/rollback, loop
  execution, and daily/weekly self-audit schedules (quiet hours 22:00–07:00 UTC).
- **Graph Engine** (`graph/`) — persistent typed nodes/edges (user, project, task, memory,
  tool, conversation, …) with BFS neighbor/path discovery; agents auto-track runs, tool calls
  and memories; the retriever enriches the context package with graph-relevant nodes.
- **Loop Engineering** (`loops/`) — OBSERVE→…→REPEAT orchestration with hard limits: max
  iterations, per-iteration timeout, cost budget, failure threshold, strategy switching,
  escalation to another brain, approval gate, and pre-loop snapshot + rollback on failure.
- **Model routing** (`providers/router.py`) — per-brain fast/default/strong model map chosen by
  task complexity; agents rebuild their provider automatically when the router selects a
  different model.
- **Critic/verification** — `Agent.verify()` runs an independent critic pass; `verify_result`
  tool and the Loop Engine use it before declaring success.
- **Self-improvement with safety** (`evolution/`) — analyst aggregates success/failure, latency,
  API errors, tool errors, handoff failures, approvals/denials, cost, unused capabilities into
  daily/weekly reports and proposals. Proposals deploy only after explicit approval and are
  wrapped in config snapshots; `rollback_snapshot` restores the previous stable configuration.
- **Health Center** — Health & Intelligence dashboard (UI) + `/api/brains`, `/api/graph*`,
  `/api/proposals`, `/api/snapshots`, `/api/evolution/*`, `/api/loops/run`.
- **Self-update** — the Electron app exposes an in-app **Update button** (electron-updater):
  when `package.json` version is increased and a new GitHub Release is published, the app
  detects it via `latest.yml`/`latest-linux.yml`, downloads and restarts to install.

## 8. Verification strategy (how "done" is proven per tier)

- **Tier 1–3** (providers/agents/delegation): unit + integration tests against a **local
  OpenAI-compatible mock server** (NVIDIA is unreachable from this sandbox) exercising streaming,
  retries, timeouts, tool round-trips, per-agent keys/models, delegation ids/timeouts/loop-guard.
- **Tier 4–5** (tools): real execution against temp directories and the local OS; headless
  capabilities (GUI/browser) return structured "environment unavailable" errors — no simulation.
- **Tier 6** (permissions): tests for gating, denials, command validation, redaction.
- **Tier 7** (memory): persistence across manager restart, FTS search, per-agent isolation.
- **Tier 8–9** (voice/heartbeat): abstraction tests + scheduler behavior tests.
- **Tier 10** (advanced): task manager limits, cancellation, observability data.
- Live smoke: boot the app, drive it over HTTP/WS with the mock provider, verify the UI streams.

`scripts/verify_tiers.py` runs every tier's checks and prints a status table.
