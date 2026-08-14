# PHANTOM + CODED — user guide

## First run

1. `./scripts/run.sh` (creates `.venv`, installs deps, starts the app at http://localhost:8000).
2. Open **Settings → API & Model** and paste an NVIDIA key for Phantom and/or Coded
   (or export `PHANTOM_NVIDIA_API_KEY` / `CODED_NVIDIA_API_KEY` before starting).
   Keys are stored in `data/secrets.json` (chmod 600) or read from the environment; they are
   never shown in full in the UI and never written into conversation records.
3. Choose a model per agent (defaults: `nvidia/llama-3.3-70b-instruct`). Any NVIDIA NIM-compatible
   model works; models without native function calling automatically fall back to the
   text-based `<tool_call>` protocol.

## Talking to the agents

- **👻 Phantom** — the main interface: general PC work, research, planning, file/application
  operations, and delegating technical tasks to Coded.
- **💻 Coded** — the technical specialist: programming, debugging, terminal, git, databases,
  servers, sysadmin — and general PC tasks too.

Both stream replies. Tool calls appear as expandable cards; the right **Activity** panel shows a
live feed of everything happening. **Tool Lab** runs any tool manually (real execution,
permission-checked) — handy for giving the agents a head start or testing a tool.

### Example requests
> "Phantom, organize my Downloads folder." → Phantom plans, uses file tools, asks before moving/deleting.
>
> "Coded, open my project and find the error." → Coded reads files, runs commands, reports evidence.
>
> "Phantom, research this topic and prepare a document." → web search → reading → write file (confirmed).
>
> "Coded, run the tests and tell me what's failing." → `run_command` → structured report.
>
> "Phantom, remember that I deploy on Fridays." → `remember` → durable memory (check Memory center).
>
> "What did Phantom and I discuss about the Code Rx backend last month?" → `search_conversations` → archived FTS results.
>
> "Phantom, remind me about this tomorrow." → Heartbeat schedule (Settings → Schedules).

## Permissions & confirmations

Tools are classified by default (read-only → auto; mutating/system → confirmation; dangerous →
blocked). The **Permissions** view shows every tool per agent with its effective level; change
any override there. Confirmations show **what / why / affected / exact action / risk** and apply
to that single action only — there is no blanket approval.

## Kill switch

The red **KILL** button (top-right, always visible) stops the heartbeat, cancels pending tasks
and in-flight runs. The app stays browsable; click **Disarm** to resume. It never relies on the
AI itself.

## Memory center

Per-agent (Phantom / Coded) and **Shared** namespaces. Add facts manually or ask the agents to
remember. "Forget" marks a memory inactive; "Delete forever" removes it. Memories are data —
stored text can never override safety rules or act as instructions.

## Heartbeat / schedules

Settings → Schedules. Expressions: `every 30m`, `every 2h`, `hourly`, `daily at 09:00`,
`at 18:30`, or 5-field cron (`*/10 * * * *`). Quiet hours suppress runs (UTC). Missed runs catch
up after a restart (within 24 h). Results arrive as dismissible notifications — heartbeat never
runs high-risk actions without your approval.

## Voice (push-to-talk)

Click 🎙️ and speak (Web Speech API in Chrome/Edge). The transcript goes through the *same* agent
core as typed text. 🔊 toggles spoken replies (browser speech synthesis; interrupted on your next
message). Server-side STT/TTS providers (OpenAI/NIM-compatible endpoints) plug in behind the same
abstraction — configure in Settings → Voice.

## Security posture

- No hard-coded keys; secrets masked and file-chmod-0600'd; command lines sanitized of
  credential-looking env vars.
- SSRF guard on HTTP tools; path-traversal guard on file tools; archive extraction rejects
  `..`/absolute members.
- External content (web, files, tool results) is wrapped as data and never treated as commands.
- Audit log records tool calls, decisions, latency, tokens, errors, confirmations, delegations.
- Optional access token: set `PHAI_ACCESS_TOKEN` and every API/WS call needs it
  (`X-Access-Token` header / `?token=` for WS).

## Operational notes

- Storage is a single SQLite file (`data/phantom_coded.db`) — copy it to move machines;
  the heartbeat/storage layer is designed to migrate to an always-on host without rewriting.
- Bind address/port: `PHAI_HOST` / `PHAI_PORT` (default `0.0.0.0:8000`). For local-only use,
  set `PHAI_HOST=127.0.0.1`.
- GUI automation needs a real desktop session (`pyautogui`/`xdotool`/`mss`); browser automation
  needs `pip install playwright && playwright install chromium`.
