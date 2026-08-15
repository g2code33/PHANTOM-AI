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
  agent: "phantom", conversations: [], currentConv: null,
  runId: null, running: false, killEngaged: false, voiceOn: true,
  pendingConfirmations: {}, userName: localStorage.getItem("phantom.userName") || "",
  micLevel: 0, transcript: [],
};

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
voice.onFinal = async (text) => {
  $("voiceHint").textContent = "";
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
  setPresence("WAKING", `“${agent}” heard — verifying…`);
  const wav = await voice.captureWav(2);
  if (!wav) { setPresence("IDLE", "Couldn't capture audio for verification"); return; }
  const reader = new FileReader();
  const b64 = await new Promise((res) => { reader.onload = () => res(String(reader.result).split(",")[1]); reader.readAsDataURL(wav); });
  try {
    const res = await api("/api/presence/wake", { body: { agent, sample_wav: b64 } });
    if (res.woken) {
      voice.playReadyCue();
      setPresence("LISTENING", (agent === "coded" ? "Coded" : "Phantom") + " is ready — speak");
      if (state.voiceOn) voice.speak("Yes, " + (state.userName || "JOOJO") + "?");
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
let ws = null;
function connectWS() {
  const base = (WS_BASE || location.origin).replace(/\/+$/, "");
  const proto = base.startsWith("https") ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${base.replace(/^https?:\/\//, "")}/ws`);
  ws.onmessage = (ev) => { try { handleEvent(JSON.parse(ev.data)); } catch (e) {} };
  ws.onclose = () => { setPresence("DISCONNECTED"); setTimeout(connectWS, 1500); };
  ws.onopen = () => { ws.send(JSON.stringify({ type: "ping" })); if (!state.running) setPresence("IDLE"); };
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
  switch (event) {
    case "agent.chunk":
      if (run_id === state.runId) {
        appendChunk(data.text);
        if (state.voiceOn) pushChunkToSpeech(data.text);
      }
      break;
    case "agent.run_started":
      if (run_id === state.runId) {
        state.running = true; setPresence("THINKING");
        showRunIndicator(); setContextLine(data);
      }
      break;
    case "tool.started":
      if (run_id === state.runId) setPresence("EXECUTING");
      addToolCard(data, "started"); addActivity("tool", `🔧 ${data.name}`, "tool-start");
      break;
    case "tool.completed":
      if (run_id === state.runId) setPresence("THINKING");
      updateToolCard(data, true);
      addActivity("tool", `✓ ${data.name} · ${(data.latency_ms || 0).toFixed(0)}ms`, "tool-ok");
      break;
    case "tool.error":
      if (run_id === state.runId) setPresence("THINKING");
      updateToolCard(data, false);
      addActivity("tool", `✗ ${data.name}: ${data.message}`, "tool-err");
      break;
    case "agent.run_completed":
      if (run_id === state.runId) {
        state.running = false; hideRunIndicator(); setContextLine(null);
        finalizeAssistantMessage(data);
        loadConversations();
        flushSpeech();
        if (data.status === "ok" && state.voiceOn && data.content && voice.mode !== "private") {
          voice.speak(data.content); // full reply in case chunks were missed
        } else {
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
      break;
    case "confirmation.decided":
      if (data.confirmation) delete state.pendingConfirmations[data.confirmation.id];
      hideConfirmation(data.confirmation && data.confirmation.id);
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
    default: break;
  }
}

function resumeListeningAfterReply() {
  if (voice.mode === "conversation" && !state.killEngaged && voice.micEnabled) {
    setTimeout(() => { if (!state.running) voice.startListening(); }, 350);
  } else {
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
  div.innerHTML = `<div class="md">${who ? `<span class="muted small">${esc(who)} · </span>` : ""}${role === "user" ? esc(content) : renderMarkdown(content)}</div>`;
  host.appendChild(div);
  host.scrollTop = host.scrollHeight;
  return div.querySelector(".md");
}

function renderMessages(msgs) {
  const host = $("messages");
  host.innerHTML = "";
  const full = $("fullTranscript");
  if (full) full.innerHTML = "";
  for (const m of msgs || []) {
    if (m.role === "tool") continue;
    if (m.role === "user") addMessageEl("user", m.content || "");
    else if (m.role === "assistant") addMessageEl("assistant", m.content || "…");
  }
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
  if (!clean || state.running) return;
  if (state.killEngaged) { toast("⛔ Kill switch is engaged"); return; }
  if (opts.via === "voice") {
    voice.stopListening(); // pause recognition while thinking/replying
  }
  addMessageEl("user", clean);
  setPresence("THINKING");
  try {
    const res = await api(`/api/agents/${state.agent}/chat`, {
      body: { text: clean, conversation_id: state.currentConv || "", session_id: "web" },
    });
    state.runId = res.run_id;
    state.currentConv = res.conversation_id;
    activeStreamEl = null;
    sentenceBuf = "";
    loadConversations();
  } catch (err) {
    state.running = false;
    addMessageEl("error", `Could not start: ${err.message}`);
    setPresence("ERROR");
    setTimeout(() => setPresence("IDLE"), 2500);
  }
}

async function loadConversations() {
  const res = await api(`/api/conversations?agent=${state.agent}`);
  state.conversations = res.conversations || [];
  const list = $("convList");
  if (!list) return;
  list.innerHTML = "";
  for (const c of state.conversations.slice(0, 40)) {
    const item = document.createElement("div");
    item.className = "conv-item" + (c.id === state.currentConv ? " active" : "");
    item.innerHTML = `<span>${esc(c.title || "Untitled")}</span><span class="conv-date">${timeAgo(c.updated_at)} · ${c.message_count || 0}</span>`;
    item.onclick = () => openConversation(c.id);
    list.appendChild(item);
  }
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
  if (state.running) { toast("Wait for the current run to finish"); return; }
  state.agent = agent;
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
  if (panel === "memory") loadMemories();
  if (panel === "wellness") loadWellness();
  if (panel === "health") loadHealth();
  if (panel === "tasks") loadTasks();
  if (panel === "audit") { loadAuditEvents(); loadAudit(); }
  if (panel === "permissions") loadPermissions();
  if (panel === "settings") loadSettings();
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
      if (!level) await api("/api/permissions", { body: { agent, tool, delete: true } });
      else await api("/api/permissions", { body: { agent, tool, level } });
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
          <input type="password" id="key-${agentId}" placeholder="${k.configured ? "configured — type to replace" : "not set"}">
          <button class="btn" onclick="saveKey('${agentId}')">Save</button>
          ${k.configured ? `<span class="muted small">${esc(k.masked || "")}</span>` : ""}</div>
        <div class="row"><label>Model</label><input type="text" id="model-${agentId}"></div>
        <div class="row"><label>Temperature</label><input type="text" id="temp-${agentId}" style="width:80px"></div>
      </div>`);
  }
  sections.push(`
    <div class="settings-section"><h3>🎙️ Voice</h3>
      <div class="row"><label>STT provider</label>
        <select id="sttProvider"><option value="browser">browser (offline)</option><option value="deepgram">deepgram (online)</option></select></div>
      <div class="row"><label>TTS provider</label>
        <select id="ttsProvider"><option value="browser">browser (offline)</option><option value="deepgram">deepgram (online)</option></select></div>
      <div class="row"><label>Voice mode</label>
        <select id="voiceModeSel"><option value="private">private</option><option value="push">push-to-talk</option><option value="conversation">conversation</option></select></div>
      <div class="row"><label>Proactive speech</label>
        <select id="proactiveSel"><option value="0">off</option><option value="1">on</option></select></div>
      <div class="row"><label>Deepgram API key</label>
        <input type="password" id="deepgramKey" placeholder="${res.voice?.deepgram_masked ? "configured — type to replace" : "not set"}">
        <button class="btn" onclick="saveDeepgram()">Save</button></div>
      <p class="muted small">The Deepgram key stays server-side; the UI only ever gets a short-lived token.</p>
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
    <div class="settings-section"><h3>⬆ App updates</h3>
      <div class="row"><label>Desktop app</label><span id="updateState" class="muted">checking…</span>
        <button id="updateCheckBtn" class="btn">Check now</button>
        <button id="updateInstallBtn" class="btn btn-primary hidden">Restart &amp; install</button></div>
    </div>
    <div class="settings-section"><h3>📱 Mobile / remote backend</h3>
      <div class="row"><label>Backend URL</label><input type="text" id="apiBase" placeholder="http://192.168.1.50:8000">
        <button class="btn" onclick="saveApiBase()">Save</button></div>
    </div>`);
  body.innerHTML = sections.join("");
  for (const agentId of ["phantom", "coded", "health"]) {
    const s = (res.settings && res.settings[agentId]) || {};
    $("model-" + agentId).value = s.model || "";
    $("temp-" + agentId).value = s["model.temperature"] ?? 0.4;
  }
  const g = res.settings && res.settings["*"] ? res.settings["*"] : {};
  $("quietStart").value = g["quiet.start"] || "";
  $("quietEnd").value = g["quiet.end"] || "";
  $("apiBase").value = localStorage.getItem("phai.apiBase") || "";
  const vc = res.voice || {};
  $("sttProvider").value = vc.stt?.provider || "browser";
  $("ttsProvider").value = vc.tts?.provider || "browser";
  $("voiceModeSel").value = vc.mode || "conversation";
  $("proactiveSel").value = vc.proactive_speech ? "1" : "0";
  await Promise.all([loadBrainKeys(), loadEnrollStatus()]);
  await loadSchedules();
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
  await api(`/api/brains/${bid}/config`, { body });
  $(`bkey-${bid}`).value = "";
  toast(`Saved config for ${bid}`);
  loadBrainKeys();
};
window.clearBrainKey = async (bid) => {
  await api(`/api/brains/${bid}/config`, { body: { delete_key: true } });
  toast(`Removed own key for ${bid} — now shares Phantom's`);
  loadBrainKeys();
};

/* voice enrollment */
async function loadEnrollStatus() {
  const [p, c] = await Promise.all([
    api("/api/voice/enroll/status?agent=phantom"),
    api("/api/voice/enroll/status?agent=coded"),
  ]);
  $("enrollPhantomStatus").textContent = p.enrolled ? "✅ enrolled" : (p.engine.available ? "not enrolled" : "engine unavailable: " + (p.engine.reason || "").slice(0, 60));
  $("enrollCodedStatus").textContent = c.enrolled ? "✅ enrolled" : (c.engine.available ? "not enrolled" : "engine unavailable");
  $("speakerLockCb").checked = p.speaker_lock;
}
async function enrollVoice(agent) {
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
    if (res.error) { toast("Enroll failed: " + res.error); return; }
    if (res.enrolled) { toast(`✅ ${agent} voice enrolled`); break; }
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
    await api("/api/voice/enroll/speaker-lock", { body: { enabled: lockCb.checked } });
    toast(lockCb.checked ? "🔒 Speaker lock ON — only your voice wakes them" : "Speaker lock off");
  };
});
async function saveSetting(agent, key, value) {
  await api("/api/settings", { body: { agent, key, value } });
}
window.saveKey = async (agentId) => {
  const val = $(`key-${agentId}`).value.trim();
  if (!val) return;
  await api("/api/settings", { body: { agent: agentId, key: "nvidia_api_key", value: val } });
  $(`key-${agentId}`).value = ""; toast("Key saved"); loadSettings(); loadStatus();
};
window.saveDeepgram = async () => {
  const val = $("deepgramKey").value.trim();
  if (!val) return;
  await api("/api/voice/config", { body: { deepgram_api_key: val } });
  $("deepgramKey").value = ""; toast("Deepgram key saved (server-side)"); loadSettings();
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
window.toggleSchedule = async (id, on) => { await api(`/api/schedules/${id}`, { body: { enabled: !!on } }); loadSchedules(); };
window.deleteSchedule = async (id) => { await api(`/api/schedules/${id}`, { method: "DELETE" }); loadSchedules(); };
window.saveApiBase = () => {
  localStorage.setItem("phai.apiBase", $("apiBase").value.trim());
  toast("Backend URL saved — reloading…"); setTimeout(() => location.reload(), 600);
};

/* ============================== UPDATER ============================== */
const updater = window.phaiUpdater || null;
let updaterState = { state: "idle" };
function renderUpdater() {
  const installBtn = $("updateInstallBtn"); const stateEl = $("updateState");
  if (!updater) { if (stateEl) stateEl.textContent = "packaged app only"; return; }
  const s = updaterState;
  let text = "not checked";
  if (s.state === "checking") text = "checking…";
  else if (s.state === "up-to-date") text = `up to date (v${s.version})`;
  else if (s.state === "available") text = `update available: v${s.version} — downloading…`;
  else if (s.state === "ready") text = `update ready: v${s.version} — restart to install`;
  else if (s.state === "downloading") text = `downloading… ${s.percent || 0}%`;
  else if (s.state === "error") {
    text = `update error: ${s.message || ""}`;
    // deb installs live in root-owned /opt — electron-updater can't write there.
    const msg = String(s.message || "").toLowerCase();
    if (msg.includes("eacces") || msg.includes("permission") || msg.includes("denied")) {
      text += " — .deb installs need sudo: use the AppImage, or run the update script from Settings.";
    }
  } else if (s.state === "dev") text = "dev mode";
  if (stateEl) stateEl.textContent = text;
  if (installBtn) installBtn.classList.toggle("hidden", s.state !== "ready");
}
async function checkForUpdates() {
  if (!updater) return;
  updaterState = { state: "checking" }; renderUpdater();
  const res = await updater.check().catch((e) => ({ state: "error", message: String(e) }));
  updaterState = res || {}; renderUpdater();
  if (updaterState.state === "available") {
    toast(`⬆ Update v${updaterState.version} found — downloading…`);
    updater.download();
  } else if (updaterState.state === "ready") {
    toast(`⬆ Update v${updaterState.version} ready — restart to install`);
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

/* ============================== CORE CANVAS ============================== */
const coreCanvas = $("coreCanvas");
const ctx = coreCanvas && coreCanvas.getContext("2d");
let rafId = null;
function drawCore(ts) {
  if (!ctx || !coreCanvas) return;
  const w = coreCanvas.width = coreCanvas.clientWidth || 320;
  const h = coreCanvas.height = coreCanvas.clientHeight || 320;
  const cx = w / 2, cy = h / 2;
  ctx.clearRect(0, 0, w, h);
  const st = document.body.dataset.state;
  const level = micLevelTarget || 0;
  const bars = 40;
  const baseR = 62;
  for (let i = 0; i < bars; i++) {
    const a = (i / bars) * Math.PI * 2 + ts / 4000;
    let amp = 0.5;
    if (st === "listening") amp = 0.35 + level * 2.2;
    else if (st === "speaking") amp = 0.5 + Math.abs(Math.sin(ts / 90 + i * 0.6)) * 0.9;
    else if (st === "thinking" || st === "executing") amp = 0.4 + Math.abs(Math.sin(ts / 220 + i)) * 0.7;
    else amp = 0.25 + Math.sin(ts / 900 + i * 0.3) * 0.12;
    const r = baseR + amp * 16;
    const x = cx + Math.cos(a) * r, y = cy + Math.sin(a) * r;
    ctx.strokeStyle = st === "error" ? "rgba(240,113,139,.55)" : "rgba(110,168,254,.4)";
    ctx.lineWidth = 1.4;
    ctx.beginPath();
    ctx.moveTo(cx + Math.cos(a) * (baseR - 4), cy + Math.sin(a) * (baseR - 4));
    ctx.lineTo(x, y);
    ctx.stroke();
  }
  // thinking: orbiting particles
  if (st === "thinking" || st === "executing" || st === "verifying") {
    for (let i = 0; i < 14; i++) {
      const a = (i / 14) * Math.PI * 2 + ts / 600;
      const r = 105 + Math.sin(ts / 500 + i) * 10;
      ctx.fillStyle = "rgba(183,155,255,.5)";
      ctx.beginPath();
      ctx.arc(cx + Math.cos(a) * r, cy + Math.sin(a) * r, 1.8, 0, Math.PI * 2);
      ctx.fill();
    }
  }
  rafId = requestAnimationFrame(drawCore);
}
rafId = requestAnimationFrame(drawCore);

/* ============================== ONBOARDING ============================== */
function maybeOnboarding() {
  if (localStorage.getItem("phantom.onboarded")) return;
  $("onboarding").classList.remove("hidden");
}
window.addEventListener("DOMContentLoaded", () => {
  const done = $("onbDone");
  if (done) done.onclick = async () => {
    state.userName = $("onbName").value.trim() || "friend";
    localStorage.setItem("phantom.userName", state.userName);
    localStorage.setItem("phantom.onboarded", "1");
    const voiceSel = $("onbVoice").value;
    const proactive = $("onbProactive").value === "1";
    try {
      await api("/api/voice/config", { body: {
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
function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => t.classList.add("hidden"), 3500);
}
window.toast = toast;

document.addEventListener("DOMContentLoaded", async () => {
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
    await api("/api/voice/config", { body: { mode: next } });
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
    if (id === "sttProvider") api("/api/voice/config", { body: { stt: { provider: ev.target.value } } });
    if (id === "ttsProvider") api("/api/voice/config", { body: { tts: { provider: ev.target.value } } });
    if (id === "voiceModeSel") { voice.setMode(ev.target.value); api("/api/voice/config", { body: { mode: ev.target.value } }); }
    if (id === "proactiveSel") api("/api/voice/config", { body: { proactive_speech: ev.target.value === "1" } });
  });

  // updater
  if (updater) {
    updater.onStatus((s) => { updaterState = s || {}; renderUpdater(); });
    $("updateCheckBtn").onclick = checkForUpdates;
    $("updateInstallBtn").onclick = () => updater.install();
    renderUpdater();
    setTimeout(checkForUpdates, 4000);
  }

  await voice.init();
  await loadPresence();
  await loadStatus();
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
