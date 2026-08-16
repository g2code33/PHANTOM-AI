# PHANTOM — Complete Jarvis Transformation (migration plan)

This document records the audit (Phase 1) and the incremental migration plan
(Phases 2–15) for turning the existing PHANTOM-AI repository into a **voice-first
personal AI computer companion** — without throwing away the working infrastructure.

## Phase 1 — Audit (what exists, what stays)

Verified at checkpoint `db38235` (96 tests passing, 87 tools):

| System | Status | Action |
|---|---|---|
| Shared agent core (Phantom, Coded, Evolution, Health) | Working: streaming, tools, permissions, memory, delegation, loop engine | **Keep** — this is Phantom Core |
| NVIDIA NIM provider + ModelRouter + offline fallback | Working | **Keep** — add voice provider abstraction alongside |
| Tool registry (87 real tools: files, terminal, processes, web, GUI, browser, memory, health, evolution, delegation) | Working | **Keep** — reorganize behind SAFE / POWER USER modes |
| Permissions (5 levels), confirmations, audit, kill switch | Working | **Keep + extend** — kill switch also stops voice/mic |
| Memory: conversations+FTS, per-agent/shared, retriever, health vault (encrypted) | Working | **Keep** — health stays isolated |
| Graph engine, Loop engine, Evolution (proposals/snapshots/analyst), heartbeat scheduler | Working | **Keep** — heartbeat gains proactive-speech hook |
| Capability Registry (BrainDefinition + dynamic registration) | Working | **Extend** — seed the full specialist-brain list |
| Electron + Capacitor packaging + release-on-push CI | Working (v0.1.2 fixes) | **Keep** — rename product to **Phantom**, bump 0.2.0 |
| **UI** | Functional dashboard — **the core problem** | **Rebuild** as the Phantom presence environment |
| **Voice** | Abstraction stubs only | **Build** the real-time voice engine (primary layer) |

## Phases 2–15 (execution order, each BUILT → TESTED → VERIFIED)

