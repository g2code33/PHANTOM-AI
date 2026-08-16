/* Phantom — presence environment + voice-first interaction.
 * All backend APIs are unchanged; this is the new product shell. */
"use strict";

const API_BASE = (localStorage.getItem("phai.apiBase") || "").replace(/\/+$/, "");
const WS_BASE = API_BASE ? API_BASE.replace(/^http/, "ws") : "";

const AGENTS = { phantom: { name: "Phantom", emoji: "👻" },
                 coded:   { name: "Coded",   emoji: "💻" } };

const STATE_TEXT = {
  IDLE: "Phantom is here.", LISTENING: "Listening…", THINKING: "Thinking…",
  SPEAKING: "Speaking…", INTERRUPTED: "Interrupted — listening…",
  EXECUTING: "Executing…", VERIFYING: "Verifying…",
  ERROR: "Something went wrong.", DISCONNECTED: "Connection lost — reconnecting…",
};

const state = {
  // exposed for HUD/session badge (module-scope const is not on window)
  agent: "phantom", conversations: [], currentConv: null,
  runId: null, running: false, killEngaged: false, voiceOn: true,
  // MULTI-TASKING: per-agent run state — Phantom and Coded can work at the
  // same time on separate tasks, and each can command the other.
  runs: {},            // agent -> { runId, running }
  convs: {},           // agent -> current conversation id
  pendingConfirmations: {}, userName: localStorage.getItem("phantom.userName") || "",
  micLevel: 0, transcript: [],
};
window.phantomState = state;

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const timeAgo = (iso) => {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  if (isNaN(t)) return "";
  const s = Math.floor((Date.now() - t) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return new Date(t).toLocaleDateString();
};

// ---- frontend error log (shown in Settings → Diagnostics) ----
const FE_LOG = [];
window.addEventListener("error", (ev) => {
  const line = `${new Date().toISOString().slice(11, 19)} ERROR ${ev.message || ev.error || "?"} @ ${ev.filename || ""}:${ev.lineno || "?"}`;
  FE_LOG.push(line);
  if (FE_LOG.length > 60) FE_LOG.shift();
});
window.addEventListener("unhandledrejection", (ev) => {
  const r = ev.reason || {};
  const line = `${new Date().toISOString().slice(11, 19)} ERROR unhandled: ${r.message || r || ev}`;
  FE_LOG.push(line);
  if (FE_LOG.length > 60) FE_LOG.shift();
});

async function api(path, opts = {}) {
  const res = await fetch(API_BASE + path, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    method: opts.method || (opts.body ? "POST" : "GET"),
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { const j = await res.json(); detail = j.detail || j.error || detail; } catch (e) {}
    throw new Error(detail || `HTTP ${res.status}`);
  }
  return res.json();
}

/* ============================== VOICE ============================== */
const voice = new PhantomVoice();
let micLevelTarget = 0;

function setPresence(pstate, sub) {
  const s = (pstate || "IDLE").toUpperCase();
  document.body.dataset.state = s.toLowerCase();
  const label = $("stateLabel");
  if (label) label.textContent = s;
  const subEl = $("stateSub");
  if (subEl) subEl.textContent = sub || STATE_TEXT[s] || "";
  if (typeof hudApplyTier === "function") hudApplyTier();
}

voice.onState = (s) => {
  setPresence(s);
  const ptt = $("pttBtn");
  if (ptt) {
    ptt.classList.toggle("listening", s === "LISTENING");
    ptt.classList.toggle("speaking", s === "SPEAKING");
  }
  if (s === "ERROR") toast("⚠️ " + $("voiceHint")?.textContent || "voice error");
};
voice.onInterim = (t) => {
  const h = $("voiceHint");
  if (h) h.textContent = t ? "🎙️ " + t.slice(0, 160) : "";
};
voice.onFinal = async (text, meta) => {
  $("voiceHint").textContent = "";
  if (meta?.provider) {
    // show which provider handled the speech (multi-provider transparency)
    const provLabel = { deepgram: "Deepgram", groq: "Groq Whisper", local_whisper: "Local Whisper", browser: "Browser" }[meta.provider] || meta.provider;
    const el = $("stateSub");
    if (el) el.textContent = "via " + provLabel;
  }
  if (state.running) return; // ignore stray transcripts while an agent is running
  await sendMessage(text, { via: "voice" });
};
voice.onAudioLevel = (l) => { micLevelTarget = l; };
voice.onError = (msg) => {
  const h = $("voiceHint");
  if (h) h.textContent = "⚠️ " + msg;
};

/* wake word heard while Phantom is sleeping → capture + verify + wake */
voice.onWakeWord = async (agent) => {
  if (state.killEngaged) return;
  // wake word = bring the app to the front + Phantom splash, then continue
  if (window.phaiApp && window.phaiApp.showWindow) { try { window.phaiApp.showWindow(); } catch (e) {} }
  showSplash(1600);
  setPresence("WAKING", `“${agent}” heard — verifying…`);
  const wav = await voice.captureWav(2);
  if (!wav) { setPresence("IDLE", "Couldn't capture audio for verification"); return; }
  const reader = new FileReader();
  const b64 = await new Promise((res) => { reader.onload = () => res(String(reader.result).split(",")[1]); reader.readAsDataURL(wav); });
  try {
    const res = await api("/api/presence/wake", { body: { agent, sample_wav: b64 } });
    if (res.woken) {
      voice.currentAgent = agent;
      voice.playReadyCue();
      const name = state.userName || "JOOJO";
      const greet = agent === "coded"
        ? "Coded here. What are we building, " + name + "?"
        : "I'm here, " + name + ". What do you need?";
      setPresence("LISTENING", (agent === "coded" ? "Coded" : "Phantom") + " is awake — speak");
      if (state.voiceOn) voice.speak(greet);
    } else if (res.need_verification) {
      setPresence("IDLE", "Voice sample needed — try again");
    } else {
      setPresence("IDLE", "Voice not recognized — only JOOJO can wake me");
      addActivity("wake", "🔇 wake denied: " + (res.reason || "voice mismatch"), "tool-err");
    }
  } catch (e) {
    setPresence("IDLE", "Wake failed: " + e.message);
  }
};

/* ============================== WS ============================== */
// Robust reconnect: backoff (1.5s → 15s cap), a max before showing
// DISCONNECTED instead of an infinite retry loop, and an HTTP presence
// fallback so the UI stays LIVE even when the WS won't connect.
let ws = null;
let wsRetries = 0;
let wsTimer = null;
let wsFallbackTimer = null;
const WS_MAX_RETRIES = 5;
function connectWS() {
  if (wsTimer) { clearTimeout(wsTimer); wsTimer = null; }
  const base = (WS_BASE || location.origin).replace(/\/+$/, "");
  const proto = base.startsWith("https") ? "wss" : "ws";
  try {
    ws = new WebSocket(`${proto}://${base.replace(/^https?:\/\//, "")}/ws`);
  } catch (e) {
    scheduleWSRetry();
    return;
  }
  ws.onmessage = (ev) => { try { handleEvent(JSON.parse(ev.data)); } catch (e) {} };
  ws.onclose = () => {
    wsRetries += 1;
    if (wsRetries > WS_MAX_RETRIES) {
      setPresence("DISCONNECTED");
      startPresenceFallback();   // keep the UI live via HTTP polls
      return;                    // no infinite reconnect loop
    }
    scheduleWSRetry();
  };
  ws.onopen = () => {
    wsRetries = 0;
    stopPresenceFallback();
    try { ws.send(JSON.stringify({ type: "ping" })); } catch (e) {}
    if (!state.running) setPresence("IDLE");
  };
}
function scheduleWSRetry() {
  const delay = Math.min(1500 * Math.pow(2, wsRetries - 1), 15000);
  wsTimer = setTimeout(connectWS, delay);
}
function stopPresenceFallback() {
  if (wsFallbackTimer) { clearInterval(wsFallbackTimer); wsFallbackTimer = null; }
}
// HTTP presence poll fallback: when WS is down we still show real state.
function startPresenceFallback() {
  if (wsFallbackTimer) return;
  loadPresence();
  wsFallbackTimer = setInterval(loadPresence, 4000);
}

let sentenceBuf = "";
function flushSpeech() {
  if (sentenceBuf.trim()) { voice.speak(sentenceBuf.trim()); sentenceBuf = ""; }
}
function pushChunkToSpeech(text) {
  sentenceBuf += text;
  const parts = sentenceBuf.split(/(?<=[.!?\n])/);
  if (parts.length > 1) {
    sentenceBuf = parts.pop();
    const ready = parts.join("").trim();
    if (ready) voice.speak(ready);
  }
}

function handleEvent(payload) {
  const { event, data, agent, run_id } = payload;
  // MULTI-TASK: events are routed per agent — both can stream at once.
  const run = agentRun(agent);
  const isFocused = agent === state.agent && run_id === state.runId;
  switch (event) {
    case "agent.chunk":
      if (run_id === run.runId) {
        if (isFocused) appendChunk(data.text);
        if (state.voiceOn) pushChunkToSpeech(data.text);
      }
      break;
    case "agent.run_started":
      if (run_id === run.runId) {
        setAgentRunning(agent, run_id);
        if (isFocused) { setPresence("THINKING"); showRunIndicator(); setContextLine(data); }
      }
      break;
    case "tool.started":
      if (isFocused) setPresence("EXECUTING");
      addToolCard(data, "started"); addActivity("tool", `🔧 ${data.name}`, "tool-start");
      break;
    case "tool.completed":
      if (isFocused) setPresence("THINKING");
      updateToolCard(data, true);
      addActivity("tool", `✓ ${data.name} · ${(data.latency_ms || 0).toFixed(0)}ms`, "tool-ok");
      break;
    case "tool.error":
      if (isFocused) setPresence("THINKING");
      updateToolCard(data, false);
      addActivity("tool", `✗ ${data.name}: ${data.message}`, "tool-err");
      break;
    case "agent.run_completed":
      if (run_id === run.runId) {
        setAgentDone(agent);
        if (isFocused) { hideRunIndicator(); setContextLine(null); finalizeAssistantMessage(data); loadConversations(); }
        flushSpeech();
        if (data.status === "ok" && state.voiceOn && data.content && voice.mode !== "private") {
          voice.speak(data.content); // voice-first: both agents' replies are spoken
        } else if (!anyRunning()) {
          resumeListeningAfterReply();
        }
      }
      addActivity("agent", `${(AGENTS[data.agent]?.emoji || "")} ${data.status} · ${(data.latency_ms || 0).toFixed(0)}ms`, "tool-ok");
      break;
    case "voice.speak":
      if (state.voiceOn && !state.killEngaged) {
        voice.speak(data.text || "");
        addActivity("voice", `🗣 ${(data.text || "").slice(0, 120)}`, "delegation");
      }
      break;
    case "presence.state":
      handlePresence(data);
      break;
    case "wake.detected":
      addActivity("wake", `🔔 ${data.agent === "coded" ? "Coded" : "Phantom"} woken (${data.source})`, "tool-ok");
      break;
    case "ready_cue":
      voice.playReadyCue();
      setPresence("LISTENING", (data.agent === "coded" ? "Coded" : "Phantom") + " is ready — speak");
      break;
    case "voice.stop":
      voice.stopAll(); voice.stopMic(); setPresence("IDLE");
      break;
    case "confirmation.requested":
      if (data.confirmation) state.pendingConfirmations[data.confirmation.id] = data;
      showConfirmation(data);
      loadApprovals();  // keep the Approvals tab in sync
      break;
    case "confirmation.decided":
      if (data.confirmation) delete state.pendingConfirmations[data.confirmation.id];
      hideConfirmation(data.confirmation && data.confirmation.id);
      loadApprovals();
      break;
    case "notification.new":
      toast(`🔔 ${data.notification?.title || "Notification"}`);
      refreshNotifications();
      break;
    case "task.update": if (state.currentPanel === "tasks") loadTasks(); break;
    case "delegation.started":
      addActivity("delegation", `⇄ ${AGENTS[data.origin]?.name || data.origin} → ${AGENTS[data.target]?.name || data.target}`, "delegation");
      break;
    case "killswitch.state":
      setKillState(data.engaged);
      if (data.engaged) { voice.setKill(true); voice.stopMic(); }
      else voice.setKill(false);
      break;
    case "heartbeat.run": addActivity("heartbeat", `💓 ${data.name}`, "tool-start"); break;
    case "cloud.deploy_log":
      document.dispatchEvent(new CustomEvent("cloud-deploy-log", { detail: data.line || "" }));
      break;
    case "hud.open": {
      // an agent (Phantom/Coded) asked to show a panel on screen
      const key = (data && data.panel) || "";
      const titles = { cpu: "CPU", memory: "Memory", disk: "Disk I/O", net: "Network", weather: "Weather", moon: "Moon", system: "System" };
      if (titles[key] && typeof openHudModal === "function") openHudModal(titles[key], key, "settings");
      break;
    }
    default: break;
  }
}

function resumeListeningAfterReply() {
  if (voice.mode === "conversation" && !state.killEngaged && voice.micEnabled) {
    setTimeout(() => { if (!anyRunning()) voice.startListening(); }, 350);
  } else if (!anyRunning()) {
    setPresence("IDLE");
  }
}

function handlePresence(p) {
  voice.setPresenceState(p);
  document.body.classList.toggle("presence-sleeping", p.state === "sleeping" || p.state === "silenced");
  document.body.classList.toggle("presence-awake", p.state === "listening");
  const agentName = p.active_agent === "coded" ? "Coded" : "Phantom";
  const pill = $("presencePill");
  if (pill) {
    pill.textContent = p.state === "listening"
      ? `🟢 ${agentName} awake`
      : p.state === "silenced" ? "🤫 silent" : "💤 sleeping";
  }
  if (p.state === "listening") {
    setPresence("LISTENING", agentName + " is awake — listening for you");
  } else if (p.state === "sleeping") {
    setPresence("IDLE", "Phantom is here — say “Phantom” or “Coded” to wake me");
  } else if (p.state === "silenced") {
    setPresence("IDLE", "Staying silent — say my name to wake me");
  } else if (p.state === "killed") {
    setPresence("IDLE", "Kill switch engaged — everything stopped");
  }
}

async function loadPresence() {
  try {
    const p = await api("/api/presence");
    handlePresence(p);
  } catch (e) {}
}

function setContextLine(data) {
  const el = $("contextLine");
  if (!data) { el.classList.add("hidden"); el.textContent = ""; return; }
  el.classList.remove("hidden");
  el.textContent = (AGENTS[data.agent]?.emoji || "") + " " + (data.conversation_id ? "task in progress" : "working…");
}

/* ============================== MARKDOWN ============================== */
function renderMarkdown(src) {
  const s = String(src ?? "");
  let out = esc(s);
  out = out.replace(/```(\w*)\n?([\s\S]*?)```/g, (m, lang, code) => `<pre><code>${code.trim()}</code></pre>`);
  out = out.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  out = out.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/(^|\n)#{1,4} (.*)/g, (m, nl, t) => `${nl}<h4>${t}</h4>`);
  out = out.replace(/^[-*] (.*)$/gm, '<div class="li">• $1</div>');
  out = out.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  out = out.replace(/\n{2,}/g, "</p><p>");
  out = out.replace(/\n/g, "<br>");
  return `<p>${out}</p>`;
}

/* ============================== TRANSCRIPT ============================== */
function addMessageEl(role, content, container) {
  const host = container || $("messages");
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  const who = role === "user" ? (AGENTS[state.agent]?.emoji || "") + " you"
    : role === "assistant" ? (AGENTS[state.agent]?.emoji || "") + " " + (AGENTS[state.agent]?.name || "")
    : role === "error" ? "⚠️" : "";
  const body = role === "user" ? esc(content) : renderMarkdown(content);
  // voice-first: 🔊 replay button on every assistant bubble (and errors)
  const speakBtn = role !== "user" ? `<button class="speak-replay" title="Read aloud" onclick="speakText(this.parentElement.querySelector('.md')?.innerText || '')">🔊</button>` : "";
  div.innerHTML = `<div class="md">${who ? `<span class="muted small">${esc(who)} · </span>` : ""}${body}${speakBtn}</div>`;
  host.appendChild(div);
  host.scrollTop = host.scrollHeight;
  return div.querySelector(".md");
}
window.speakText = (text) => {
  const clean = String(text || "").replace(/🔊/g, "").trim();
  if (!clean) return;
  if (voice) voice.speak(clean, { priority: "high" });
  else { try { speechSynthesis.cancel(); speechSynthesis.speak(new SpeechSynthesisUtterance(clean)); } catch (e) {} }
};

function renderMessages(msgs) {
  // MAIN SCREEN: only the current chat — user + assistant turns, clean.
  // Tool cards / system noise / older history belong to the Chats menu.
  const host = $("messages");
  host.innerHTML = "";
  for (const m of msgs || []) {
    if (m.role === "tool" || m.role === "system") continue;
    if (m.role === "user") addMessageEl("user", m.content || "");
    else if (m.role === "assistant") addMessageEl("assistant", m.content || "…");
  }
  // CHATS MENU: the full transcript incl. tool cards for the same conv.
  renderFullTranscript(msgs);
}
function renderFullTranscript(msgs) {
  const full = $("fullTranscript");
  if (!full) return;
  full.innerHTML = "";
  for (const m of msgs || []) {
    if (m.role === "tool") continue;
    const el = document.createElement("div");
    el.className = "msg " + m.role;
    if (m.role === "user") el.innerHTML = `<div class="bubble user"><span class="who">You</span>${esc(m.content || "")}</div>`;
    else if (m.role === "assistant") el.innerHTML = `<div class="bubble assistant">${renderMarkdown(m.content || "…")}</div>`;
    else el.innerHTML = `<div class="muted small">${esc(m.content || "")}</div>`;
    full.appendChild(el);
  }
  full.scrollTop = full.scrollHeight;
}

let activeStreamEl = null;
function appendChunk(text) {
  if (!activeStreamEl) activeStreamEl = addMessageEl("assistant", "");
  activeStreamEl.innerHTML = renderMarkdown(activeStreamEl.textContent + text);
  const host = $("messages"); host.scrollTop = host.scrollHeight;
}
function finalizeAssistantMessage(data) {
  activeStreamEl = null;
  if (data.status === "cancelled") addMessageEl("system", "⏹ run stopped" + (data.error ? ` — ${data.error}` : ""));
  else if (data.status === "error") addMessageEl("error", data.error || "run failed");
  else if (!data.content) addMessageEl("assistant", "(no textual answer)");
  // voice-first: always read the final answer aloud (chunks may have been muted)
  if (data.content && state.voiceOn && !state.killEngaged) {
    flushSpeech();
    if (sentenceBuf.trim()) { voice.speak(sentenceBuf.trim()); sentenceBuf = ""; }
  }
}

function addToolCard(data, phase) {
  const div = document.createElement("div");
  div.className = `tool-card ${phase === "started" ? "" : "ok"}`;
  div.id = `tool-${data.tool_call_id || Math.random().toString(36).slice(2)}`;
  div.innerHTML = `<div class="tc-head">
      <span class="tc-status">${phase === "started" ? "◌" : "✓"}</span>
      <span class="tc-name">${esc(data.name)}</span><span class="tc-meta">▾</span></div>
    <div class="tc-body"><div class="tc-label">args</div><pre>${esc(JSON.stringify(data.arguments || {}))}</pre></div>`;
  div.querySelector(".tc-head").onclick = () => div.classList.toggle("open");
  $("messages").appendChild(div);
  $("messages").scrollTop = $("messages").scrollHeight;
}
function updateToolCard(data, ok) {
  const el = document.getElementById(`tool-${data.tool_call_id}`);
  if (!el) return;
  el.classList.toggle("ok", ok);
  el.classList.toggle("err", !ok);
  const st = el.querySelector(".tc-status"); if (st) st.textContent = ok ? "✓" : "✗";
  const body = el.querySelector(".tc-body");
  if (body) body.innerHTML = `<div class="tc-label">output</div><pre>${esc((data.output || data.message || "").slice(0, 3000))}</pre>`;
}

function showRunIndicator() {
  $("runIndicator")?.classList.remove("hidden");
  $("stopRun")?.classList.remove("hidden");
}
function hideRunIndicator() {
  $("runIndicator")?.classList.add("hidden");
  $("stopRun")?.classList.add("hidden");
}

/* ============================== CHAT ============================== */
async function sendMessage(text, opts = {}) {
  const clean = String(text || "").trim();
  // MULTI-TASK: only block if THIS agent is busy — Phantom can be working
  // while you ask Coded something, and vice versa.
  const target = (opts && opts.agent) || state.agent;
  if (!clean || agentRun(target).running) return;
  if (state.killEngaged) { toast("⛔ Kill switch is engaged"); return; }
  if (opts.via === "voice") {
    voice.stopListening(); // pause recognition while thinking/replying
  }
  if (target === state.agent) addMessageEl("user", clean);
  setPresence("THINKING");
  try {
    const convId = state.convs[target] || "";
    const res = await api(`/api/agents/${target}/chat`, {
      body: { text: clean, conversation_id: convId, session_id: "web" },
    });
    state.convs[target] = res.conversation_id;
    if (target === state.agent) state.currentConv = res.conversation_id;
    activeStreamEl = null;
    sentenceBuf = "";
    loadConversations();
  } catch (err) {
    setAgentDone(target);
    if (target === state.agent) { addMessageEl("error", `Could not start: ${err.message}`); setPresence("ERROR"); setTimeout(() => setPresence("IDLE"), 2500); }
    else toast("✗ " + (AGENTS[target]?.name || target) + ": " + err.message, "err");
  }
}

let convSearchTerm = "";
async function loadConversations() {
  const res = await api(`/api/conversations?agent=${state.agent}`);
  state.conversations = res.conversations || [];
  const list = $("convList");
  if (!list) return;
  list.innerHTML = "";
  const all = state.conversations.slice(0, 200);
  const term = convSearchTerm.toLowerCase();
  const filtered = term ? all.filter((c) => String(c.title || "").toLowerCase().includes(term)) : all;
  if (!filtered.length) {
    list.innerHTML = `<div class="muted small" style="padding:8px 2px">${term ? "No conversations match." : "No conversations yet — start a chat!"}</div>`;
    return;
  }
  const day = 24 * 3600 * 1000;
  const now = Date.now();
  const active = filtered.filter((c) => now - new Date(c.updated_at).getTime() < day);
  const past = filtered.filter((c) => now - new Date(c.updated_at).getTime() >= day);
  const renderGroup = (title, items) => {
    if (!items.length) return "";
    let html = `<div class="conv-group">${title} (${items.length})</div>`;
    for (const c of items) {
      const isActive = c.id === state.currentConv;
      const isRunning = state.running && isActive;
      html += `<div class="conv-item${isActive ? " active" : ""}" onclick="openConversation('${c.id}')">
        <span>${isRunning ? "● " : ""}${esc(c.title || "Untitled")}</span>
        <span class="conv-date">${timeAgo(c.updated_at)} · ${c.message_count || 0}</span></div>`;
    }
    return html;
  };
  list.innerHTML = renderGroup("Active", active) + renderGroup("Past", past);
}
// conversation search
const convSearchEl = $("convSearch");
if (convSearchEl) convSearchEl.addEventListener("input", (ev) => {
  convSearchTerm = ev.target.value.trim();
  loadConversations();
});
function openChatsMenu() {
  const d = $("drawer");
  if (d && d.classList.contains("hidden")) openDrawer();
  switchPanel("chats");
}
async function openConversation(id) {
  state.currentConv = id;
  const res = await api(`/api/conversations/${id}`);
  $("chatTitle").textContent = res.conversation.title || "Conversation";
  renderMessages(res.messages || []);
  loadConversations();
}
async function newConversation() {
  state.currentConv = null;
  $("chatTitle").textContent = "New conversation";
  renderMessages([]);
}

function switchPersona(agent) {
  if (agentRun(agent).running) { toast(`${AGENTS[agent]?.name || agent} is working — wait for it to finish`); return; }
  state.agent = agent;
  voice.currentAgent = agent;   // replies use this persona's voice
  document.querySelectorAll(".seg").forEach((b) => b.classList.toggle("active", b.dataset.persona === agent));
  $("identityName").textContent = "Phantom"; // product name stays Phantom
  $("chatAgentFace").textContent = AGENTS[agent].emoji;
  $("chatAgentName").textContent = AGENTS[agent].name;
  $("input").placeholder = `Type to ${AGENTS[agent].name}…`;
  state.currentConv = null;
  renderMessages([]);
  loadConversations();
}

/* ============================== DRAWER ============================== */
state.currentPanel = "activity";
function openDrawer(panel) {
  $("drawer").classList.remove("hidden");
  switchPanel(panel || state.currentPanel || "activity");
}
function closeDrawer() { $("drawer").classList.add("hidden"); }
function switchPanel(panel) {
  state.currentPanel = panel;
  document.querySelectorAll(".panel-btn").forEach((b) => b.classList.toggle("active", b.dataset.panel === panel));
  document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("active", p.id === `panel-${panel}`));
  $("drawerTitle").textContent = panel[0].toUpperCase() + panel.slice(1);
  if (panel === "today") loadToday();
  if (panel === "memory") loadMemories();
  if (panel === "wellness") loadWellness();
  if (panel === "health") loadHealth();
  if (panel === "tasks") loadTasks();
  if (panel === "audit") { loadAuditEvents(); loadAudit(); }
  if (panel === "permissions") loadPermissions();
  if (panel === "approvals") loadApprovals();
  if (panel === "settings") loadSettings();
}

