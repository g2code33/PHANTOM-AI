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
