# PHANTOM + CODED — user guide

## First run

1. `./scripts/run.sh` (creates `.venv`, installs deps, starts the app at http://localhost:8000).
2. Open **Settings → API & Model** and paste an NVIDIA key for Phantom and/or Coded
   (or export `PHANTOM_NVIDIA_API_KEY` / `CODED_NVIDIA_API_KEY` before starting).
   Keys are stored in `data/secrets.json` (chmod 600) or read from the environment; they are
   never shown in full in the UI and never written into conversation records.
3. Choose a model per agent (defaults: `meta/llama-3.3-70b-instruct`). Any NVIDIA NIM-compatible
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

## 🧬 Evolution & Health dashboard

The sidebar's **Health** view is the Evolution Brain's dashboard:
- **Brains** — every registered brain (Phantom, Coded, Evolution, plus any you register) with
  live status, model, tools, success rate, average latency, error rate, task count.
- **Knowledge graph** — node/edge counts and manual tracking ("project → Code Rx").
- **Proposals** — improvement suggestions (from Evolution's self-audit or the agents); approve
  & deploy (wrapped in a pre-change snapshot) or reject.
- **Snapshots** — configuration version control; restore any snapshot to roll back.
- **Loop lab** — run an objective through the improvement loop
  (observe/understand/plan/execute/test/verify/evaluate/learn/improve/repeat) with iteration,
  timeout, cost and failure limits; results land in Tasks.
- **Self-audit** — "Run daily self-audit" generates the internal report; Evolution also runs
  daily/weekly audits on a schedule (quiet hours apply).

Try: "Evolution, run a self-audit and propose improvements." Or register a new specialist brain
from the API/UI: id, role, system prompt, tools — it becomes a live agent (privileged brains
require approval).

## Voice-first Phantom (the primary interface)

Phantom launches into a **presence environment** — a calm, living core at the center of the
screen that reflects Phantom's real state (idle / listening / thinking / speaking / executing /
verifying / interrupted / error / disconnected). Text is secondary; the transcript sits below
the presence.

- **Speak naturally.** In *conversation* mode the mic is always listening (browser Web Speech
  streaming, or Deepgram streaming when configured). Phantom replies out loud and you can
  **interrupt** — the moment it hears you, it stops speaking and listens.
- **Push-to-talk** — hold/tap the mic button to talk; release to end the turn.
- **Private mode** — mic off, no external voice services; typed input only.
- **Proactive speech** — when enabled (Settings → Voice), scheduled events can speak to you
  ("your build finished", reminders), respecting quiet hours and a cooldown. Quiet by default.
- **Kill switch** — the ⏻ button stops voice, the microphone, tasks and agent runs instantly.
- **Deepgram** — configure `DEEPGRAM_API_KEY` in Settings → Voice for low-latency streaming
  STT/TTS. The key stays server-side; the UI only ever receives a 10-minute ephemeral token.
- **First run** — a short onboarding asks your name, voice, AI mode and permission level, then
  Phantom greets you by voice.

## Specialist brains

The Capability Registry seeds the full specialist catalogue (planner, tutor, research, coding,
security, critic, testing, execution, finance, goals, …). They share the platform provider by
default; each can be given its own model/API later. Evolution (🧬 panel) shows every brain's
health and lets you approve proposals and restore snapshots.

## 🩺 Health Center (Health Brain)

The **🩺 Wellness** view is the Health Brain — a private personal health & wellness assistant
(a separate entity with its own identity, tools, memory and permissions):

- **TODAY** — hydration, meals, activity/steps, sleep, medications, appointments at a glance
  (only what you record).
- **Quick log** — record a measurement (weight, blood pressure, heart rate, temperature,
  glucose, SpO2, sleep, steps), a symptom (severity 0–10, duration, triggers), a habit
  (water/meal/exercise/break), a medication (schedule only), a health goal, or an appointment.
- **Red-flag safety** — if you record something potentially serious (e.g. chest pain, SpO2 <
  92%), the Health brain clearly says it may be concerning and urges appropriate
  urgent/emergency care. It never diagnoses, never reassures falsely, never delays care, and
  never changes medication doses.
- **Health Memory Vault** — every health record is stored **encrypted** (Fernet, key kept in
  the chmod-600 secrets file) and strictly separate from general Phantom memory; other brains
  cannot read it. You can search, delete individual records, clear categories or everything,
  and **export** your data as JSON.
- **Privacy** — settings control sharing with other brains (default OFF), inclusion in the
  Daily Briefing (default ON, non-sensitive) and sensitive briefing details (default OFF).
- **Routines** — morning/afternoon/evening wellness routines that become real scheduled
  notifications (quiet-hours aware); a "Daily health briefing" runs each morning at 07:30.
- **Disable** — turn the Health Brain off anytime; records stay encrypted and untouched.

Health Brain is not a doctor: it organizes, tracks, educates and reminds — and points you to
real professionals whenever that's the right move.

## ⬆ App updates

The packaged desktop app self-updates from GitHub Releases. When the version in `package.json`
is increased and a new release is published, the ⬆ button in the top bar offers the update
(download → restart & install). Settings → App updates shows the current state and a manual
check. Works in the packaged app only (dev mode/browser show status only).

## Heartbeat / schedules

Settings → Schedules. Expressions: `every 30m`, `every 2h`, `hourly`, `daily at 09:00`,
`at 18:30`, or 5-field cron (`*/10 * * * *`). Quiet hours suppress runs (UTC). Missed runs catch
up after a restart (within 24 h). Results arrive as dismissible notifications — heartbeat never
runs high-risk actions without your approval.

## Voice (multi-provider engine)

Settings → Voice runs the full **failover chains** (automatic — you don't switch manually):

- **STT (speech-to-text):** Deepgram → Groq Whisper (`whisper-large-v3-turbo`) → Local Whisper (offline)
- **TTS (speech):** Deepgram Aura → configured cloud fallback → Local (Piper / espeak-ng)

The central `VoiceManager` (backend) tracks provider health, disables unhealthy providers with
exponential backoff (short for rate limits, long for invalid keys), counts usage + estimated cost
(24h dashboard in Settings), and always returns friendly messages — e.g.
*"Deepgram is rate-limited — switching to the next provider"* — never raw errors or keys.

- **API keys** (NVIDIA, Deepgram, Groq, cloud token) are stored server-side in a chmod-600 file,
  masked everywhere in the UI (`dg_••••`), and each has a **Test** button that verifies it live
  without the key ever leaving the backend.
- **Offline:** Local Whisper (`base` recommended on 8 GB laptops; `tiny` for weaker machines) +
  Piper/espeak-ng keep voice working with zero internet. The Whisper model loads **only while you
  speak** and releases after idle — it never runs in the background. Install:
  `pip install faster-whisper` + `sudo apt install espeak-ng`, then download a model once with
  `python -m phantom_ai.voice.install_whisper base` (see Operational notes).
- **Provider status dashboard** (Settings → Voice) shows 🟢/🟡/🔴 per provider, which one is
  active, and last error. **Test Voice System** records 2 s and runs the whole pipeline
  (mic → STT → Phantom AI → TTS → speaker) end-to-end.
- Voice modes: conversation (continuous, auto-stops after silence), push-to-talk, private.
  Adjustable VAD sensitivity, auto-stop silence, and max recording length in Settings.
- Browser (Web Speech / speechSynthesis) and Deepgram streaming modes remain available if you
  prefer them.

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

## Temperature (what it does)

Each agent has a **Temperature** setting in Settings → AI. It controls how
"creative" vs "strict" the model is:

- **0** → always picks the most likely answer: factual, repeatable, safe.
- **0.4 (default)** → balanced: dependable but not robotic. Good for a
  personal companion.
- **0.7 – 1.0** → more creative/varied: better for ideas, storytelling, jokes.
- **Above 1.0** → increasingly random; can ramble or repeat.

Lower it if Phantom gives off-topic answers; raise it if replies feel wooden.
It's per-agent, so you can keep Phantom calm (0.4) and Coded technical (0.2)
if you like.

## iPhone companion — how to start

The phone companion (`/mobile`) is a PWA: no App Store. Three ways to connect,
in order of preference:

1. **Same Wi-Fi (PC nearby)** — PC: keep Phantom running
   (`bash scripts/run.sh` or the tray app). On the phone: open
   `http://<PC-IP>:8000/mobile` (find PC-IP with `hostname -I`), enter the
   address in the connect screen → **Connect**. Set `PHAI_ACCESS_TOKEN` on the
   PC if you want it password-protected.
2. **Anywhere (tunnel)** — PC: `bash scripts/tunnel.sh` prints an
   `https://…trycloudflare.com` URL. Open it on the iPhone in **Safari**, then
   **Share → Add to Home Screen** → it becomes a full-screen app icon. Works
   from home/campus anywhere. (Public URL — keep the token set.)
3. **Portable cloud (PC off)** — deploy the Worker once with
   `bash cloud/deploy.sh`, paste its URL into the phone's connect screen
   (Portable field) + the cloud token. The companion keeps working even when
   your PC is asleep.

The 🔄 button on the phone cycles **auto → PC → portable**: auto prefers your
PC when reachable and falls back to the cloud. Tap-to-talk on the phone sends
audio to the PC's voice engine (or the Worker's) and streams the reply back.