/* ============================== TODAY (briefing + monitor) ============================== */
async function loadToday(force) {
  try {
    const briefing = await api(`/api/briefing${force ? "?force=true" : ""}`);
    renderBriefing(briefing);
    const monitor = await api("/api/monitor");
    renderMonitor(monitor);
    const cfg = await api("/api/briefing/config").catch(() => null);
    if (cfg && $("briefingTime")) $("briefingTime").value = cfg.time;
  } catch (e) { /* backend not ready yet */ }
}
async function saveBriefingTime() {
  const t = $("briefingTime").value;
  if (!t) return;
  try {
    const res = await api("/api/briefing/config", { method: "PUT", body: { time: t } });
    toast(`✓ Briefing moved to ${res.time} — schedules updated`, "ok");
    loadToday();
  } catch (e) { toast("Error: " + e.message); }
}
function applyTheme(theme) {
  document.body.dataset.theme = theme === "yellow" ? "yellow" : "midnight";
  localStorage.setItem("phantom.theme", theme === "yellow" ? "yellow" : "midnight");
  _saveUiPref("ui.theme", theme === "yellow" ? "yellow" : "midnight");
}
function renderBriefing(b) {
  const top = $("briefingTop");
  top.innerHTML = "";
  for (const group of b.top_priorities || []) {
    const g = document.createElement("div");
    g.className = "card";
    const lvlCls = group.level === "high" ? "err" : group.level === "in_progress" ? "warn" : "info";
    g.innerHTML = `<div class="card-title"><span class="pill ${lvlCls}">${esc(group.level)}</span></div>`;
    for (const item of group.items) {
      const d = document.createElement("div");
      d.className = "card-body";
      d.innerHTML = `<b>${esc(item.title)}</b>${item.detail ? ` — ${esc(item.detail)}` : ""}`;
      g.appendChild(d);
    }
    top.appendChild(g);
  }
  if (!(b.top_priorities || []).length) top.innerHTML = `<div class="card muted">Nothing urgent today.</div>`;

  const sections = $("briefingSections");
  sections.innerHTML = "";
  for (const [key, s] of Object.entries(b.sections || {})) {
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `<div class="card-title">${esc(s.title)}</div>
      <div class="card-body small">${esc((s.items || []).join(" · "))}</div>`;
    sections.appendChild(card);
  }
}
function renderMonitor(m) {
  const el = $("monitorCards");
  el.innerHTML = "";
  const sys = m.system || {};
  const mk = (t, v, cls) => {
    const d = document.createElement("div");
    d.className = "card";
    d.innerHTML = `<div class="card-title"><span class="pill ${cls}">${esc(t)}</span></div>
      <div class="card-body small">${esc(v)}</div>`;
    el.appendChild(d);
  };
  mk("CPU", `${sys.cpu_percent ?? "—"}%`, sys.cpu_percent > 80 ? "err" : "ok");
  mk("Memory", `${sys.memory_percent ?? "—"}%`, sys.memory_percent > 85 ? "err" : "ok");
  mk("Disk", `${sys.disk_percent ?? "—"}%`, sys.disk_percent > 90 ? "err" : "ok");
  if (sys.battery) mk("Battery", `${sys.battery.percent}%${sys.battery.plugged ? " ⚡" : ""}`, sys.battery.percent < 20 && !sys.battery.plugged ? "warn" : "ok");
  const t = m.tasks || {};
  if (t.counts && t.counts.failed) mk("Failed tasks", String(t.counts.failed), "err");
  const s = m.signals || {};
  if (s.tool_failures_24h) mk("Tool failures 24h", String(s.tool_failures_24h), "warn");
}
function speakBriefing() {
  api("/api/briefing").then((b) => { if (b.spoken && state.voiceOn) voice.speak(b.spoken); });
}

/* ============================== ACTIVITY ============================== */
function addActivity(kind, text, cls) {
  const feed = $("activityFeed");
  if (!feed) return;
  const div = document.createElement("div");
  div.className = `activity-item ${cls || ""}`;
  div.innerHTML = `<div class="a-time">${new Date().toLocaleTimeString()}</div><div class="a-text">${esc(text)}</div>`;
  feed.prepend(div);
  while (feed.children.length > 60) feed.lastChild.remove();
}

/* ============================== CONFIRMATION ============================== */
function showConfirmation(data) {
  const c = data.confirmation || {};
  $("confirmAgent").textContent = `${AGENTS[data.agent]?.emoji || ""} ${AGENTS[data.agent]?.name || c.agent} is asking for permission`;
  $("confirmWhat").textContent = data.what || `Run tool ${c.tool_name}`;
  $("confirmWhy").textContent = data.why || c.reason || "";
  $("confirmAffected").textContent = data.affected || c.impact || "";
  $("confirmAction").textContent = data.action || `${c.tool_name}(${JSON.stringify(c.arguments || {})})`;
  $("confirmRisk").textContent = data.risk || c.risk || "";
  $("confirmModal").classList.remove("hidden");
  $("confirmModal").dataset.cid = c.id;
}
function hideConfirmation(cid) {
  if (!cid || $("confirmModal").dataset.cid === cid) $("confirmModal").classList.add("hidden");
}
async function decideConfirmation(approve) {
  const cid = $("confirmModal").dataset.cid;
  if (!cid) return;
  try {
    await api(`/api/confirmations/${cid}/${approve ? "approve" : "deny"}`);
    toast(approve ? "✅ Approved" : "⛔ Denied");
  } catch (e) { toast("Error: " + e.message); }
  hideConfirmation(cid);
}