1. **Checkpoint** — arena branch `db38235` is the version checkpoint; destructive changes only after it.
2. **Rename** — app/product name becomes **Phantom** (Coded stays as an internal persona); deb/exe/AppImage artifact names, window title, Android app name; version → 0.2.0.
3. **Real-time voice foundation** — `ui/voice.js`: state machine (IDLE/LISTENING/THINKING/SPEAKING/INTERRUPTED/EXECUTING/VERIFYING/ERROR/DISCONNECTED), streaming STT (browser Web Speech default; Deepgram streaming via server-minted ephemeral token), streaming TTS (browser speechSynthesis default; Deepgram speak socket), VAD (energy-based) for end-of-turn + **barge-in**, interruption/cancellation, reconnect, mic/speaker state.
4. **Connect voice → agent core** — voice transcript → existing chat API → streamed chunks → TTS; tool activity drives EXECUTING/VERIFYING states.
5. **Barge-in** — speaking stops the instant mic energy is detected or the user taps the mic; engine returns to LISTENING.
6. **Heartbeat → voice** — scheduled events can speak proactively when `voice.proactive_speech` is enabled, respecting quiet hours, cooldowns and mute; `voice.speak` events over WebSocket.
7. **Primary interface rebuild** — immersive Phantom presence: central living core/orb (state-reactive), ambient environment, minimal chrome, transcript secondary, contextual activity, optional drawers for power features, first-run onboarding ("Hello. I'm Phantom.").
8. **Contextual views** — activity stream (expandable), persona switch (Phantom/Coded), panels appear when relevant.
9. **Specialist brains** — seed the full 31-capability list (planner, tutor, research, coding, security, critic, …) as registry definitions with tool allowlists; no rewrite needed (registry already dynamic).
10. **Health Brain** — already present + isolated encrypted vault (keep; wire into the new UI's Wellness panel).
11. **Graph + Evolution** — already present (keep; surfaced in the new UI).
12. **Local/online modes** — PRIVATE (mic off, no external voice), PUSH-TO-TALK, CONVERSATION, PROACTIVE; LOCAL ONLY / ONLINE ENHANCED indicator; graceful degradation.
13. **Security hardening** — kill switch stops voice + mic + automation; secrets stay server-side (Deepgram key never reaches the frontend — ephemeral tokens only); external content stays data.
14. **End-to-end tests** — new tests: voice config/ephemeral-token endpoints, proactive speak event, kill-switch voice stop, specialist seeding, rename; full suite green.
15. **Packaging** — Phantom .deb / .exe / .AppImage / .apk via the existing CI; app launches directly into the presence environment.

## Guardrails
- No component is rewritten because of style preference — only the UI shell and the voice layer are new; everything else is preserved and extended.
- No fake capabilities: the orb reflects real states (mic/stream/tool events); if the browser lacks mic/TTS, the UI says so.
- Barge-in and kill switch are hard requirements, not decorative.

## HUD (real telemetry, tiered rendering)

The presence orb + gauges (added with the v0.4.8 HUD pass) follow these rules:

- **One state machine, two rendering tiers.** The tier is derived ONLY from the
  existing voice/presence state (`body[data-state]` + `body.presence-sleeping`)
  via the pure `hudTier()` helper in `ui/hud.js` — there is no second state
  system. Sleeping/silenced → **low tier**: the canvas redraw loop stops
  entirely (one static dim frame), CSS ring/sweep animations are paused, and
  the gauges poll every 8 s. Awake/listening/speaking → **full tier**: canvas
  loop active, gauges every 2 s.
- **Spectrum analyzer = real audio.** While listening, the canvas draws the
  frequency data from an AnalyserNode tapped off the *existing* mic stream
  (`PhantomVoice._analyser` — no second getUserMedia). While speaking, it
  draws the TTS playback audio routed through an AnalyserNode (server-TTS and
  Deepgram-TTS both; system speechSynthesis voices cannot be tapped, so the
  HUD honestly shows "system voice" instead of faking bars).
- **Layered orb, no WebGL.** Concentric counter-rotating CSS rings, a radial
  glow layer whose opacity follows state, one-time static SVG tick marks, and
  a conic-gradient radar sweep — all paused in the low tier.
- **Telemetry (`GET /api/hud`, `phantom_ai/hud/sampler.py`) — all psutil, no
  fakes.** Per-core CPU (`cpu_percent(percpu=True)`), disk I/O and network
  byte-rate deltas between polls, battery (`sensors_battery()`; reports
  "unavailable" honestly on desktops), and the top process by *measured* CPU
  between samples. The foreground/focused window is NOT reported: the existing
  psutil-based tools can't see it on Linux without adding X11 utilities, and
  per the guardrails we skip rather than add invasive new access.
- Every gauge shows a real reading or an honest "unavailable" — never a
  simulated number. Verified by `node scripts/test-hud.mjs` (pure helpers)
  and `tests/test_hud.py` (sampler contract + endpoint).

## Full radial HUD (v0.4.9)

The presence view is now a Stark-style radial layout, every panel real-data:

- **Top:** session badge (avatar + "Phantom OS" + real app version + user name
  from app state) and a client-side live clock/date.
- **Left:** CPU history line graph + memory graph (rolling buffers from
  /api/hud polls), per-core "processor units" bar cluster, disk I/O R/W rates.
- **Right:** weather (Open-Meteo via /api/hud/weather proxy — no API key;
  temp/condition/humidity/wind/pressure, sunrise/sunset, 5-day outlook;
  location = settings `hud.weather.lat/lon`, default Accra), moon phase
  (computed client-side from the real date), network up/down traffic graphs
  (byte-rate deltas), and a system panel (battery — honest unavailable on
  desktops — process count, top process by measured CPU).
- **Bottom:** horizontal spectrum strip driven by the REAL AnalyserNode data
  (mic while listening, TTS playback while speaking; system-voice fallback
  shows "system voice"), plus the PHANTOM wordmark footer.
- **Center:** the existing orb untouched (state/glow logic unchanged) with the
  layered CSS rings + one-time SVG tick marks + corner brackets.

Sleep/awake tiers still rule: sleeping = panels static, gauges poll every 8 s,
spectrum strip frozen; awake = 2 s polls, live spectrum. Weather caches 10 min.

**Onboarding no longer re-asks every launch.** Root cause: the Electron shell
passed PHAI_PORT=0 → random port per launch → the browser origin changed →
localStorage (onboarding flag, theme, mic permission) reset each open. Fixed
by a stable default port (47611, fallback only if busy) AND server-side
persistence of `ui.onboarded` / `ui.userName` / `ui.theme` via /api/settings.

## In-app diagnostics (no console needed)

Settings → **Diagnostics** shows what's wrong without opening a terminal:
- recent backend log lines (last 400, captured in-process) + frontend JS errors
- version, uptime, wake state, which API keys are set (presence only — never values)
- speaker-engine status with an actionable **install hint** when the speaker lock
  engine (resemblyzer + torch) isn't installed: the exact `pip install --user`
  command with a Copy button + Re-check.

**Voice enrollment auto-instruct** (chosen over bundling torch into the app to
keep the build light): when the engine is missing, the enrollment section shows
the reason + the one-time install command; the Enroll buttons stay disabled
until it's installed. After installing, click **Re-check** and enroll normally.
