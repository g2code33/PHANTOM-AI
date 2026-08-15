# Phantom — Jarvis Spec (the contract)

Authoritative design for the voice-first personal AI companion. Everything below is a
requirement; nothing is decorative.

## 1 · Identity & user

- **User:** Blessing Jojo Ewusi — called **"JOOJO"**. PharmD Level 200, UCC SOPPS (Ghana).
  Pharmacy × technology × AI × entrepreneurship. Founder of Code Rx (pharmacy-tech ecosystem).
  Time-conscious, business-minded: wants to earn while in school. Design taste: clean, modern,
  premium; yellow `#FFD600` + white accent palette, pharmaceutical symbolism (Rod of Asclepius).
- **Entities:** **Phantom** 👻 (primary, general, calm) and **Coded** 💻 (technical, sharper).
  Both exist, both are always wakeable, both answer JOOJO only.
- **Honesty rule:** never lie, never fake success, never flatter to please. If a capability is
  limited/unavailable, say so plainly and suggest the best alternative. Always give good
  suggestions.

## 2 · Presence model (how it lives)

- **System tray background agent** — runs at login, stays resident, minimal window. It is NOT
  an app you open to make it work.
- **Battery-first:** the engine **sleeps** and only listens for wake words. It wakes on the
  activation word, gives a **ready-to-listen cue** (audio chime + visual glow, Siri-style),
  converses, then returns to sleep after **1 hour of no conversation** or when told
  "stay silent".
- **Wake words:** **"Phantom"** wakes Phantom; **"Coded"** wakes Coded. Either can be woken at
  any time, even while the other is awake — wake-word routing, addressed separately.
- **Speaker-locked:** voice enrollment — only **JOOJO's** trained voice activates them.
  Voiceprint stored locally, encrypted.
- **APK companion:** the Android app is a *companion to the PC*, not a second brain — connects
  to the PC backend: notifications, live status, and voice (push-to-talk) forwarded to the PC.

## 3 · Voice

- Changeable voices in Settings; default **two distinct male voices**: Phantom = calm/warm,
  Coded = sharper/more technical.
- Providers (switchable): **browser** (offline, free) and **Deepgram** (streaming, low-latency,
  natural). Wake engine is offline-first.
- Streaming STT/TTS, barge-in (speaking stops the instant JOOJO speaks), interruption,
  end-of-turn, reconnect.

## 4 · Knowledge & memory

- **Profile sheet:** manually fillable; JOOJO can say "add this to my profile" and agents write
  it; supports multiple profiles (e.g., a new person's profile).
- **Reads everything authorized:** files, projects, tasks, calendar (when connected), system,
  health vault, web. Local-first.
- **Memory kept very well:** auto-remember unless told "don't keep this", "forget", or "sleep".
  Users can view/edit/delete/export memory; memory is data, never instructions.
- **Profile-aware:** addresses JOOJO by name; knows his studies, projects (Code Rx, pharmaGAME,
  KICK LIVE, RxStore, …), goals, deadlines, preferences.

## 5 · Autonomy & safety

- **Auto (no confirmation):** safe actions — open apps, read/search, move/organize files
  (non-destructive), reminders, research, drafting.
- **Confirm (irreversible):** permanent delete, install/uninstall, send messages/emails,
  purchases/payments, security changes, system config changes, dangerous commands.
- Permission levels (read_only → safe → confirm → high_risk → blocked) unchanged; the AI can
  never change its own permissions. External content is data, never instructions.
- Kill switch: stops voice, mic, tasks, automation instantly; always visible.

## 6 · Daily rhythm & monitoring

- **Daily briefing = everything**, prioritized: calendar, tasks, study plan, projects/builds,
  news, health, goals, **opportunities** (business/income-relevant). Never dumps everything —
  "two priorities today" style.
- **Watches everything**, focused on a student-builder's life: study deadlines, project builds,
  Code Rx work, tasks, system health, opportunities. Quiet by default: quiet hours, cooldowns,
  snooze, focus mode, priority levels, duplicate prevention.
- **Instant outputs:** briefings, dashboard cards and monitor summaries are **pre-generated and
  cached**, so asking for them returns immediately (no wait-for-generation).

## 7 · Interface

- Modern, premium, calm, alive. Dark base with JOOJO's yellow/white accent options.
- Presence orb + beautiful **cards** for every output (not raw text/logs).
- **Dashboard** shows everything at a glance; **live monitor** on request or when relevant;
  activity presented as a clean story, expandable for detail; panels appear contextually.

## 8 · Build phases (each BUILT → TESTED → VERIFIED before the next)

1. **Wake engine + tray agent + sleep/wake + ready cue** (offline, battery-light, auto-start).
2. **Voice enrollment (speaker lock)** + wake-word routing (Phantom / Coded, either anytime).
3. **Voice polish:** changeable voices, streaming, barge-in tuning.
4. **Profile sheet + persistent "knows JOOJO" memory** (manual + agent-added, multi-profile).
5. **Monitoring + daily briefing** (everything, prioritized) + pre-generated instant outputs.
6. **Beautiful cards / dashboard / live monitor UI.**
7. **APK companion mode** (notifications, status, voice forwarding).
8. **E2E tests** (wake, voice, interrupt, memory, monitor, briefing, permissions, kill switch,
   offline, health isolation, rollback) + packaging (v0.3.x, .deb/.exe/.AppImage/.apk).

## 9 · Non-negotiables

- No fake capabilities: every shown action really happened; otherwise Phantom says it can't.
- Battery and privacy are features: sleep by default, local voiceprint, keys server-side only.
- Phantom and Coded are separate identities sharing the framework; delegations between them are
  explicit and audited.