/* ============================== PANELS (unchanged logic) ============================== */
async function loadMemories() {
  const agent = $("memAgent").value, kind = $("memKind").value;
  const q = $("memSearch").value.trim();
  const res = q ? await api(`/api/memories/search?agent=${agent}&q=${encodeURIComponent(q)}`)
                : await api(`/api/memories?agent=${agent}&kind=${encodeURIComponent(kind)}`);
  const list = $("memoryList"); list.innerHTML = "";
  const rows = res.memories || [];
  if (!rows.length) { list.innerHTML = `<div class="card muted">No memories yet.</div>`; return; }
  for (const m of rows) {
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `
      <div class="card-title">🧠 ${esc(m.content.slice(0, 110))}</div>
      <div class="card-meta">
        <span class="pill info">${esc(m.kind)}</span>
        <span class="pill ${m.agent === "shared" ? "warn" : "ok"}">${m.agent === "shared" ? "shared" : esc(m.agent)}</span>
        <span>imp ${Number(m.importance).toFixed(2)}</span><span>${timeAgo(m.updated_at)}</span>
      </div>
      <div class="card-body">${esc(m.content)}</div>
      <div class="card-actions">
        <button class="mini-btn" onclick="forgetMemory('${m.id}')">Forget</button>
        <button class="mini-btn" onclick="deleteMemory('${m.id}')">Delete</button>
      </div>`;
    list.appendChild(card);
  }
}
async function forgetMemory(id) { await api(`/api/memories/${id}/forget`); loadMemories(); }
async function deleteMemory(id) { await api(`/api/memories/${id}`, { method: "DELETE" }); loadMemories(); }
async function addMemory() {
  const content = $("memNewContent").value.trim();
  if (!content) return;
  await api("/api/memories", { body: { agent: $("memAgent").value, content, kind: $("memKind").value || "fact" } });
  $("memNewContent").value = ""; loadMemories(); toast("Memory stored");
}

async function loadWellness() {
  const status = await api("/api/health/status").catch(() => null);
  if (!status) return;
  const banner = $("wellnessBanner");
  if (!status.enabled) {
    banner.classList.remove("hidden");
    banner.innerHTML = `<div class="card-title">🩺 Health Brain is disabled</div><div class="card-body small">Records remain encrypted and untouched.</div>`;
    $("wellnessEnable").classList.remove("hidden");
    $("wellnessDisable").classList.add("hidden");
    $("wellnessToday").innerHTML = `<div class="card muted">Health Brain disabled.</div>`;
    return;
  }
  banner.classList.add("hidden");
  $("wellnessEnable").classList.add("hidden");
  $("wellnessDisable").classList.remove("hidden");
  $("wellnessPrivacy").textContent = `🔒 ${status.privacy.encryption.split(";")[0]} · ${status.vault_records} records`;
  const [today, memories, routines] = await Promise.all([
    api("/api/health/today"), api("/api/health/memories?limit=50"), api("/api/health/routines"),
  ]);
  renderWellnessToday(today);
  renderWellnessMemories(memories.records || []);
  renderWellnessRoutines(routines.routines || {});
}
function renderWellnessToday(t) {
  const el = $("wellnessToday"); el.innerHTML = "";
  const mk = (title, body) => {
    const d = document.createElement("div"); d.className = "card";
    d.innerHTML = `<div class="card-title">${title}</div><div class="card-body">${body}</div>`;
    el.appendChild(d);
  };
  mk("💧 Hydration", `${t.hydration.liters}L / ${t.hydration.goal_liters}L (${t.hydration.pct}%)`);
  mk("🍎 Nutrition", `${t.nutrition.meals_logged} of ${t.nutrition.goal_meals} meals`);
  mk("🏃 Activity", `${t.activity.sessions} session(s) · ${t.activity.steps} steps`);
  mk("😴 Sleep", t.sleep.value != null ? `${t.sleep.value}h` : "not recorded");
  mk("💊 Medications", t.medications.length ? t.medications.map(m => m.title || m.name).join(", ") : "none");
  mk("📅 Appointments", t.appointments.length ? t.appointments.map(a => a.title || a.name).join(", ") : "none");
}
function renderWellnessMemories(records) {
  const el = $("wellnessMemories"); el.innerHTML = "";
  if (!records.length) { el.innerHTML = `<div class="card muted">No health records yet.</div>`; return; }
  for (const r of records) {
    const d = document.createElement("div"); d.className = "card";
    d.innerHTML = `<div class="card-title">🩺 ${esc(r.category)} <span class="pill info">${esc(r.title || "")}</span>
      <span class="muted small">${esc((r.recorded_at || "").slice(0, 16))}</span></div>
      <div class="card-body small">${esc(JSON.stringify(r.data || {}))}</div>
      <div class="card-actions"><button class="mini-btn" onclick="wellnessDelete('${r.id}')">Delete</button></div>`;
    el.appendChild(d);
  }
}
function renderWellnessRoutines(routines) {
  const el = $("wellnessRoutines");
  el.innerHTML = Object.entries(routines || {}).map(([b, items]) =>
    `<div class="card-title" style="margin-top:6px">${b.charAt(0).toUpperCase() + b.slice(1)}</div>
     <div class="card-body small">${esc((items || []).join(" · ") || "(empty)")}</div>`).join("") ||
    `<div class="card muted">No routines set.</div>`;
}
async function wellnessLog() {
  const type = $("wellnessLogType").value, a = $("wellnessLogA").value.trim(),
        b = $("wellnessLogB").value.trim(), c = $("wellnessLogC").value.trim();
  let body = {};
  if (type === "measurement") body = { category: "measurement", title: `${a} ${b}${c}`, data: { metric: a, value: parseFloat(b) || 0, unit: c } };
  else if (type === "symptom") body = { category: "symptom", title: a, data: { symptom: a, severity: parseInt(b, 10) || 5, duration: c } };
  else if (type === "habit") body = { category: "habit", title: `${a} ${b}${c}`, data: { kind: a, amount: parseFloat(b) || 0, unit: c } };
  else if (type === "medication") body = { category: "medication", title: a, data: { name: a, dose: b, schedule: c } };
  else if (type === "goal") body = { category: "goal", title: a, data: { goal: a, target: b } };
  else body = { category: "appointment", title: a, data: { when: b, provider: c } };
  await api("/api/health/memories", { body });
  const flagEl = $("wellnessRedFlag");
  if (type === "symptom" || type === "measurement") {
    const check = await api("/api/health/redflag", { body: type === "symptom"
      ? { symptom: a, severity: parseInt(b, 10) || 0 } : { metric: a, value: parseFloat(b) || 0, unit: c } });
    if (check.red_flag) {
      flagEl.classList.remove("hidden");
      flagEl.innerHTML = `<div class="card-title">${check.red_flag.level === "emergency" ? "🚨 URGENT" : "⚠️ Note"}</div><div class="card-body">${esc(check.text)}</div>`;
    } else flagEl.classList.add("hidden");
  } else flagEl.classList.add("hidden");
  $("wellnessLogA").value = ""; $("wellnessLogB").value = ""; $("wellnessLogC").value = "";
  loadWellness();
}
async function wellnessTrends() {
  const t = await api(`/api/health/trends?metric=${$("wellnessTrendMetric").value}&limit=14`);
  const el = $("wellnessTrends");
  if (!t.points || !t.points.length) { el.textContent = "No records yet."; return; }
  const lines = t.points.slice(-10).map(p => `${p.value} ${p.unit} (${p.when.slice(0, 16)})`);
  if (t.trend_vs_previous != null) lines.push(`trend: ${t.trend_vs_previous > 0 ? "up" : "down"} ${Math.abs(t.trend_vs_previous)} ${t.points[t.points.length - 1].unit} (not a diagnosis)`);
  el.textContent = lines.join("\n");
}
async function wellnessMemories() {
  const res = await api(`/api/health/memories?category=${encodeURIComponent($("wellnessMemCategory").value)}&q=${encodeURIComponent($("wellnessMemSearch").value.trim())}&limit=100`);
  renderWellnessMemories(res.records || []);
}
async function wellnessDelete(id) { await api(`/api/health/memories/${id}/delete`); wellnessMemories(); }
async function wellnessClear() {
  const category = $("wellnessMemCategory").value;
  if (!confirm(category ? `Clear ALL ${category} records?` : "Clear the ENTIRE Health Memory?")) return;
  await api("/api/health/clear", { body: { category } });
  wellnessMemories(); loadWellness();
}
async function wellnessExport() {
  const data = await api("/api/health/export");
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `phantom-health-${new Date().toISOString().slice(0, 10)}.json`;
  a.click(); URL.revokeObjectURL(a.href);
  toast("Health data exported");
}
async function wellnessToggle(enable) {
  await api("/api/health/enable", { body: { enabled: enable } });
  loadWellness();
}
window.wellnessLog = wellnessLog; window.wellnessTrends = wellnessTrends;
window.wellnessMemories = wellnessMemories; window.wellnessDelete = wellnessDelete;
window.wellnessClear = wellnessClear; window.wellnessExport = wellnessExport;

async function loadHealth() {
  const [brains, graph, proposals, snapshots] = await Promise.all([
    api("/api/brains").catch(() => ({ brains: [] })),
    api("/api/graph").catch(() => ({ stats: {} })),
    api("/api/proposals").catch(() => ({ proposals: [] })),
    api("/api/snapshots").catch(() => ({ snapshots: [] })),
  ]);
  const stats = graph.stats || {};
  $("graphStats").textContent = `nodes: ${stats.nodes || 0} · edges: ${stats.edges || 0}`;
  const bl = $("brainList"); bl.innerHTML = "";
  for (const b of (brains.brains || [])) {
    const stateCls = b.health_state === "online" ? "ok" : b.health_state === "degraded" ? "warn" : "info";
    const d = document.createElement("div"); d.className = "card";
    d.innerHTML = `<div class="card-title">🧬 ${esc(b.name)} <span class="muted small">${esc(b.id)}</span>
      <span class="pill ${stateCls}">${esc(b.health_state || "?")}</span><span class="pill info">v${b.version || 1}</span></div>
      <div class="card-meta"><span>${esc(b.model)}</span><span>role: ${esc(b.role)}</span>
      <span>success: ${b.success_rate != null ? (b.success_rate * 100).toFixed(0) + "%" : "—"}</span>
      <span>lat: ${b.avg_latency_ms != null ? b.avg_latency_ms.toFixed(0) + "ms" : "—"}</span></div>`;
    bl.appendChild(d);
  }
  if (!(brains.brains || []).length) bl.innerHTML = `<div class="card muted">No brains.</div>`;
  const pl = $("proposalList"); pl.innerHTML = "";
  for (const p of (proposals.proposals || [])) {
    const d = document.createElement("div"); d.className = "card";
    d.innerHTML = `<div class="card-title">💡 ${esc(p.title)} <span class="pill ${p.status === "deployed" ? "ok" : p.status === "proposed" ? "warn" : "info"}">${esc(p.status)}</span>
      <span class="pill ${p.risk === "high" ? "err" : "info"}">${esc(p.risk)}</span></div>
      <div class="card-body small">${esc(p.description)}</div>
      <div class="card-actions">${p.status === "proposed" ? `<button class="mini-btn" onclick="approveProposal('${p.id}')">Approve & deploy</button>
      <button class="mini-btn" onclick="rejectProposal('${p.id}')">Reject</button>` : ""}</div>`;
    pl.appendChild(d);
  }
  if (!(proposals.proposals || []).length) pl.innerHTML = `<div class="card muted">No proposals.</div>`;
  const sl = $("snapshotList"); sl.innerHTML = "";
  for (const s of (snapshots.snapshots || [])) {
    const d = document.createElement("div"); d.className = "card";
    d.innerHTML = `<div class="card-title">📸 ${esc(s.label)} ${s.restored_at ? `<span class="pill warn">restored</span>` : ""}</div>
      <div class="card-actions"><button class="mini-btn" onclick="restoreSnapshot('${s.id}')">Restore</button></div>`;
    sl.appendChild(d);
  }
  if (!(snapshots.snapshots || []).length) sl.innerHTML = `<div class="card muted">No snapshots.</div>`;
}
window.approveProposal = async (id) => { const r = await api(`/api/proposals/${id}/approve`); toast(`Proposal ${r.status}`); loadHealth(); };
window.rejectProposal = async (id) => { await api(`/api/proposals/${id}/reject`); loadHealth(); };
window.restoreSnapshot = async (id) => { await api(`/api/snapshots/${id}/restore`, { body: { reason: "manual" } }); toast("Snapshot restored"); loadHealth(); };
async function runLoopTask() {
  const objective = $("loopObjective").value.trim();
  if (!objective) return;
  await api("/api/loops/run", { body: { agent: $("loopAgent").value, objective, max_iterations: 4, failure_threshold: 2, rollback: true } });
  $("loopResult").classList.remove("hidden");
  $("loopResult").textContent = "Loop task started — watch Tasks.";
  toast("Loop launched");
}
async function runSelfAudit() {
  const res = await api("/api/evolution/audit", { body: { period: "daily" } });
  const card = document.createElement("div"); card.className = "card";
  card.innerHTML = `<div class="card-title">📋 Daily self-audit</div><div class="card-body mono small">${esc(res.report)}</div>`;
  $("proposalList").prepend(card);
}
window.runLoopTask = runLoopTask;

async function loadTasks() {
  const res = await api(`/api/tasks?agent=${encodeURIComponent($("taskAgent").value)}&status=${encodeURIComponent($("taskStatus").value)}`);
  const list = $("taskList"); const rows = res.tasks || [];
  list.innerHTML = "";
  if (!rows.length) { list.innerHTML = `<div class="card muted">No tasks.</div>`; return; }
  for (const t of rows) {
    const cls = t.status === "completed" ? "ok" : t.status === "running" ? "warn" : t.status === "failed" ? "err" : "info";
    const d = document.createElement("div"); d.className = "card";
    d.innerHTML = `<div class="card-title">${AGENTS[t.agent]?.emoji || ""} ${esc(t.name)}
      <span class="pill ${cls}">${esc(t.status)}</span></div>
      <div class="card-meta"><span>${timeAgo(t.created_at)}</span>${t.error ? `<span class="muted">${esc(t.error)}</span>` : ""}</div>
      <div class="card-actions">${(t.status === "running" || t.status === "queued") ? `<button class="mini-btn" onclick="cancelTask('${t.id}')">Cancel</button>` : ""}</div>`;
    list.appendChild(d);
  }
}
window.cancelTask = async (id) => { await api(`/api/tasks/${id}/cancel`); loadTasks(); };

async function loadAuditEvents() {
  const res = await api("/api/audit/events");
  $("auditEvent").innerHTML = `<option value="">All events</option>` +
    (res.events || []).map((e) => `<option value="${esc(e)}">${esc(e)}</option>`).join("");
}
async function loadAudit() {
  const res = await api(`/api/audit?agent=${encodeURIComponent($("auditAgent").value)}&event=${encodeURIComponent($("auditEvent").value)}&limit=250`);
  const list = $("auditList"); const rows = res.events || [];
  list.innerHTML = "";
  if (!rows.length) { list.innerHTML = `<div class="card muted">No audit events.</div>`; return; }
  for (const r of rows) {
    const d = document.createElement("div"); d.className = "card";
    d.innerHTML = `<div class="card-title"><span class="pill info">${esc(r.event)}</span>
      <span class="muted small">${esc(r.agent || "*")} · ${esc(r.ts || "")}</span>
      ${r.latency_ms != null ? `<span class="muted small">${Number(r.latency_ms).toFixed(0)}ms</span>` : ""}</div>
      <div class="card-body mono small">${esc(JSON.stringify(r.detail || {}).slice(0, 900))}</div>`;
    list.appendChild(d);
  }
}

async function loadApprovals() {
  try {
    const res = await api("/api/confirmations");
    const list = res.confirmations || [];
    const badge = $("approvalsBadge");
    if (badge) {
      badge.textContent = list.length;
      badge.classList.toggle("hidden", list.length === 0);
    }
    const el = $("approvalsList");
    if (!el) return;
    el.innerHTML = "";
    if (!list.length) {
      el.innerHTML = `<div class="muted small">No pending approvals — everything is decided. 🎉</div>`;
      return;
    }
    for (const c of list) {
      const card = document.createElement("div");
      card.className = "card";
      const agentEmoji = AGENTS[c.agent]?.emoji || "🤖";
      const args = Object.entries(c.arguments || {}).slice(0, 4)
        .map(([k, v]) => `<span class="muted small">${esc(k)}: ${esc(String(v).slice(0, 80))}</span>`).join(" · ");
      card.innerHTML = `
        <div class="card-title">${agentEmoji} ${esc(c.agent)} · <span class="pill warn">${esc(c.tool_name)}</span>
          <span class="muted small">${timeAgo(c.requested_at)}</span></div>
        <div class="muted small" style="margin:4px 0">${esc(c.reason || "")}</div>
        ${args ? `<div class="muted small mono" style="margin:4px 0">${args}</div>` : ""}
        <div class="muted small" style="margin:2px 0">Impact: ${esc(c.impact || "")} · Risk: ${esc(c.risk || "")}</div>
        <div class="btnRow" style="margin-top:8px">
          <button class="btn btn-primary" onclick="decideApproval('${c.id}', true)">Approve</button>
          <button class="btn btn-danger" onclick="decideApproval('${c.id}', false)">Deny</button>
        </div>`;
      el.appendChild(card);
    }
  } catch (e) {
    const el = $("approvalsList");
    if (el) el.innerHTML = `<div class="muted small">Could not load approvals: ${esc(e.message)}</div>`;
  }
}
window.decideApproval = async (cid, approve) => {
  try {
    await api(`/api/confirmations/${cid}/${approve ? "approve" : "deny"}`, { method: "POST", body: { decided_by: "user" } });
    toast(approve ? "✓ Approved" : "✗ Denied", approve ? "ok" : "err");
    loadApprovals();
    hideConfirmation(cid);
  } catch (e) { toast("✗ " + e.message, "err"); }
};

async function loadPermissions() {
  const agent = $("permAgent").value;
  const res = await api("/api/permissions");
  const rows = (res.agents && res.agents[agent]) || [];
  const list = $("permTable"); list.innerHTML = "";
  const head = document.createElement("div"); head.className = "card perm-row head";
  head.innerHTML = `<div class="col">tool</div><div class="col">default</div><div class="col">effective</div><div class="col">override</div><div class="col">description</div>`;
  list.appendChild(head);
  for (const t of rows) {
    const d = document.createElement("div"); d.className = "card perm-row";
    d.innerHTML = `<div class="col mono">${esc(t.tool)}</div>
      <div class="col"><span class="pill info">${esc(t.default)}</span></div>
      <div class="col"><span class="pill ${t.effective === "blocked" ? "err" : t.effective === "confirm_required" ? "warn" : t.effective === "read_only" ? "ok" : "info"}">${esc(t.effective)}</span></div>
      <div class="col"><select class="perm-override" data-tool="${esc(t.tool)}">
        <option value="">default</option>
        ${["read_only", "safe_action", "confirm_required", "high_risk", "blocked"].map((l) => `<option value="${l}" ${t.override === l ? "selected" : ""}>${l}</option>`).join("")}
      </select></div>
      <div class="col small muted">${esc(t.description)}</div>`;
    list.appendChild(d);
  }
  list.querySelectorAll(".perm-override").forEach((sel) => {
    sel.onchange = async () => {
      const tool = sel.dataset.tool, level = sel.value;
      try {
        if (!level) await api("/api/permissions", { method: "PUT", body: { agent, tool, delete: true } });
        else await api("/api/permissions", { method: "PUT", body: { agent, tool, level } });
        toast("✓ Permission saved", "ok");
      } catch (e) { toast("✗ " + e.message, "err"); }
      loadPermissions();
    };
  });
}

async function loadSettings() {
  const res = await api("/api/settings");
  const keys = res.keys || {};
  const body = $("settingsBody");
  const sections = [];
  for (const agentId of ["phantom", "coded", "health"]) {
    const k = keys[agentId] || {};
    const meta = AGENTS[agentId] || { emoji: "🩺", name: "Health" };
    sections.push(`
      <div class="settings-section"><h3>${meta.emoji} ${meta.name} — AI</h3>
        <div class="row"><label>NVIDIA API key (${esc(k.env || "")})</label>
          <input type="password" id="key-${agentId}" placeholder="${k.configured ? `configured (${esc(k.masked || "")}) — type to replace` : "not set"}">
          <button class="btn" onclick="saveKey('${agentId}')">Save</button>
          <button class="btn" onclick="testKey('nvidia', '${agentId}')">Test</button>
          ${k.configured ? `<span class="muted small">${esc(k.masked || "")}</span>` : ""}</div>
        <div class="row"><label>Model preset</label>
          <select id="modelPreset-${agentId}" onchange="applyModelPreset('${agentId}')">
            <option value="fast">⚡ Fast (8B — snappy)</option>
            <option value="smart" selected>🧠 Smart (70B — default)</option>
            <option value="custom">✏️ Custom</option>
          </select>
          <span class="muted small">Fast replies use meta/llama-3.1-8b-instruct</span></div>
        <div class="row"><label>Model</label><input type="text" id="model-${agentId}" placeholder="custom model ID" title="NVIDIA model ID, e.g. meta/llama-3.3-70b-instruct or meta/llama-3.1-8b-instruct. Empty = default."></div>
        <div class="row"><label>Temperature</label><input type="text" id="temp-${agentId}" style="width:80px" title="Creativity/randomness of the model: 0 = strict &amp; factual, higher (up to 2) = more creative &amp; varied. Default 0.4 — a balanced, dependable personality."></div>
      </div>
      ${agentId === "phantom" ? `<div class="row" style="margin-top:6px"><label>⚡ General mode</label>
        <select id="generalModeSel"><option value="0">off — ask before routine actions</option><option value="1">on — just do it (no confirm popups for routine things)</option></select>
        <span class="muted small">Open apps/URLs, edit files, close processes without asking. Destructive actions stay protected.</span></div>` : ""}`);
  }
  sections.push(`
    <div class="settings-section"><h3>🎙️ Voice</h3>
      <div class="row"><label>STT provider</label>
        <select id="sttProvider"><option value="server">auto (Deepgram → Groq → local)</option><option value="browser">browser (offline)</option><option value="deepgram">deepgram (online)</option></select></div>
      <div class="row"><label>TTS provider</label>
        <select id="ttsProvider"><option value="server">auto (Deepgram Aura → cloud → local)</option><option value="browser">browser (system voices)</option><option value="deepgram">deepgram Aura (online)</option></select></div>
      <div class="row"><label>STT priority</label>
        <input type="text" id="sttPriority" placeholder="deepgram, groq, local_whisper" style="flex:1"></div>
      <div class="row"><label>TTS priority</label>
        <input type="text" id="ttsPriority" placeholder="deepgram, cloud, local" style="flex:1"></div>
      <div class="row"><label>Local Whisper model</label>
        <select id="localModelSel"><option value="tiny">tiny (fastest, ~39 MB)</option><option value="base" selected>base (good, ~74 MB)</option><option value="small">small (better, ~460 MB)</option><option value="medium">medium (slow)</option></select></div>
      <div class="row"><label>Voice activity threshold</label>
        <input type="range" id="vadThreshold" min="0.005" max="0.12" step="0.005" style="flex:1">
        <span id="vadThresholdLbl" class="muted small" style="width:60px"></span></div>
      <div class="row"><label>Auto-stop after silence (ms)</label>
        <input type="number" id="autoStopMs" min="300" max="5000" step="100" style="width:120px"></div>
      <div class="row"><label>Max recording (ms)</label>
        <input type="number" id="maxRecordMs" min="2000" max="60000" step="500" style="width:120px"></div>
      <div class="row"><label>Continuous listening</label>
        <select id="continuousSel"><option value="1">on</option><option value="0">off</option></select></div>
      <div class="row"><label>👻 Phantom voice</label>
        <select id="voicePhantom"><option value="">default (calm male)</option></select></div>
      <div class="row"><label>💻 Coded voice</label>
        <select id="voiceCoded"><option value="">default (sharp male)</option></select></div>
      <div class="row"><label>Voice mode</label>
        <select id="voiceModeSel"><option value="private">private</option><option value="push">push-to-talk</option><option value="conversation">conversation</option></select></div>
      <div class="row"><label>Proactive speech</label>
        <select id="proactiveSel"><option value="0">off</option><option value="1">on</option></select></div>
      <div class="row"><label>🎤 Test voice system</label>
        <button class="btn" onclick="testVoicePipeline()">Test Voice System</button>
        <span class="muted small">records 2s → STT → Phantom → TTS → speaker</span></div>
      <div id="voiceStatusBox"></div>
      <div class="row"><label>Deepgram API key</label>
        <input type="password" id="deepgramKey" placeholder="${res.voice?.deepgram_masked ? `configured (${esc(res.voice.deepgram_masked)}) — type to replace` : "not set"}">
        <button class="btn" onclick="saveDeepgram()">Save</button>
        <button class="btn" onclick="testKey('deepgram')">Test</button>
        ${res.voice?.deepgram_masked ? `<span class="muted small">${esc(res.voice.deepgram_masked)}</span>` : ""}</div>
      <div class="row"><label>Groq API key (Whisper STT)</label>
        <input type="password" id="groqKey" placeholder="${res.voice?.groq_masked ? `configured (${esc(res.voice.groq_masked)}) — type to replace` : "not set (free at groq.com)"}">
        <button class="btn" onclick="saveGroq()">Save</button>
        <button class="btn" onclick="testKey('groq')">Test</button>
        ${res.voice?.groq_masked ? `<span class="muted small">${esc(res.voice.groq_masked)}</span>` : ""}</div>
      <p class="muted small">Keys stay server-side (chmod-600 file); the UI never sees the full value — only a masked hint like <span class="mono">dg_••••</span>. Deepgram aura voices need a Deepgram key; Groq Whisper gives a fast cloud STT fallback.</p>
    </div>
    <div class="settings-section"><h3>🔁 Heartbeat & quiet hours</h3>
      <div class="row"><label>Quiet hours start (UTC)</label><input id="quietStart" placeholder="22:00"></div>
      <div class="row"><label>Quiet hours end (UTC)</label><input id="quietEnd" placeholder="07:00"></div>
    </div>
    <div class="settings-section"><h3>🗓️ Schedules</h3><div id="schedList"></div>
      <div class="row" style="margin-top:8px">
        <input id="schedName" placeholder="name" style="width:120px">
        <input id="schedExpr" placeholder="every 30m | daily at 09:00" style="width:180px">
        <input id="schedPrompt" placeholder="prompt" style="flex:1">
        <button class="btn btn-primary" onclick="addSchedule()">Add</button></div>
    </div>
    <div class="settings-section"><h3>🔊 Voice enrollment (speaker lock)</h3>
      <p class="muted small">Only your voice should wake Phantom and Coded. Record 3 short phrases (2s each) for each agent.</p>
      <div id="enrollHint" class="hidden"></div>
      <div class="row"><label>Enroll Phantom</label>
        <span id="enrollPhantomStatus" class="muted">…</span>
        <button id="enrollPhantomBtn" class="btn">🎙 Enroll</button></div>
      <div class="row"><label>Enroll Coded</label>
        <span id="enrollCodedStatus" class="muted">…</span>
        <button id="enrollCodedBtn" class="btn">🎙 Enroll</button></div>
      <div class="row"><label>Speaker lock</label>
        <input type="checkbox" id="speakerLockCb"> <span class="muted small">only my voice wakes them</span></div>
      <div class="row"><label>Clear enrollment</label>
        <button id="enrollClearBtn" class="btn btn-danger">Clear</button></div>
    </div>
    <div class="settings-section"><h3>🧠 Brains & API keys</h3>
      <p class="muted small">Each of the 31 specialist brains can have its OWN API key + model for efficient routing. Leave a key empty to share Phantom's.</p>
      <div id="brainKeys"></div>
    </div>
    <div class="settings-section"><h3>🎨 Appearance</h3>
      <div class="row"><label>Theme</label>
        <select id="themeSel">
          <option value="midnight">Midnight (default)</option>
          <option value="yellow">JOOJO Yellow #FFD600</option>
        </select></div>
    </div>
    <div class="settings-section"><h3>⬆ App updates</h3>
      <div class="row"><label>Desktop app</label>
        <span id="updateVer" class="muted small"></span>
        <span id="updateState" class="muted">not checked</span></div>
      <div class="row">
        <button id="updateCheckBtn" class="btn" onclick="checkForUpdates()">Check for updates</button>
        <button id="updateDownloadBtn" class="btn hidden" onclick="updaterDownload()">⬇ Download</button>
        <button id="updateInstallBtn" class="btn btn-primary hidden" onclick="updaterInstall()">Restart &amp; install</button>
        <button id="updateReleaseBtn" class="btn hidden" onclick="updaterOpenReleases()">Open releases page</button>
      </div>
    </div>
    <div class="settings-section"><h3>☁️ Portable Phantom (cloud)</h3>
      <p class="muted small">The always-on cloud Phantom for when your PC is off. Set your Worker URL + Cloud token,
        then add the cloud NVIDIA/Deepgram keys here (stored in Cloudflare's secret store, masked, never shown).</p>
      <div class="row"><label>Worker URL</label><input type="text" id="cloudUrl" placeholder="https://phantom-portable.xxx.workers.dev"></div>
      <div class="row"><label>Cloud token 🔒</label><input type="password" id="cloudToken" placeholder="the PHANTOM_CLOUD_TOKEN you set at deploy"></div>
      <div class="row"><label>Test connection</label><button class="btn" onclick="testKey('cloud')">Test</button>
        <span class="muted small">checks the worker is reachable and the token works</span></div>
      <div class="row"><label>Cloud NVIDIA key</label><input type="password" id="cloudNvidia" placeholder="same NVIDIA key as your PC (nvapi-…)"></div>
      <div class="row"><label>Cloud Deepgram key</label><input type="password" id="cloudDeepgram" placeholder="same Deepgram key as your PC"></div>
      <div class="row"><label>Cloud model</label><input type="text" id="cloudModel" placeholder="meta/llama-3.3-70b-instruct (default)" title="NVIDIA model the cloud Phantom uses when your PC is off. Leave empty for the default.">
        <span class="muted small" id="cloudModelHint"></span></div>
      <div class="row">
        <button class="btn" onclick="saveCloud()">Save config</button>
        <button class="btn" onclick="saveCloudKeys()">Save keys</button>
        <button class="btn" onclick="redeployCloud()">🔄 Redeploy cloud</button>
        <button class="btn" onclick="cloudSyncAll()">Sync now</button>
        <button class="btn btn-danger" onclick="clearCloud()">Clear</button>
        <span id="cloudStatus" class="muted small"></span>
      </div>
      <pre id="cloudDeployLog" class="mono small hidden" style="max-height:220px;overflow:auto;background:var(--bg3);border:1px solid var(--border);border-radius:10px;padding:10px;margin-top:8px"></pre>
      <p class="muted small">These are <b>the same API keys you use on your PC</b> (Settings → AI / Voice) — the cloud Phantom uses them when your PC is off. They are <b>NOT</b> the cloud token above (that is the access password for the Worker). Keys are sent straight to the Worker over https — never stored in the app, never shown back.</p>
    </div>
    <div class="settings-section"><h3>🔧 Diagnostics</h3>
      <p class="muted small">Everything the console sees — backend logs, frontend console, network calls, WS events, unhandled errors. No terminal needed.</p>
      <div class="row"><label>Status</label><span id="diagStatus" class="muted small"></span></div>
      <div class="row" style="flex-wrap:wrap">
        <button class="btn mini-btn" onclick="diagTab('all')">All</button>
        <button class="btn mini-btn" onclick="diagTab('backend')">Backend</button>
        <button class="btn mini-btn" onclick="diagTab('console')">Console</button>
        <button class="btn mini-btn" onclick="diagTab('network')">Network/WS</button>
        <button class="btn mini-btn" onclick="diagTab('system')">System</button>
        <button class="btn mini-btn" onclick="loadDiagnostics()">Refresh</button>
        <button class="btn mini-btn" onclick="copyDiagLog()">Copy</button>
        <button class="btn mini-btn" onclick="exportDiagLog()">Export file</button>
        <button class="btn mini-btn" onclick="clearDiagLog()">Clear console</button>
      </div>
      <pre id="diagLog" class="mono small" style="max-height:300px;overflow:auto;background:var(--bg3);border:1px solid var(--border);border-radius:10px;padding:10px">loading…</pre>
      <div class="row" style="margin-top:8px">
        <input id="replInput" placeholder="JS expression — try: document.title · voice.state · state.agent" style="flex:1" onkeydown="if(event.key==='Enter')replRun()">
        <button class="btn" onclick="replRun()">Run</button>
        <span class="muted small">evaluates in the app's context (dev tool)</span>
      </div>
      <pre id="replOut" class="mono small hidden" style="max-height:140px;overflow:auto;background:var(--bg3);border:1px solid var(--border);border-radius:10px;padding:10px"></pre>
    </div>
    <div class="settings-section"><h3>📱 Mobile / remote backend</h3>
      <div class="row"><label>Backend URL</label><input type="text" id="apiBase" placeholder="http://192.168.1.50:8000">
        <button class="btn" onclick="saveApiBase()">Save</button></div>
      <p class="muted small">On your iPhone (no App Store): run <code>bash scripts/tunnel.sh</code> on your PC,
        open the <code>https://…trycloudflare.com</code> URL in Safari, then <b>Share → Add to Home Screen</b>.
        If you set an access token, enter it in the phone's connection screen.</p>
    </div>`);
  body.innerHTML = sections.join("");
  for (const agentId of ["phantom", "coded", "health"]) {
    const s = (res.settings && res.settings[agentId]) || {};
    const modelVal = s.model || "";
    $("model-" + agentId).value = modelVal;
    const preset = $(`modelPreset-${agentId}`);
    if (preset) {
      if (!modelVal) preset.value = "smart";
      else if (modelVal.includes("8b")) preset.value = "fast";
      else preset.value = "custom";
    }
    $("temp-" + agentId).value = s["model.temperature"] ?? 0.4;
  }
  const gm = $("generalModeSel");
  if (gm) {
    const gv = (res.settings && res.settings["*"] && res.settings["*"]["permissions.general_mode"]) || false;
    gm.value = gv ? "1" : "0";
    gm.onchange = () => {
      api("/api/settings", { method: "PUT", body: { agent: "*", key: "permissions.general_mode", value: gm.value === "1" } })
        .then(() => toast(gm.value === "1" ? "⚡ General mode ON — I'll just do it" : "General mode off — I'll ask first", "ok"))
        .catch((e) => toast("✗ " + e.message, "err"));
    };
  }
  const g = res.settings && res.settings["*"] ? res.settings["*"] : {};
  $("quietStart").value = g["quiet.start"] || "";
  $("quietEnd").value = g["quiet.end"] || "";
  $("apiBase").value = localStorage.getItem("phai.apiBase") || "";
  try {
    const cc = await api("/api/cloud/config");
    $("cloudUrl").value = cc.url_masked || "";
    $("cloudStatus").textContent = cc.url ? `☁️ ${cc.url_masked}${cc.token_configured ? " (token set)" : ""}` : "not configured";
  } catch (e) {}
  const vc = res.voice || {};
  $("sttProvider").value = vc.stt?.provider || "server";
  $("ttsProvider").value = vc.tts?.provider || "server";
  $("voiceModeSel").value = vc.mode || "conversation";
  $("proactiveSel").value = vc.proactive_speech ? "1" : "0";
  if ($("sttPriority")) $("sttPriority").value = (vc.stt_priority || ["deepgram", "groq", "local_whisper"]).join(", ");
  if ($("ttsPriority")) $("ttsPriority").value = (vc.tts_priority || ["deepgram", "cloud", "local"]).join(", ");
  if ($("localModelSel")) $("localModelSel").value = vc.local_model || "base";
  if ($("vadThreshold")) {
    $("vadThreshold").value = vc.vad_threshold ?? 0.03;
    $("vadThresholdLbl").textContent = "sensitivity " + (vc.vad_threshold ?? 0.03);
  }
  if ($("autoStopMs")) $("autoStopMs").value = vc.auto_stop_ms ?? 900;
  if ($("maxRecordMs")) $("maxRecordMs").value = vc.max_record_ms ?? 15000;
  if ($("continuousSel")) $("continuousSel").value = vc.continuous ? "1" : "0";
  loadVoiceStatus();
  loadDiagnostics();   // fire-and-forget: fill the Diagnostics log box
  wireHudClickables(); // make home-screen gauges clickable
  await loadVoicePickers(vc);
  await Promise.all([loadBrainKeys(), loadEnrollStatus()]);
  await loadSchedules();
}

/* voice pickers (browser voices + deepgram catalog), per-persona */
async function loadVoicePickers(vc) {
  let catalog = { deepgram: [] };
  try { catalog = await api("/api/voice/voices"); } catch (e) {}
  const synth = window.speechSynthesis;
  const browserVoices = synth ? synth.getVoices() : [];
  if (synth && !browserVoices.length) {
    synth.onvoiceschanged = () => loadVoicePickers(vc); // voices load async
  }
  const dgOpts = (catalog.deepgram || []).map((v) =>
    `<option value="${esc(v.id)}">${v.gender === "male" ? "👨" : "👩"} ${esc(v.id)} — ${esc(v.style)}</option>`).join("");
  const brOpts = browserVoices.map((v) =>
    `<option value="${esc(v.name)}">🔊 ${esc(v.name)} (${esc(v.lang)})</option>`).join("");
  const fill = (sel, cur) => {
    $(sel).innerHTML = `<option value="">default</option>` + brOpts + (dgOpts ? `<optgroup label="Deepgram">${dgOpts}</optgroup>` : "");
    if (cur) $(sel).value = cur;
  };
  fill("voicePhantom", vc.voices?.phantom || "");
  fill("voiceCoded", vc.voices?.coded || "");
}

/* brains & API keys */
async function loadBrainKeys() {
  const res = await api("/api/brains/configs").catch(() => ({ brains: [] }));
  const el = $("brainKeys");
  el.innerHTML = "";
  const rows = res.brains || [];
  if (!rows.length) { el.innerHTML = `<div class="card muted">No brains.</div>`; return; }
  for (const b of rows) {
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `
      <div class="card-title">🧩 ${esc(b.name)} <span class="muted small">${esc(b.brain_id)}</span>
        <span class="pill ${b.key_configured ? "ok" : "info"}">${b.key_configured ? "own key" : "shared"}</span></div>
      <div class="memory-controls" style="margin:6px 0">
        <input type="password" id="bkey-${esc(b.brain_id)}" placeholder="${b.key_configured ? "configured (" + esc(b.key_masked) + ") — type to replace" : "own API key (empty = share Phantom's)"}" style="flex:1">
        <input type="text" id="bmodel-${esc(b.brain_id)}" value="${esc(b.model)}" placeholder="model" style="width:230px">
        <button class="btn" onclick="saveBrainKey('${esc(b.brain_id)}')">Save</button>
        <button class="btn" onclick="testKey('nvidia', '${esc(b.brain_id)}')">Test</button>
        ${b.key_configured ? `<button class="btn btn-danger" onclick="clearBrainKey('${esc(b.brain_id)}')">Remove key</button>` : ""}
      </div>`;
    el.appendChild(card);
  }
}
window.saveBrainKey = async (bid) => {
  const key = $(`bkey-${bid}`).value.trim();
  const model = $(`bmodel-${bid}`).value.trim();
  const body = {};
  if (key) body.api_key = key;
  if (model) body.model = model;
  if (!Object.keys(body).length) return;
  try {
    const r = await api(`/api/brains/${bid}/config`, { method: "PUT", body });
    $(`bkey-${bid}`).value = "";
    toast(r.persisted ? `✓ Saved config for ${bid}` : "⚠️ Could not write to disk", r.persisted ? "ok" : "err");
    loadBrainKeys();
  } catch (e) { toast("✗ Save failed: " + e.message, "err"); }
};
window.clearBrainKey = async (bid) => {
  try {
    const r = await api(`/api/brains/${bid}/config`, { method: "PUT", body: { delete_key: true } });
    toast(r.persisted !== false ? `✓ Removed own key for ${bid} — now shares Phantom's` : "⚠️ Could not update disk", r.persisted !== false ? "ok" : "err");
    loadBrainKeys();
  } catch (e) { toast("✗ " + e.message, "err"); }
};

/* voice enrollment */
let diagActiveTab = "all";
function diagTab(tab) {
  diagActiveTab = tab;
  loadDiagnostics();
}
async function loadDiagnostics() {
  const logEl = $("diagLog"); const stEl = $("diagStatus");
  if (!logEl) return;
  try {
    const d = await api("/api/diagnostics");
    const pc = window.PhantomConsole;
    const consoleLines = pc ? pc.getLogs() : [];
    const lines = [
      `version: ${d.version || "?"} · uptime ${Math.round(d.uptime_s || 0)}s · wake ${(d.wake && d.wake.state) || "?"}`,
      `keys: NVIDIA ${d.keys?.nvidia ? "set" : "—"} · Deepgram ${d.keys?.deepgram ? "set" : "—"} · Groq ${d.keys?.groq ? "set" : "—"}`,
      `speaker engine: ${d.speaker?.available ? "available" : "unavailable"}`,
    ];
    const tab = diagActiveTab;
    if (tab === "all" || tab === "backend") {
      lines.push("---- model endpoints (base_url + model) ----");
      for (const p of (d.providers || [])) lines.push(`  ${p.agent}: ${p.base_url || "?"} · ${p.model || "?"} · ${p.provider || "?"}`);
      lines.push("---- backend log ----");
      lines.push(...(d.logs || []).slice(-160));
    }
    if (tab === "all" || tab === "console") {
      lines.push("---- frontend console ----");
      lines.push(...(consoleLines.length ? consoleLines.map((c) => `${c.t} ${c.level.toUpperCase()} ${c.text}`).slice(-120) : ["(none)"]));
    }
    if (tab === "all" || tab === "network") {
      lines.push("---- network / websocket ----");
      const nets = consoleLines.filter((c) => c.level === "net");
      lines.push(...(nets.length ? nets.map((c) => `${c.t} ${c.text}`).slice(-80) : ["(none)"]));
    }
    if (tab === "all" || tab === "system") {
      const sys = d.system || {};
      lines.push("---- system ----");
      lines.push(`cpu total ${sys.cpu?.total ?? "?"}% · cores ${sys.cpu?.cores ?? "?"} · mem ${sys.memory?.percent ?? "?"}%`);
      lines.push(`disk R ${sys.disk?.read_bps ?? 0} B/s · W ${sys.disk?.write_bps ?? 0} B/s · net ↓${sys.net?.down_bps ?? 0} ↑${sys.net?.up_bps ?? 0}`);
      lines.push(`battery: ${sys.battery?.available ? sys.battery.percent + "%" : "unavailable"}`);
      lines.push(`processes: ${sys.process_count ?? "?"} · top cpu: ${sys.process?.name || "—"} ${sys.process?.cpu_percent || 0}%`);
      lines.push(`pending asyncio tasks: ${d.pending_tasks ?? "?"} · ws subscribers: ${d.ws_subscribers ?? "?"}`);
    }
    logEl.textContent = lines.join("\n");
    logEl.scrollTop = logEl.scrollHeight;
    if (stEl) stEl.textContent = `connected · v${d.version || "?"} · tab: ${tab}`;
  } catch (e) {
    logEl.textContent = "Could not reach backend diagnostics: " + e.message +
      "\n\nFrontend console:\n" + (window.PhantomConsole ? window.PhantomConsole.getLogs().map((c) => `${c.t} ${c.level.toUpperCase()} ${c.text}`).slice(-20).join("\n") : "(none)");
    if (stEl) stEl.textContent = "backend unreachable";
  }
}
window.replRun = () => {
  const inp = $("replInput"); const out = $("replOut");
  if (!inp || !out) return;
  const code = inp.value.trim();
  if (!code) return;
  out.classList.remove("hidden");
  let r;
  if (window.PhantomConsole) r = window.PhantomConsole.evalJs(code);
  else { try { r = { ok: true, result: String((0, eval)(code)) }; } catch (e) { r = { ok: false, error: String(e) }; } }
  out.textContent += (r.ok ? "> " + code + "\n" + r.result : "> " + code + "\n✗ " + r.error) + "\n";
  out.scrollTop = out.scrollHeight;
};
window.exportDiagLog = () => {
  const el = $("diagLog");
  if (!el) return;
  try {
    const blob = new Blob([el.textContent || ""], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = "phantom-diag-" + new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-") + ".txt";
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 3000);
    toast("✓ Diagnostics exported", "ok");
  } catch (e) { toast("Export failed: " + e.message, "err"); }
};
window.clearDiagLog = async () => {
  if (window.PhantomConsole) window.PhantomConsole.clear();
  try { await api("/api/diagnostics/clear", { method: "POST" }); } catch (e) {}
  loadDiagnostics();
  toast("✓ Console cleared", "ok");
};

// ---- HUD panels: click any home gauge for details + manage ----
function wireHudClickables() {
  const map = {
    cpuHistPanel: ["CPU", "cpu", "settings"],
    memHistPanel: ["Memory", "memory", "settings"],
    corePanel: ["Processor units", "cpu", "settings"],
    diskPanel: ["Disk I/O", "disk", "settings"],
    weatherPanel: ["Weather", "weather", "settings"],
    moonPanel: ["Moon", "moon", "settings"],
    netPanel: ["Network", "net", "settings"],
    sysPanel: ["System", "system", "settings"],
  };
  for (const [id, [title, key, manage]] of Object.entries(map)) {
    const el = document.getElementById(id);
    if (!el || el._hudWired) continue;
    el._hudWired = true;
    el.classList.add("hud-clickable");
    el.title = "Click for details — " + title;
    el.addEventListener("click", (ev) => {
      if (ev.target.closest("button, a, input, select")) return;
      openHudModal(title, key, manage);
    });
  }
}
let hudModalTimer = null;
async function openHudModal(title, key, manage) {
  const modal = $("hudModal");
  if (!modal) return;
  $("hudModalTitle").textContent = title;
  modal._key = key; modal._manage = manage || "settings";
  modal.classList.remove("hidden");
  await refreshHudModal();
  if (hudModalTimer) clearInterval(hudModalTimer);
  hudModalTimer = setInterval(() => { if (!$("hudModal").classList.contains("hidden")) refreshHudModal(); }, 2000);
}
function fmtRate(bps) {
  const v = Number(bps) || 0;
  if (v >= 1024 * 1024) return (v / 1024 / 1024).toFixed(1) + " MB/s";
  if (v >= 1024) return Math.round(v / 1024) + " KB/s";
  return Math.round(v) + " B/s";
}
window.fmtRate = fmtRate;
async function refreshHudModal() {
  const body = $("hudModalBody");
  const modal = $("hudModal");
  const key = modal && modal._key;
  if (!body || !key) return;
  try {
    const d = await api("/api/hud");
    const sec = (k, v) => `<h4 class="muted" style="margin:10px 0 4px;letter-spacing:1px">${k}</h4><pre class="mono small">${esc(JSON.stringify(v, null, 2))}</pre>`;
    const bar = (pct) => `<div style="background:var(--bg3);border-radius:6px;height:8px;overflow:hidden"><div style="width:${Math.min(100, pct || 0)}%;height:100%;background:${(pct||0)>=80?"#f0716b":(pct||0)>=50?"#f6c945":"#6ea8fe"};border-radius:6px"></div></div>`;
    const topProc = (d.top_processes || []).map((p2) => `${p2.cpu_percent}%  ${p2.name} (pid ${p2.pid})`);
    let html = "";
    if (key === "cpu") {
      const c = d.cpu || {};
      html = `<div class="hud-panel-val" style="font-size:15px">CPU total <b>${c.total ?? "?"}%</b> · ${c.cores ?? "?"} cores · load ${(c.load_avg||[]).join(" / ")}</div>`;
      html += `<div style="margin:8px 0">${bar(c.total)}</div>`;
      html += `<div class="cpu-bars" style="height:44px">${(c.per_core||[]).map((p2) => `<div class="cpu-bar${p2>=80?" hot":p2>=50?" warm":""}" style="height:${Math.max(3,Math.min(100,p2))}%"></div>`).join("")}</div>`;
      html += `<div class="muted small">per-core ${(c.per_core||[]).join(" · ") || "—"}</div>`;
      html += sec("Top processes by CPU", topProc);
      html += `<button class="btn" style="margin-top:10px" onclick="askPhantomAbout('cpu')">🤖 Ask Phantom what's using CPU</button>`;
    } else if (key === "memory") {
      const m = d.memory || {};
      html = `<div class="hud-panel-val" style="font-size:15px">Memory <b>${m.percent ?? "?"}%</b></div>`;
      html += `<div style="margin:8px 0">${bar(m.percent)}</div>`;
      html += `<div class="muted small">used ${((m.used_bytes||0)/1024**3).toFixed(1)} GB / ${((m.total_bytes||0)/1024**3).toFixed(1)} GB</div>`;
      html += sec("Top processes by CPU", topProc);
      html += `<button class="btn" style="margin-top:10px" onclick="askPhantomAbout('memory')">🤖 Ask Phantom about memory</button>`;
    } else if (key === "disk") {
      const ds = d.disk || {};
      html = `<div class="hud-panel-val" style="font-size:15px">Disk I/O</div>`;
      html += `<div class="muted small" style="margin:6px 0">read ${fmtRate(ds.read_bps)} · write ${fmtRate(ds.write_bps)}</div>`;
      if (ds.usage_percent != null) {
        html += `<div class="muted small">storage ${ds.usage_percent}% used — ${ds.used_gb} / ${ds.total_gb} GB</div><div style="margin:6px 0">${bar(ds.usage_percent)}</div>`;
      }
      html += sec("Top processes by CPU", topProc);
      html += `<button class="btn" style="margin-top:10px" onclick="askPhantomAbout('disk')">🤖 Ask Phantom about disk usage</button>`;
    } else if (key === "net") {
      const n = d.net || {};
      html = `<div class="hud-panel-val" style="font-size:15px">Network</div>`;
      html += `<div class="muted small" style="margin:6px 0">↓ ${fmtRate(n.down_bps)} · ↑ ${fmtRate(n.up_bps)}</div>`;
      html += `<canvas id="netModalGraph" class="hud-graph" width="400" height="90"></canvas>`;
      html += sec("Top processes by CPU", topProc);
      html += `<button class="btn" style="margin-top:10px" onclick="askPhantomAbout('network')">🤖 Ask Phantom about network</button>`;
      requestAnimationFrame(() => drawNetModalGraph());
    } else if (key === "system") {
      html = sec("Battery", d.battery && d.battery.available ? `${d.battery.percent}%${d.battery.plugged ? " (plugged)" : ""}` : "unavailable");
      html += sec("Process count", d.process_count ?? "?");
      html += sec("Top processes by CPU", topProc);
    } else if (key === "weather") {
      const w = await api("/api/hud/weather").catch(() => ({ available: false, reason: "unavailable" }));
      html = sec("Weather", w.available ? { current: w.current, daily: (w.daily || {}).time ? { next5: (w.daily.time||[]).slice(0,5), max: (w.daily.temperature_2m_max||[]).slice(0,5), min: (w.daily.temperature_2m_min||[]).slice(0,5) } : "no outlook" } : w.reason);
    } else if (key === "moon") {
      html = sec("Moon", window.PhantomHud ? (() => { const m = window.PhantomHud.moonPhase(new Date()); return `${m.icon} ${m.name} — illumination ${m.illumination}%`; })() : "unavailable");
    } else {
      html = sec(key, d[key]);
    }
    body.innerHTML = html +
      `<div class="muted small" style="margin-top:10px">Live reading from /api/hud — every value is real. Say “Phantom, check the CPU” and he'll open this too.</div>`;
  } catch (e) {
    body.innerHTML = `<div class="muted small">could not refresh: ${esc(e.message)}</div>`;
  }
}
function drawNetModalGraph() {
  const c = document.getElementById("netModalGraph");
  if (!c || !hudGauges) return;
  const ctx = c.getContext && c.getContext("2d");
  if (!ctx) return;
  const down = hudGauges.netDown.get(), up = hudGauges.netUp.get();
  const max = Math.max(1024, ...down, ...up);
  const w = c.width, h = c.height;
  ctx.clearRect(0, 0, w, h);
  const draw = (vals, color) => {
    if (!vals.length) return;
    ctx.beginPath();
    for (let i = 0; i < vals.length; i++) {
      const x = vals.length === 1 ? 0 : (i / (vals.length - 1)) * w;
      const y = h - (Math.min(vals[i], max) / max) * (h - 6) - 2;
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    }
    ctx.strokeStyle = color; ctx.lineWidth = 1.6; ctx.stroke();
  };
  draw(down, "rgba(110,168,254,.9)");
  draw(up, "rgba(46,204,113,.7)");
  ctx.fillStyle = "rgba(110,168,254,.5)";
  ctx.font = "10px sans-serif";
  ctx.fillText("blue = ↓ down · green = ↑ up", 6, 12);
}
async function askPhantomAbout(topic) {
  const labels = { cpu: "what's using my CPU right now", memory: "how my memory looks and if I should close anything", disk: "my disk usage and if I should clean anything", network: "my network traffic and if anything looks wrong" };
  const q = labels[topic] || ("check my " + topic);
  const modal = $("hudModal");
  if (modal) modal.classList.add("hidden");
  switchPersona("phantom");
  switchPanel("chats");
  $("input").value = "Look at the HUD " + topic + " data and tell me " + q + ". Use your system tools to inspect.";
  $("sendBtn").click();
}
window.askPhantomAbout = askPhantomAbout;
window.hudOpenModal = openHudModal;
window.hudRefreshModal = refreshHudModal;
$("hudModal") && ($("hudModal").addEventListener("click", (ev) => {
  if (ev.target.id === "hudModal") $("hudModal").classList.add("hidden");
}));
$("hudModalClose") && ($("hudModalClose").onclick = () => $("hudModal").classList.add("hidden"));
$("hudModalRefresh") && ($("hudModalRefresh").onclick = () => refreshHudModal());
$("hudModalManage") && ($("hudModalManage").onclick = () => {
  $("hudModal").classList.add("hidden");
  openDrawer(); switchPanel("settings");
});

window.copyDiagLog = () => {
  const el = $("diagLog");
  if (!el) return;
  const text = el.textContent || "";
  (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject(new Error("no clipboard")))
    .then(() => toast("✓ Diagnostics copied", "ok"))
    .catch(() => { const ta = document.createElement("textarea"); ta.value = text; document.body.appendChild(ta); ta.select(); try { document.execCommand("copy"); toast("✓ Diagnostics copied", "ok"); } catch (e) { toast("Copy failed — select the log manually", "err"); } ta.remove(); });
};

async function loadEnrollStatus() {
  const [p, c] = await Promise.all([
    api("/api/voice/enroll/status?agent=phantom"),
    api("/api/voice/enroll/status?agent=coded"),
  ]);
  const fmt = (r) => r.enrolled ? "✅ enrolled" : (r.engine.available ? "not enrolled" : "⚠️ " + (r.engine.reason || "engine unavailable").slice(0, 80));
  const pe = $("enrollPhantomStatus"); if (pe) pe.textContent = fmt(p);
  const ce = $("enrollCodedStatus"); if (ce) ce.textContent = fmt(c);
  const lock = $("speakerLockCb"); if (lock) lock.checked = p.speaker_lock;
  // disable enroll buttons + tell the user WHY when the engine can't load
  const pb = $("enrollPhantomBtn"); const cb = $("enrollCodedBtn");
  const blocked = !p.engine.available;
  if (pb) { pb.disabled = blocked; pb.title = blocked ? "voice engine unavailable — see hint below" : ""; }
  if (cb) { cb.disabled = blocked; cb.title = blocked ? "voice engine unavailable — see hint below" : ""; }
  // auto-instruct: show the exact install command when the engine is missing
  const hint = $("enrollHint");
  if (hint) {
    if (blocked) {
      const reason = (p.engine.reason || "engine unavailable").replace(/</g, "&lt;");
      const cmd = (p.engine.install_hint || "pip install --user resemblyzer torch").replace(/\n/g, "<br>").replace(/</g, "&lt;");
      hint.className = "card";
      hint.innerHTML = `<div style="color:#f6c945;font-weight:700">⚠️ Speaker lock engine not installed</div>
        <div class="muted small" style="margin:4px 0">${reason}</div>
        <div style="margin:6px 0">Run this once in a terminal, then click Re-check:</div>
        <pre class="mono small" id="enrollCmd" style="background:var(--bg3);border:1px solid var(--border);border-radius:8px;padding:8px;overflow:auto">${cmd}</pre>
        <button class="btn" onclick="copyEnrollCmd()">Copy command</button>
        <button class="btn" onclick="loadEnrollStatus()">Re-check</button>`;
    } else {
      hint.className = "hidden";
      hint.innerHTML = "";
    }
  }
}
window.copyEnrollCmd = () => {
  const el = $("enrollCmd");
  if (!el) return;
  const text = el.textContent || "";
  (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject(new Error("no clipboard")))
    .then(() => toast("✓ Install command copied", "ok"))
    .catch(() => toast("Select the command and copy manually", "err"));
}
async function enrollVoice(agent) {
  // pre-check the engine so the user gets a clear message instead of silence
  try {
    const st = await api(`/api/voice/enroll/status?agent=${agent}`);
    if (!st.engine || !st.engine.available) {
      const why = (st.engine && st.engine.reason) || "voice engine unavailable";
      toast("Voice enrollment unavailable: " + why.slice(0, 120), "err");
      loadEnrollStatus();
      return;
    }
  } catch (e) { /* let the loop surface errors */ }
  toast(`🎙 Speak 3 short phrases for ${agent}…`);
  for (let i = 1; i <= 3; i++) {
    $("voiceHint").textContent = `Enrollment ${i}/3 — speak now…`;
    const wav = await voice.captureWav(2);
    if (!wav) { toast("mic capture failed"); return; }
    const reader = new FileReader();
    const b64 = await new Promise((res) => { reader.onload = () => res(String(reader.result).split(",")[1]); reader.readAsDataURL(wav); });
    const res = await api(`/api/voice/enroll/sample?agent=${agent}`, {
      headers: { "Content-Type": "application/octet-stream" },
      method: "POST",
      body: b64 ? atob(b64) : "",
    }).catch((e) => ({ error: e.message }));
    if (res.error) { toast("Enroll failed: " + res.error, "err"); return; }
    if (res.enrolled) { toast(`✅ ${agent} voice enrolled`, "ok"); break; }
    else toast(`✓ sample ${i} captured (${res.samples || 1}/${res.samples_needed || 3})`, "ok");
  }
  $("voiceHint").textContent = "";
  loadEnrollStatus();
}
window.enrollVoice = enrollVoice;

/* wake detection from settings */
window.addEventListener("DOMContentLoaded", () => {
  const enrollPhantom = $("enrollPhantomBtn");
  const enrollCoded = $("enrollCodedBtn");
  if (enrollPhantom) enrollPhantom.onclick = () => enrollVoice("phantom");
  if (enrollCoded) enrollCoded.onclick = () => enrollVoice("coded");
  const clearBtn = $("enrollClearBtn");
  if (clearBtn) clearBtn.onclick = async () => {
    await api("/api/voice/enroll/clear", { body: { agent: "phantom" } });
    await api("/api/voice/enroll/clear", { body: { agent: "coded" } });
    toast("Enrollments cleared");
    loadEnrollStatus();
  };
  const lockCb = $("speakerLockCb");
  if (lockCb) lockCb.onchange = async () => {
    try {
      await api("/api/voice/enroll/speaker-lock", { method: "PUT", body: { enabled: lockCb.checked } });
      toast(lockCb.checked ? "🔒 Speaker lock ON — only your voice wakes them" : "Speaker lock off", "ok");
    } catch (e) { toast("✗ " + e.message, "err"); }
  };
});
async function saveSetting(agent, key, value) {
  await api("/api/settings", { method: "PUT", body: { agent, key, value } });
}
window.applyModelPreset = async (agentId) => {
  const preset = $(`modelPreset-${agentId}`)?.value;
  const modelInput = $(`model-${agentId}`);
  if (!modelInput) return;
  if (preset === "fast") {
    modelInput.value = "meta/llama-3.1-8b-instruct";
    await api("/api/settings", { method: "PUT", body: { agent: agentId, key: "model", value: modelInput.value } });
    toast("⚡ Fast mode — " + modelInput.value, "ok");
  } else if (preset === "smart") {
    modelInput.value = "";
    await api("/api/settings", { method: "PUT", body: { agent: agentId, key: "model", value: "" } });
    toast("🧠 Smart mode — default model", "ok");
  }
  loadStatus();
};

window.saveKey = async (agentId) => {
  const input = $(`key-${agentId}`);
  const val = input.value.trim();
  if (!val) return;
  try {
    const r = await api("/api/settings", { method: "PUT",
      body: { agent: agentId, key: "nvidia_api_key", value: val } });
    // model rides along with the key: whatever the user typed (or none -> default)
    const modelInput = $(`model-${agentId}`);
    const model = modelInput ? modelInput.value.trim() : "";
    if (model) {
      await api("/api/settings", { method: "PUT",
        body: { agent: agentId, key: "model", value: model } });
    }
    input.value = "";
    const modelNote = model ? `model: ${model}` : "model: default (meta/llama-3.3-70b-instruct)";
    toast(r.persisted ? `✓ Saved — key stored safely · ${modelNote}` : "⚠️ Could not write to disk", r.persisted ? "ok" : "err");
    loadSettings(); loadStatus();
  } catch (e) { toast("✗ Save failed: " + e.message, "err"); }
};
window.saveDeepgram = async () => {
  const input = $("deepgramKey");
  const val = input.value.trim();
  if (!val) return;
  try {
    const r = await api("/api/voice/config", { method: "PUT", body: { deepgram_api_key: val } });
    input.value = "";
    toast(r.persisted ? "✓ Deepgram key saved (server-side)" : "⚠️ Could not write to disk", r.persisted ? "ok" : "err");
    loadSettings();
  } catch (e) { toast("✗ Save failed: " + e.message, "err"); }
};
window.saveGroq = async () => {
  const input = $("groqKey");
  const val = input.value.trim();
  if (!val) return;
  try {
    const r = await api("/api/voice/config", { method: "PUT", body: { groq_api_key: val } });
    input.value = "";
    toast(r.persisted ? "✓ Groq key saved (server-side)" : "⚠️ Could not write to disk", r.persisted ? "ok" : "err");
    loadSettings();
  } catch (e) { toast("✗ Save failed: " + e.message, "err"); }
};
window.testKey = async (kind, agent) => {
  const label = { nvidia: "NVIDIA", deepgram: "Deepgram", groq: "Groq", cloud: "Portable Phantom" }[kind] || kind;
  toast(`⏳ Testing ${label}…`);
  try {
    const r = await api("/api/keys/test", { body: { kind, agent: agent || "phantom" } });
    toast(r.message, r.ok ? "ok" : "err");
  } catch (e) { toast("✗ " + e.message, "err"); }
};

const VOICE_STATE_ICON = { healthy: "🟢", degraded: "🟡", disabled: "🔴", unavailable: "⚪", untested: "⚪" };

async function loadVoiceStatus() {
  const box = $("voiceStatusBox");
  if (!box) return;
  try {
    const [st, us] = await Promise.all([
      api("/api/voice/status"), api("/api/voice/usage"),
    ]);
    const provs = (st.providers || []).map((p) => {
      const icon = VOICE_STATE_ICON[p.state] || "⚪";
      const active = p.active ? " <b>← active</b>" : "";
      const err = p.last_error ? `<div class="small muted" style="margin-left:22px">${esc(p.last_error)}</div>` : "";
      const prio = p.priority ? ` <span class="muted small">#${p.priority}</span>` : "";
      return `<div style="margin:4px 0">${icon} ${esc(p.label)} <span class="muted small">(${esc(p.role_label)})</span>${prio}${active}${err}</div>`;
    }).join("");
    const lw = st.local_whisper || {};
    const lwLine = lw.installed
      ? `🟢 Local Whisper <span class="muted small">(${esc(lw.model)}${lw.loaded ? ", loaded" : ", idle"})</span>`
      : `⚪ Local Whisper <span class="muted small">(${esc(lw.model)}) — ${esc(lw.reason || "not installed")}</span>`;
    const t = us.totals || {};
    box.innerHTML = `
      <div class="card" style="margin-top:8px">
        <div class="card-title">Provider status</div>
        <div>${provs || "no providers"}</div>
        <div style="margin-top:4px">${lwLine}</div>
        <div class="muted small" style="margin-top:6px">Last 24h: ${t.requests || 0} requests · ${((t.audio_seconds || 0) / 60).toFixed(1)} min audio · est. cost $${(t.est_cost_usd || 0).toFixed(4)}</div>
      </div>`;
  } catch (e) {
    box.innerHTML = `<div class="muted small">provider status unavailable: ${esc(e.message)}</div>`;
  }
}

window.testVoicePipeline = async () => {
  try {
    toast("🎤 Recording 2s… speak now");
    const wav = await voice.captureWav(2);
    if (!wav) { toast("✗ Microphone not available", "err"); return; }
    toast("⏳ Running full pipeline (STT → Phantom → TTS)…");
    const fd = new FormData();
    fd.append("audio", wav, "test.wav");
    const res = await fetch("/api/voice/test", { method: "POST", body: fd });
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch (e) {}
      toast("✗ " + detail, "err");
      return;
    }
    const sttProv = res.headers.get("X-STT-Provider") || "?";
    const ttsProv = res.headers.get("X-TTS-Provider") || "?";
    const transcript = res.headers.get("X-Transcript") || "";
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    audio.onended = () => URL.revokeObjectURL(url);
    await audio.play();
    toast(`✓ Voice system OK — STT: ${sttProv} → TTS: ${ttsProv}${transcript ? ` (heard: "${transcript.slice(0, 60)}")` : ""}`, "ok");
    loadVoiceStatus();
  } catch (e) {
    toast("✗ Voice test failed: " + e.message, "err");
  }
};
async function loadSchedules() {
  const res = await api("/api/schedules");
  const list = $("schedList"); const rows = res.schedules || [];
  list.innerHTML = rows.length ? "" : `<div class="muted small">No schedules yet.</div>`;
  for (const s of rows) {
    const d = document.createElement("div"); d.className = "card";
    d.innerHTML = `<div class="card-title">${esc(s.name)}
      <span class="pill ${s.enabled ? "ok" : "err"}">${s.enabled ? "on" : "off"}</span>
      <span class="pill info">${esc(s.expression)}</span></div>
      <div class="card-meta"><span>next: ${esc(s.next_run_at || "—")}</span>
      ${s.last_status ? `<span>last: ${esc(s.last_status)}</span>` : ""}</div>
      <div class="card-actions">
        <button class="mini-btn" onclick="toggleSchedule('${s.id}', ${s.enabled ? 0 : 1})">${s.enabled ? "Disable" : "Enable"}</button>
        <button class="mini-btn" onclick="deleteSchedule('${s.id}')">Delete</button></div>`;
    list.appendChild(d);
  }
}
window.addSchedule = async () => {
  try {
    await api("/api/schedules", { body: { agent: "phantom", name: $("schedName").value.trim() || "Scheduled check",
      expression: $("schedExpr").value.trim(), prompt: $("schedPrompt").value.trim() } });
    loadSchedules();
  } catch (e) { toast("Error: " + e.message); }
};
window.toggleSchedule = async (id, on) => { await api(`/api/schedules/${id}`, { method: "PUT", body: { enabled: !!on } }); toast("✓ Schedule updated", "ok"); loadSchedules(); };
window.deleteSchedule = async (id) => { await api(`/api/schedules/${id}`, { method: "DELETE" }); loadSchedules(); };
window.saveApiBase = () => {
  localStorage.setItem("phai.apiBase", $("apiBase").value.trim());
  toast("Backend URL saved — reloading…"); setTimeout(() => location.reload(), 600);
};
window.saveCloud = async () => {
  try {
    const r = await api("/api/cloud/config", { method: "PUT", body: {
      url: $("cloudUrl").value.trim(), token: $("cloudToken").value.trim() } });
    $("cloudToken").value = "";
    toast("✓ Portable Phantom saved", "ok");
    $("cloudStatus").textContent = `☁️ ${r.url_masked}${r.token_configured ? " (token set)" : ""}`;
  } catch (e) { toast("Error: " + e.message); }
};
window.saveCloudKeys = async () => {
  const nv = $("cloudNvidia").value.trim();
  const dg = $("cloudDeepgram").value.trim();
  const model = $("cloudModel").value.trim();
  const body = {};
  if (nv) body.nvidia_key = nv;
  if (dg) body.deepgram_key = dg;
  if (model) body.model = model;
  if (!Object.keys(body).length) { toast("Paste a key or model first"); return; }
  try {
    const r = await api("/api/cloud/keys", { body });
    $("cloudNvidia").value = ""; $("cloudDeepgram").value = "";
    toast("Cloud config saved (masked) — " + JSON.stringify(r.masked || {}));
    const hint = $("cloudModelHint");
    if (hint) hint.textContent = (r.masked && r.masked.model) ? "active: " + r.masked.model : "";
    loadSettings();
  } catch (e) { toast("Error: " + e.message); }
};
window.clearCloud = async () => {
  try {
    await api("/api/cloud/config", { method: "PUT", body: { clear: true } });
    $("cloudUrl").value = ""; $("cloudStatus").textContent = "not configured";
    toast("✓ Cloud config cleared", "ok");
  } catch (e) { toast("✗ " + e.message, "err"); }
};
window.cloudSyncAll = async () => {
  try {
    const r = await api("/api/cloud/sync", { method: "POST" });
    toast(`Synced — profile ✓, reminders ${r.reminders?.pushed ?? 0}, memories ${r.memories?.mirrored_new ?? 0} new`);
  } catch (e) { toast("Sync failed: " + e.message); }
};
window.redeployCloud = async () => {
  const logEl = $("cloudDeployLog");
  logEl.classList.remove("hidden");
  logEl.textContent = "Redeploying Portable Phantom… (this takes ~1 min)";
  try {
    const r = await api("/api/cloud/deploy", {
      body: { nvidia_key: $("cloudNvidia").value.trim(),
              deepgram_key: $("cloudDeepgram").value.trim(),
              cloud_token: $("cloudToken").value.trim() } });
    toast("Redeploy started — watch the log");
    $("cloudNvidia").value = ""; $("cloudDeepgram").value = ""; $("cloudToken").value = "";
  } catch (e) {
    logEl.textContent = "✗ " + e.message;
    toast("Redeploy failed: " + e.message);
  }
};
/* stream cloud.deploy_log events into the log box */
document.addEventListener("cloud-deploy-log", (ev) => {
  const logEl = $("cloudDeployLog");
  if (logEl && !logEl.classList.contains("hidden")) {
    logEl.textContent += "\n" + ev.detail;
    logEl.scrollTop = logEl.scrollHeight;
  }
});

/* ============================== UPDATER ============================== */
const updater = window.phaiUpdater || null;
let updaterState = { state: "idle" };
let updaterVersion = "";
function renderUpdater() {
  const installBtn = $("updateInstallBtn"); const downloadBtn = $("updateDownloadBtn");
  const releaseBtn = $("updateReleaseBtn"); const stateEl = $("updateState");
  const verEl = $("updateVer");
  if (verEl) verEl.textContent = updaterVersion ? `Phantom v${updaterVersion}` : "";
  if (!updater) { if (stateEl) stateEl.textContent = "updates are for the desktop app (browser mode)"; return; }
  const s = updaterState;
  let text = "not checked — press “Check for updates”";
  let downloadVisible = false, installVisible = false, releaseVisible = false;
  if (s.state === "checking") text = "⏳ checking for updates…";
  else if (s.state === "up-to-date") text = `✓ you're on the latest version — v${s.version || updaterVersion}`;
  else if (s.state === "available") {
    text = `⬆ update available: v${s.version} — downloading…`;
    downloadVisible = true; // manual fallback if auto-download didn't start
    releaseVisible = true;
  }
  else if (s.state === "downloading") text = `⬇ downloading… ${s.percent || 0}%`;
  else if (s.state === "ready") { text = `⬆ update ready: v${s.version} — restart to install`; installVisible = true; releaseVisible = true; }
  else if (s.state === "error") {
    text = `update check failed: ${s.message || ""}`;
    releaseVisible = true;
    // deb installs live in root-owned /opt — electron-updater can't write there.
    const msg = String(s.message || "").toLowerCase();
    if (msg.includes("eacces") || msg.includes("permission") || msg.includes("denied")) {
      text += " — the .deb install needs sudo. Use the AppImage, or grab the new .deb from the releases page.";
    }
  } else if (s.state === "dev") text = "dev mode — updates only in the packaged app";
  if (stateEl) stateEl.textContent = text;
  if (installBtn) installBtn.classList.toggle("hidden", !installVisible);
  if (downloadBtn) downloadBtn.classList.toggle("hidden", !downloadVisible);
  if (releaseBtn) releaseBtn.classList.toggle("hidden", !releaseVisible);
}
window.updaterDownload = () => {
  if (!updater) return;
  updaterState = { state: "downloading", percent: 0 }; renderUpdater();
  toast("⬇ Downloading update…");
  updater.download();
};
window.updaterInstall = () => { if (updater) updater.install(); };
window.updaterOpenReleases = () => {
  try { window.open("https://github.com/g2code33/PHANTOM-AI/releases"); } catch (e) {}
};
async function checkForUpdates() {
  if (!updater) { toast("Updates are for the desktop app — you're in a browser", "err"); return; }
  if (!updaterVersion) {
    try { updaterVersion = (await updater.getVersion()) || ""; } catch (e) {}
  }
  updaterState = { state: "checking" }; renderUpdater();
  const res = await updater.check().catch((e) => ({ state: "error", message: String(e) }));
  // never let a transient 'checking' clobber the real result the events
  // already delivered (the race that stuck the button on 'checking…')
  if (res && res.state && res.state !== "checking") updaterState = res;
  renderUpdater();
  const st = updaterState;
  if (st.state === "available") {
    toast(`⬆ Update v${st.version} found — downloading…`);
    updater.download();
  } else if (st.state === "ready") {
    toast(`⬆ Update v${st.version} downloaded — restart to install`, "ok");
  } else if (st.state === "up-to-date") {
    toast(`✓ You're on the latest — v${st.version || updaterVersion}`, "ok");
  } else if (st.state === "error") {
    const msg = String(st.message || "");
    toast("⬆ Update check failed: " + (msg || "no detail"), "err");
  } else if (st.state === "dev") {
    toast("Dev mode — updates only in the packaged app", "err");
  } else if (!st.state || st.state === "idle") {
    toast("⬆ No update info returned — check the releases page", "err");
  }
}
window.checkForUpdates = checkForUpdates;

/* ============================== NOTIFICATIONS ============================== */
async function refreshNotifications() {
  const res = await api("/api/notifications");
  const rows = res.notifications || [];
  $("notifBadge").textContent = rows.filter((n) => !n.read).length || "";
  $("notifBadge").classList.toggle("hidden", !rows.filter((n) => !n.read).length);
  $("notifList").innerHTML = rows.length ? "" : `<div class="notif-item muted">No notifications.</div>`;
  for (const n of rows) {
    const d = document.createElement("div"); d.className = "notif-item";
    d.innerHTML = `<div class="n-title">${esc(n.title)}</div><div class="n-body">${esc(n.body)}</div>
      <div class="n-actions"><button class="mini-btn" onclick="readNotif('${n.id}')">read</button>
      <button class="mini-btn" onclick="dismissNotif('${n.id}')">dismiss</button></div>`;
    $("notifList").appendChild(d);
  }
}
window.readNotif = async (id) => { await api(`/api/notifications/${id}/read`); refreshNotifications(); };
window.dismissNotif = async (id) => { await api(`/api/notifications/${id}/dismiss`); refreshNotifications(); };

/* ============================== STATUS / KILL / MODE ============================== */
async function loadStatus() {
  try {
    const res = await api("/api/status");
    const p = res.providers?.phantom || {};
    $("presenceDot").classList.toggle("off", !p.ok);
    const pill = $("modePill");
    if (pill) {
      const online = p.ok && p.provider !== "offline";
      pill.textContent = online ? "ONLINE" : "LOCAL";
      pill.classList.toggle("online", online);
      pill.classList.toggle("local", !online);
    }
    if (res.voice) {
      voice.mode = res.voice.mode || voice.mode;
      voice.proactiveSpeech = !!res.voice.proactive_speech;
      voice.deepgramConfigured = !!res.voice.deepgram_configured;
    }
    setKillState(res.killswitch?.engaged);
    const totalPending = Object.values(res.pending_confirmations || {}).reduce((a, b) => a + b, 0);
    if (totalPending) toast(`⏳ ${totalPending} action(s) awaiting approval`);
  } catch (e) {}
}
function setKillState(engaged) {
  state.killEngaged = !!engaged;
  $("killBanner").classList.toggle("hidden", !engaged);
  $("killSwitch").style.opacity = engaged ? 0.4 : 1;
}
async function toggleKill() {
  if (state.killEngaged) return;
  if (!confirm("Engage the KILL SWITCH?\n\nStops voice, microphone, tasks and all agent runs.")) return;
  await api("/api/killswitch/engage", { body: { reason: "manual (UI)" } });
  voice.kill();
  toast("⛔ Kill switch engaged");
  loadStatus();
}
$("disengageBtn") && ($("disengageBtn").onclick = () => api("/api/killswitch/disengage").then(loadStatus));

/* ============================== SPLASH ============================== */
function showSplash(ms = 1800) {
  const sp = $("splash");
  if (!sp) return;
  sp.classList.remove("hide");
  clearTimeout(showSplash._t);
  showSplash._t = setTimeout(() => sp.classList.add("hide"), ms);
}

/* ============================== CORE CANVAS ============================== */
/* JARVIS HUD — real spectrum, tiered rendering.
 * - Tier comes from the existing state machine only (voice state + presence
 *   sleeping class); sleeping = low tier = NO continuous redraw loop.
 * - Bars are driven by REAL audio data (mic analyser while listening, TTS
 *   analyser while speaking). No sine-wave "telemetry".
 * - Gauges below poll /api/hud: per-core CPU, disk/net I/O, battery, top CPU
 *   process — every value real, "unavailable" shown honestly. */
const coreCanvas = $("coreCanvas");
const ctx = coreCanvas && coreCanvas.getContext("2d");
let rafId = null;
let hudLoopActive = false;
const hudGauges = window.PhantomHud ? new window.PhantomHud.HudGauges() : null;
const spectrumStrip = window.PhantomHud ? new window.PhantomHud.SpectrumStrip() : null;
// session/clock/moon/weather panels (started once)
window.PhantomHud && window.PhantomHud.startClock();
// session badge version: prefer the updater's app version; fall back to the
// backend status version (browser/dev mode)
if (!window.appVersion) {
  api("/api/status").then((s) => { window.appVersion = (s && s.version) || ""; window.PhantomHud && window.PhantomHud.renderSession(); }).catch(() => {});
}
window.PhantomHud && window.PhantomHud.renderSession();
window.PhantomHud && window.PhantomHud.renderMoon();
window.PhantomHud && window.PhantomHud.loadWeather();
setInterval(() => { if (window.PhantomHud) window.PhantomHud.loadWeather(); }, 10 * 60 * 1000);

function hudTierNow() {
  const sleeping = document.body.classList.contains("presence-sleeping");
  const state = document.body.dataset.state || "idle";
  return window.PhantomHud ? window.PhantomHud.hudTier(state, sleeping) : (sleeping ? "low" : "full");
}

function hudApplyTier() {
  const tier = hudTierNow();
  document.body.classList.toggle("hud-low", tier === "low");
  document.body.classList.toggle("hud-medium", tier === "medium");
  if (hudGauges) hudGauges.setTier(tier);
  if (spectrumStrip) spectrumStrip.setTier(tier);
  if (tier === "full") {
    hudStartLoop();
  } else {
    // low/medium: one static dim frame, no continuous redraw (big CPU save)
    hudStopLoop();
    drawCoreFrame(0);
  }
}

function hudStartLoop() {
  if (hudLoopActive || !ctx) return;
  if (document.hidden) return;  // background: don't burn CPU drawing
  hudLoopActive = true;
  rafId = requestAnimationFrame(drawCoreFrame);
}
function hudStopLoop() {
  hudLoopActive = false;
  if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
}
// pause drawing entirely when the window is hidden, resume on visibility
document.addEventListener("visibilitychange", () => {
  if (document.hidden) hudStopLoop();
  else if (hudTierNow() === "full") hudStartLoop();
});

let _lastCoreDraw = 0;
function drawCoreFrame(ts) {
  // throttle to ~24fps — the old 60fps loop was a big CPU cost
  if (ts - _lastCoreDraw < 41) { if (hudLoopActive) rafId = requestAnimationFrame(drawCoreFrame); return; }
  _lastCoreDraw = ts;
  if (!ctx || !coreCanvas) return;
  const w = coreCanvas.width = coreCanvas.clientWidth || 320;
  const h = coreCanvas.height = coreCanvas.clientHeight || 320;
  const cx = w / 2, cy = h / 2;
  ctx.clearRect(0, 0, w, h);
  const st = document.body.dataset.state;
  const sleeping = document.body.classList.contains("presence-sleeping");
  // REAL spectrum: mic while listening, TTS playback while speaking.
  let freq = null;
  if (!sleeping) {
    if (st === "listening" && voice.micEnabled) freq = voice.getMicSpectrum ? voice.getMicSpectrum(256) : null;
    else if (st === "speaking") freq = voice.getTtsSpectrum ? voice.getTtsSpectrum(256) : null;
  }
  const bars = 48;
  const bins = window.PhantomHud ? window.PhantomHud.avgBins(freq || [], bars) : [];
  const baseR = 58;
  const color = st === "error" ? "rgba(240,113,139,.6)" : "rgba(110,168,254,.5)";
  ctx.lineWidth = 1.4;
  ctx.strokeStyle = color;
  for (let i = 0; i < bars; i++) {
    const a = (i / bars) * Math.PI * 2;
    const amp = bins.length ? bins[i] / 255 : 0;
    const r = baseR + amp * 26;
    const x = cx + Math.cos(a) * r, y = cy + Math.sin(a) * r;
    ctx.beginPath();
    ctx.moveTo(cx + Math.cos(a) * (baseR - 3), cy + Math.sin(a) * (baseR - 3));
    ctx.lineTo(x, y);
    ctx.stroke();
  }
  if (hudLoopActive) rafId = requestAnimationFrame(drawCoreFrame);
}

// static segmented ticks (built once — not re-rendered per frame)
function buildOrbTicks() {
  const svg = $("orbTicks");
  if (!svg) return;
  const N = 72, cx = 160, cy = 160;
  let s = "";
  for (let i = 0; i < N; i++) {
    const a = (i / N) * Math.PI * 2;
    const big = i % 6 === 0;
    const r1 = big ? 148 : 152, r2 = 158;
    s += `<line x1="${(cx + Math.cos(a) * r1).toFixed(1)}" y1="${(cy + Math.sin(a) * r1).toFixed(1)}" x2="${(cx + Math.cos(a) * r2).toFixed(1)}" y2="${(cy + Math.sin(a) * r2).toFixed(1)}" stroke="rgba(110,168,254,.35)" stroke-width="${big ? 2 : 1}"/>`;
  }
  svg.innerHTML = s;
}
buildOrbTicks();
hudApplyTier();

/* ============================== ONBOARDING ============================== */
async function _uiPref(key, dflt) {
  try {
    const s = await api("/api/settings");
    const g = (s.settings && s.settings["*"]) || {};
    return (key in g) ? g[key] : dflt;
  } catch (e) { return dflt; }
}
async function _saveUiPref(key, value) {
  try { await api("/api/settings", { method: "PUT", body: { agent: "*", key, value } }); }
  catch (e) {}
}
async function maybeOnboarding() {
  // server-side first (survives port changes), localStorage as a fast cache
  let onboarded = localStorage.getItem("phantom.onboarded");
  if (!onboarded) onboarded = (await _uiPref("ui.onboarded", false)) ? "1" : "";
  if (!onboarded) { $("onboarding").classList.remove("hidden"); return; }
  // restore name + theme from the server if we have them
  const savedName = await _uiPref("ui.userName", "");
  if (savedName) state.userName = savedName;
  const savedTheme = await _uiPref("ui.theme", "");
  if (savedTheme) { applyTheme(savedTheme); const t = $("themeSel"); if (t) t.value = savedTheme; }
}
window.addEventListener("DOMContentLoaded", () => {
  const done = $("onbDone");
  if (done) done.onclick = async () => {
    state.userName = $("onbName").value.trim() || "friend";
    localStorage.setItem("phantom.userName", state.userName);
    localStorage.setItem("phantom.onboarded", "1");
    _saveUiPref("ui.onboarded", true);
    _saveUiPref("ui.userName", state.userName);
    const voiceSel = $("onbVoice").value;
    const proactive = $("onbProactive").value === "1";
    try {
      await api("/api/voice/config", { method: "PUT", body: {
        stt: { provider: voiceSel }, tts: { provider: voiceSel },
        proactive_speech: proactive } });
    } catch (e) {}
    $("onboarding").classList.add("hidden");
    loadStatus();
    setTimeout(() => {
      if (voice.mode !== "private") voice.speak(`Hello${state.userName ? ", " + state.userName : ""}. I'm Phantom. I'm ready when you are.`);
    }, 700);
  };
});

/* ============================== INIT ============================== */
function toast(msg, type = "") {
  const t = $("toast");
  t.textContent = msg;
  t.classList.remove("hidden", "toast-ok", "toast-err");
  if (type) t.classList.add("toast-" + type);
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => t.classList.add("hidden"), 3500);
}
window.toast = toast;

document.addEventListener("DOMContentLoaded", async () => {
  // apply saved theme (JOOJO yellow or midnight)
  applyTheme(localStorage.getItem("phantom.theme") || "midnight");
  const themeSel = $("themeSel");
  if (themeSel) themeSel.value = localStorage.getItem("phantom.theme") || "midnight";
  // top bar
  document.querySelectorAll(".seg").forEach((b) => b.onclick = () => switchPersona(b.dataset.persona));
  $("drawerBtn").onclick = () => $("drawer").classList.contains("hidden") ? openDrawer() : closeDrawer();
  $("drawerClose").onclick = closeDrawer;
  document.querySelectorAll(".panel-btn").forEach((b) => b.onclick = () => switchPanel(b.dataset.panel));
  $("killSwitch").onclick = toggleKill;

  // presence (wake / silent)
  $("presencePill").onclick = () => {
    // click pill = wake phantom (or the active agent's opposite if asleep)
    api("/api/presence/wake", { body: { agent: state.agent, source: "ui" } }).then(loadPresence);
  };
  $("silentBtn").onclick = () => {
    api("/api/presence/stay-silent").then(loadPresence);
    voice.stopAll(); voice.stopMic();
    toast("🤫 Staying silent until you say the wake word");
  };

  // voice controls
  $("micToggle").onclick = () => {
    voice.micEnabled = !voice.micEnabled;
    if (!voice.micEnabled) { voice.stopListening(); voice.stopMic(); }
    else if (voice.mode === "conversation") voice.startListening();
    $("micToggle").classList.toggle("active", voice.micEnabled);
    toast(voice.micEnabled ? "🎙️ microphone on" : "🎙️ microphone off");
  };
  $("voiceToggle").onclick = () => {
    state.voiceOn = !state.voiceOn;
    if (!state.voiceOn) voice.stopSpeaking();
    $("voiceToggle").classList.toggle("active", state.voiceOn);
    toast(state.voiceOn ? "🔊 voice replies on" : "🔇 voice replies muted");
  };
  $("voiceModeBtn").onclick = async () => {
    const order = ["conversation", "push", "private"];
    const next = order[(order.indexOf(voice.mode) + 1) % order.length];
    voice.setMode(next);
    try { await api("/api/voice/config", { method: "PUT", body: { mode: next } }); }
    catch (e) { toast("✗ " + e.message, "err"); }
    toast(`voice mode: ${next}`);
    $("voiceModeBtn").classList.toggle("active", next !== "private");
  };
  $("pttBtn").onclick = async () => {
    if (voice.state === "LISTENING") { voice.pushToTalkEnd(); }
    else if (voice.state === "SPEAKING") { voice.bargeIn(); voice.pushToTalkStart(); }
    else { await voice.pushToTalkStart(); }
  };

  // composer
  $("sendBtn").onclick = () => { const v = $("input").value; if (v.trim()) { $("input").value = ""; sendMessage(v); } };
  $("input").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") { ev.preventDefault(); $("sendBtn").click(); }
  });

  // chat panel
  $("newConv").onclick = newConversation;
  $("stopRun").onclick = () => { if (state.runId) api(`/api/runs/${state.runId}/cancel`); };

  // confirmation
  $("confirmApprove").onclick = () => decideConfirmation(true);
  $("confirmDeny").onclick = () => decideConfirmation(false);

  // notifications
  $("notifBtn").onclick = () => $("notifPanel").classList.toggle("hidden");
  $("notifClose").onclick = () => $("notifPanel").classList.add("hidden");

  // memory / wellness / health / tasks / audit / permissions / settings
  $("memAgent").onchange = loadMemories; $("memKind").onchange = loadMemories;
  $("memSearchBtn").onclick = loadMemories; $("memAddBtn").onclick = addMemory;
  $("wellnessRefresh").onclick = loadWellness;
  $("wellnessEnable").onclick = () => wellnessToggle(true);
  $("wellnessDisable").onclick = () => wellnessToggle(false);
  $("wellnessExport").onclick = wellnessExport;
  $("healthRefresh").onclick = loadHealth; $("auditRunBtn").onclick = runSelfAudit;
  $("taskAgent").onchange = loadTasks; $("taskStatus").onchange = loadTasks;
  $("auditAgent").onchange = loadAudit; $("auditEvent").onchange = loadAudit;
  $("auditRefresh").onclick = () => { loadAuditEvents(); loadAudit(); };
  $("permAgent").onchange = loadPermissions;

  // settings live-save
  document.addEventListener("change", (ev) => {
    const id = ev.target.id;
    const m = id && id.match(/^(model|temp)-(\w+)$/);
    if (m) {
      const key = m[1] === "model" ? "model" : "model.temperature";
      const val = m[1] === "model" ? ev.target.value.trim() : parseFloat(ev.target.value);
      if (val) saveSetting(m[2], key, val);
    }
    if (id === "quietStart") saveSetting("*", "quiet.start", ev.target.value.trim());
    if (id === "quietEnd") saveSetting("*", "quiet.end", ev.target.value.trim());
    if (id === "sttProvider") { voice.sttProvider = ev.target.value; api("/api/voice/config", { method: "PUT", body: { stt: { provider: ev.target.value } } }).then(() => toast("✓ STT provider saved", "ok")).catch((e) => toast("✗ " + e.message, "err")); }
    if (id === "ttsProvider") { voice.ttsProvider = ev.target.value; api("/api/voice/config", { method: "PUT", body: { tts: { provider: ev.target.value } } }).then(() => toast("✓ TTS provider saved", "ok")).catch((e) => toast("✗ " + e.message, "err")); }
    if (id === "voiceModeSel") { voice.setMode(ev.target.value); api("/api/voice/config", { method: "PUT", body: { mode: ev.target.value } }); }
    if (id === "proactiveSel") api("/api/voice/config", { method: "PUT", body: { proactive_speech: ev.target.value === "1" } });
    if (id === "sttPriority") api("/api/voice/config", { method: "PUT", body: { stt_priority: ev.target.value.split(",").map((s) => s.trim()).filter(Boolean) } }).then(() => { toast("✓ STT priority saved", "ok"); loadVoiceStatus(); }).catch((e) => toast("✗ " + e.message, "err"));
    if (id === "ttsPriority") api("/api/voice/config", { method: "PUT", body: { tts_priority: ev.target.value.split(",").map((s) => s.trim()).filter(Boolean) } }).then(() => { toast("✓ TTS priority saved", "ok"); loadVoiceStatus(); }).catch((e) => toast("✗ " + e.message, "err"));
    if (id === "localModelSel") api("/api/voice/config", { method: "PUT", body: { local_model: ev.target.value } }).then(() => { toast("✓ Local Whisper model: " + ev.target.value, "ok"); loadVoiceStatus(); }).catch((e) => toast("✗ " + e.message, "err"));
    if (id === "vadThreshold") {
      const v = parseFloat(ev.target.value);
      $("vadThresholdLbl").textContent = "sensitivity " + v;
      voice.vadThreshold = v;
      api("/api/voice/config", { method: "PUT", body: { vad_threshold: v } });
    }
    if (id === "autoStopMs") { voice.autoStopMs = parseInt(ev.target.value) || 900; api("/api/voice/config", { method: "PUT", body: { auto_stop_ms: voice.autoStopMs } }); }
    if (id === "maxRecordMs") { voice.maxRecordMs = parseInt(ev.target.value) || 15000; api("/api/voice/config", { method: "PUT", body: { max_record_ms: voice.maxRecordMs } }); }
    if (id === "continuousSel") { voice.continuous = ev.target.value === "1"; api("/api/voice/config", { method: "PUT", body: { continuous: voice.continuous } }); }
    if (id === "themeSel") applyTheme(ev.target.value);
    if (id === "voicePhantom") {
      voice.voices.phantom = ev.target.value;
      api("/api/voice/config", { method: "PUT", body: { voices: { phantom: ev.target.value } } }).then(() => toast("✓ Phantom voice saved", "ok")).catch((e) => toast("✗ " + e.message, "err"));
      if (state.agent === "phantom") voice.ttsVoice = ev.target.value;
    }
    if (id === "voiceCoded") {
      voice.voices.coded = ev.target.value;
      api("/api/voice/config", { method: "PUT", body: { voices: { coded: ev.target.value } } }).then(() => toast("✓ Coded voice saved", "ok")).catch((e) => toast("✗ " + e.message, "err"));
      if (state.agent === "coded") voice.ttsVoice = ev.target.value;
    }
  });

  // updater — the buttons live in Settings (rendered later); inline onclick
  // handlers cover clicks, so guard everything here. Never let a null deref
  // kill the rest of startup (voice.init/WS/presence) again.
  if (updater) {
    updater.onStatus((s) => { updaterState = s || {}; renderUpdater(); });
    const chkBtn = $("updateCheckBtn"); if (chkBtn) chkBtn.onclick = checkForUpdates;
    const instBtn = $("updateInstallBtn"); if (instBtn) instBtn.onclick = () => updater.install();
    updater.getVersion().then((v) => { updaterVersion = v || ""; window.appVersion = v || window.appVersion || ""; renderUpdater(); }).catch(() => {});
    renderUpdater();
    setTimeout(checkForUpdates, 4000); // auto-check shortly after launch
  }

  showSplash(1400);
  await voice.init();
  await loadPresence();
  await loadStatus();
  if (typeof wireHudClickables === "function") wireHudClickables();  // home gauges clickable from first paint
  loadApprovals();  // badge count on the drawer
  await refreshNotifications();
  switchPersona("phantom");
  newConversation();
  maybeOnboarding();
  connectWS();

  // start conversation listening by default (if permitted)
  if (voice.mode === "conversation" && voice.micEnabled && !state.killEngaged) {
    setTimeout(() => voice.startListening(), 1200);
  }

  setInterval(loadStatus, 20000);
  setInterval(() => { if (state.currentPanel === "audit") loadAudit(); }, 30000);
});
