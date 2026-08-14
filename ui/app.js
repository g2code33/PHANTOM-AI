/* PHANTOM + CODED — UI logic (vanilla JS, no build step) */
"use strict";

// API base: in the browser this is empty (same origin as the Python backend).
// Inside the Capacitor app there is no backend on device, so a backend URL can
// be configured (Settings → Backend URL, persisted to localStorage).
const API_BASE = (localStorage.getItem("phai.apiBase") || "").replace(/\/+$/, "");
const WS_BASE = API_BASE ? API_BASE.replace(/^http/, "ws") : "";

const AGENTS = { phantom: { name: "Phantom", emoji: "👻", color: "var(--phantom)" },
                 coded:   { name: "Coded",   emoji: "💻", color: "var(--coded)" } };

const state = {
  agent: "phantom",
  conversations: [],
  currentConv: null,
  view: "chats",
  runId: null,
  running: false,
  pendingConfirmations: {},
  killEngaged: false,
  voiceOn: false,
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

/* ============================== WEBSOCKET ============================== */
let ws = null;
function connectWS() {
  const base = (WS_BASE || location.origin).replace(/\/+$/, "");
  const proto = base.startsWith("https") ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${base.replace(/^https?:\/\//, "")}/ws`);
  ws.onmessage = (ev) => { try { handleEvent(JSON.parse(ev.data)); } catch (e) {} };
  ws.onclose = () => setTimeout(connectWS, 1500);
  ws.onopen = () => ws.send(JSON.stringify({ type: "ping" }));
}

function handleEvent(payload) {
  const { event, data, agent, run_id } = payload;
  switch (event) {
    case "agent.chunk":
      if (run_id && run_id === state.runId) appendChunk(data.text);
      break;
    case "agent.run_started":
      if (run_id === state.runId) { state.running = true; showRunIndicator(); }
      break;
    case "agent.run_completed":
      if (run_id === state.runId) {
        state.running = false; hideRunIndicator();
        finalizeAssistantMessage(data);
        loadConversations();
        if (state.voiceOn && data.content && data.status === "ok") speak(data.content);
      }
      addActivity("agent", `${AGENTS[data.agent || agent]?.emoji || ""} ${data.status} run · ${(data.latency_ms||0).toFixed(0)}ms`, "ok");
      break;
    case "tool.started":
      addToolCard(data, "started");
      addActivity("tool", `🔧 ${data.name} ${briefArgs(data.arguments)}`, "tool-start");
      break;
    case "tool.completed":
      updateToolCard(data, true);
      addActivity("tool", `✓ ${data.name} · ${(data.latency_ms||0).toFixed(0)}ms`, "tool-ok");
      break;
    case "tool.error":
      updateToolCard(data, false);
      addActivity("tool", `✗ ${data.name}: ${data.message}`, "tool-err");
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
    case "task.update":
      if (state.view === "tasks") loadTasks();
      break;
    case "delegation.started":
      addActivity("delegation", `⇄ ${AGENTS[data.origin]?.name} → ${AGENTS[data.target]?.name}: ${data.objective}`, "delegation");
      break;
    case "delegation.completed":
      addActivity("delegation", `⇄ delegation ${data.status}`, data.status === "completed" ? "tool-ok" : "tool-err");
      break;
    case "killswitch.state":
      setKillState(data.engaged);
      break;
    case "heartbeat.run":
      toast(`💓 heartbeat: ${data.name}`);
      break;
    default:
      break;
  }
}

/* ============================== MARKDOWN ============================== */
function renderMarkdown(src) {
  const s = String(src ?? "");
  let out = esc(s);
  out = out.replace(/```(\w*)\n?([\s\S]*?)```/g, (m, lang, code) => `<pre><code>${code.trim()}</code></pre>`);
  out = out.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  out = out.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/(^|\n)#{1,4} (.*)/g, (m, nl, t) => `${nl}<h4>${t}</h4>`);
  out = out.replace(/^[-*] (.*)$/gm, '<div class="li">$1</div>');
  out = out.replace(/^(\d+)\. (.*)$/gm, '<div class="li">$1. $2</div>');
  out = out.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  out = out.replace(/\n{2,}/g, "</p><p>");
  out = out.replace(/\n/g, "<br>");
  return `<p>${out}</p>`;
}

/* ============================== CHAT ============================== */
const messagesEl = $("messages");

function addMessageEl(role, content, extraClass = "") {
  const div = document.createElement("div");
  div.className = `msg ${role} ${extraClass}`;
  const roleLabel = role === "user" ? (AGENTS[state.agent]?.emoji || "") + " You"
    : role === "assistant" ? (AGENTS[state.agent]?.emoji || "") + " " + (AGENTS[state.agent]?.name || "Assistant")
    : role === "error" ? "⚠️ system" : "system";
  div.innerHTML = `<div class="msg-role">${esc(roleLabel)}</div><div class="md">${role === "user" ? esc(content) : renderMarkdown(content)}</div>`;
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return div.querySelector(".md");
}

function renderMessages(msgs) {
  messagesEl.innerHTML = "";
  let pendingToolCalls = [];
  for (const m of msgs) {
    if (m.role === "tool") {
      const tc = (m.tool_results && m.tool_results[0]) || {};
      addToolCardStatic({ name: tc.name || "tool", output: m.content || "", success: tc.success !== false, callId: tc.tool_call_id });
      continue;
    }
    if (m.tool_calls && m.tool_calls.length) {
      for (const tc of m.tool_calls) pendingToolCalls.push(tc);
    }
    if (m.role === "assistant" && pendingToolCalls.length && !m.content) {
      for (const tc of pendingToolCalls) addToolCardStatic({ name: tc.name, arguments: tc.arguments || {} });
      pendingToolCalls = [];
      continue;
    }
    if (m.role === "assistant") {
      const md = addMessageEl("assistant", m.content || "…");
      for (const tc of pendingToolCalls) addToolCardStatic({ name: tc.name, arguments: tc.arguments || {} });
      pendingToolCalls = [];
      if (md) md.dataset.runId = m.id;
      continue;
    }
    if (m.role === "user") addMessageEl("user", m.content || "");
  }
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

let activeStreamEl = null;
let activeRunToolCalls = [];

function appendChunk(text) {
  if (!activeStreamEl) {
    activeStreamEl = addMessageEl("assistant", "");
  }
  activeStreamEl.innerHTML = renderMarkdown(activeStreamEl.textContent + text);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function finalizeAssistantMessage(data) {
  activeStreamEl = null;
  activeRunToolCalls = [];
  if (data.status === "cancelled") addMessageEl("system", `⏹ run stopped${data.error ? `: ${data.error}` : ""}`);
  else if (data.status === "error") addMessageEl("error", data.error || "run failed");
  else if (!data.content) addMessageEl("assistant", "(no textual answer)");
}

function showRunIndicator() {
  $("runIndicator").classList.remove("hidden");
  $("stopRun").classList.remove("hidden");
  $("sendBtn").disabled = true;
}
function hideRunIndicator() {
  $("runIndicator").classList.add("hidden");
  $("stopRun").classList.add("hidden");
  $("sendBtn").disabled = false;
}

function briefArgs(args) {
  try {
    const a = args || {};
    return Object.keys(a).length ? JSON.stringify(a).slice(0, 90) : "";
  } catch (e) { return ""; }
}

function addToolCard(data, phase) {
  const div = document.createElement("div");
  div.className = `tool-card ${phase === "started" ? "running" : "ok"}`;
  div.id = `tool-${data.tool_call_id || Math.random().toString(36).slice(2)}`;
  div.innerHTML = `
    <div class="tc-head">
      <span class="tc-status">${phase === "started" ? "◌" : "✓"}</span>
      <span class="tc-name">${esc(data.name)}</span>
      <span class="tc-meta">${esc(briefArgs(data.arguments))}</span>
      <span class="tc-meta">▾</span>
    </div>
    <div class="tc-body"><div class="tc-label">arguments</div><pre>${esc(briefArgs(data.arguments))}</pre></div>`;
  div.querySelector(".tc-head").onclick = () => div.classList.toggle("open");
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function updateToolCard(data, ok) {
  const el = document.getElementById(`tool-${data.tool_call_id}`);
  if (!el) return;
  el.classList.remove("running");
  el.classList.add(ok ? "ok" : "err");
  const st = el.querySelector(".tc-status"); if (st) st.textContent = ok ? "✓" : "✗";
  const body = el.querySelector(".tc-body");
  if (body) {
    body.innerHTML = `<div class="tc-label">output</div><pre>${esc((data.output || data.message || "").slice(0, 4000))}</pre>`;
  }
}

function addToolCardStatic(data) {
  const div = document.createElement("div");
  div.className = `tool-card ${data.success === false ? "err" : "ok"}`;
  div.innerHTML = `
    <div class="tc-head">
      <span class="tc-status">${data.success === false ? "✗" : "✓"}</span>
      <span class="tc-name">${esc(data.name || "tool")}</span>
      <span class="tc-meta">▾</span>
    </div>
    <div class="tc-body"><pre>${esc((data.output || JSON.stringify(data.arguments || {}, null, 2)).slice(0, 4000))}</pre></div>`;
  div.querySelector(".tc-head").onclick = () => div.classList.toggle("open");
  messagesEl.appendChild(div);
}

async function sendMessage() {
  const input = $("input");
  const text = input.value.trim();
  if (!text || state.running) return;
  if (state.killEngaged) { toast("⛔ Kill switch is engaged"); return; }
  input.value = "";
  input.style.height = "auto";
  addMessageEl("user", text);
  try {
    const res = await api(`/api/agents/${state.agent}/chat`, {
      body: { text, conversation_id: state.currentConv || "", session_id: "web" },
    });
    state.runId = res.run_id;
    state.currentConv = res.conversation_id;
    activeStreamEl = null;
    if (state.voiceOn) { try { speechSynthesis.cancel(); } catch (e) {} }
    loadConversations();
  } catch (err) {
    addMessageEl("error", `Could not start: ${err.message}`);
  }
}

async function loadConversations() {
  const res = await api(`/api/conversations?agent=${state.agent}`);
  state.conversations = res.conversations || [];
  const list = $("convList");
  list.innerHTML = "";
  for (const c of state.conversations.slice(0, 50)) {
    const item = document.createElement("div");
    item.className = "conv-item" + (c.id === state.currentConv ? " active" : "");
    item.innerHTML = `<span>${esc(c.title || "Untitled")}</span><span class="conv-date">${timeAgo(c.updated_at)} · ${c.message_count || 0} msgs</span>`;
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
  messagesEl.innerHTML = "";
  addMessageEl("system", `New conversation with ${AGENTS[state.agent].name}. Say hello.`);
}

async function switchAgent(agent) {
  if (state.running) { toast("Wait for the current run to finish"); return; }
  state.agent = agent;
  document.querySelectorAll(".agent-tab").forEach((b) => b.classList.toggle("active", b.dataset.agent === agent));
  $("chatAgentFace").textContent = AGENTS[agent].emoji;
  $("chatAgentName").textContent = AGENTS[agent].name;
  $("input").placeholder = `Message ${AGENTS[agent].name}… (Enter to send)`;
  state.currentConv = null;
  messagesEl.innerHTML = "";
  addMessageEl("system", `${AGENTS[agent].emoji} ${AGENTS[agent].name} — separate identity, memory and API key.`);
  await loadConversations();
  await loadStatus();
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
  $("confirmModal").dataset.agent = data.agent || c.agent;
}
function hideConfirmation(cid) {
  if (!cid || $("confirmModal").dataset.cid === cid) $("confirmModal").classList.add("hidden");
}
async function decideConfirmation(approve) {
  const cid = $("confirmModal").dataset.cid;
  if (!cid) return;
  try {
    await api(`/api/confirmations/${cid}/${approve ? "approve" : "deny"}`);
    toast(approve ? "✅ Approved — action executed" : "⛔ Denied — action not executed");
  } catch (e) {
    toast("Error: " + e.message);
  }
  hideConfirmation(cid);
}

/* ============================== MEMORY ============================== */
async function loadMemories() {
  const agent = $("memAgent").value;
  const kind = $("memKind").value;
  let res;
  const q = $("memSearch").value.trim();
  if (q) res = await api(`/api/memories/search?agent=${agent}&q=${encodeURIComponent(q)}`);
  else res = await api(`/api/memories?agent=${agent}&kind=${encodeURIComponent(kind)}`);
  const list = $("memoryList");
  list.innerHTML = "";
  const rows = res.memories || [];
  if (!rows.length) { list.innerHTML = `<div class="card muted">No memories yet.</div>`; return; }
  for (const m of rows) {
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `
      <div class="card-title">🧠 ${esc(m.content.slice(0, 120))}</div>
      <div class="card-meta">
        <span class="pill info">${esc(m.kind)}</span>
        <span class="pill ${m.agent === "shared" ? "warn" : "ok"}">${m.agent === "shared" ? "shared" : esc(m.agent)}</span>
        <span>imp ${Number(m.importance).toFixed(2)}</span>
        ${(m.tags || []).map((t) => `<span class="tag">${esc(t)}</span>`).join(" ")}
        <span>${timeAgo(m.updated_at)}</span>
      </div>
      <div class="card-body">${esc(m.content)}</div>
      <div class="card-actions">
        <button class="mini-btn" onclick="forgetMemory('${m.id}')">Forget</button>
        <button class="mini-btn" onclick="deleteMemory('${m.id}')">Delete forever</button>
      </div>`;
    list.appendChild(card);
  }
}
async function forgetMemory(id) { await api(`/api/memories/${id}/forget`); loadMemories(); toast("Memory forgotten"); }
async function deleteMemory(id) { await api(`/api/memories/${id}`, { method: "DELETE" }); loadMemories(); toast("Memory deleted"); }
async function addMemory() {
  const content = $("memNewContent").value.trim();
  if (!content) return;
  await api("/api/memories", { body: { agent: $("memAgent").value, content, kind: $("memKind").value || "fact" } });
  $("memNewContent").value = "";
  loadMemories();
  toast("Memory stored");
}

/* ============================== TASKS ============================== */
async function loadTasks() {
  const agent = $("taskAgent").value;
  const status = $("taskStatus").value;
  const res = await api(`/api/tasks?agent=${encodeURIComponent(agent)}&status=${encodeURIComponent(status)}`);
  const list = $("taskList");
  const rows = res.tasks || [];
  $("taskCount").textContent = rows.filter((t) => t.status === "running" || t.status === "queued").length || "";
  list.innerHTML = "";
  if (!rows.length) { list.innerHTML = `<div class="card muted">No tasks.</div>`; return; }
  for (const t of rows) {
    const cls = t.status === "completed" ? "ok" : t.status === "running" ? "warn" : t.status === "failed" ? "err" : "info";
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `
      <div class="card-title">
        ${AGENTS[t.agent]?.emoji || ""} ${esc(t.name)}
        <span class="pill ${cls}">${esc(t.status)}</span>
        <span class="pill info">${esc(t.kind)}</span>
      </div>
      <div class="card-meta">
        <span>created ${timeAgo(t.created_at)}</span>
        ${t.started_at ? `<span>started ${timeAgo(t.started_at)}</span>` : ""}
        ${t.ended_at ? `<span>ended ${timeAgo(t.ended_at)}</span>` : ""}
      </div>
      ${t.error ? `<div class="card-body">error: ${esc(t.error)}</div>` : ""}
      ${(t.logs || []).length ? `<div class="card-body">${esc((t.logs || []).join("\n"))}</div>` : ""}
      <div class="card-actions">
        ${(t.status === "running" || t.status === "queued") ? `<button class="mini-btn" onclick="cancelTask('${t.id}')">Cancel</button>` : ""}
      </div>`;
    list.appendChild(card);
  }
}
async function cancelTask(id) { await api(`/api/tasks/${id}/cancel`); loadTasks(); }

/* ============================== HEALTH / EVOLUTION ============================== */
async function loadHealth() {
  const [brains, graph, proposals, snapshots] = await Promise.all([
    api("/api/brains").catch(() => ({ brains: [] })),
    api("/api/graph").catch(() => ({ stats: {} })),
    api("/api/proposals").catch(() => ({ proposals: [] })),
    api("/api/snapshots").catch(() => ({ snapshots: [] })),
  ]);
  const stats = graph.stats || {};
  $("healthSummary").textContent = `${(brains.brains || []).length} brains · ${stats.nodes || 0} graph nodes · ${stats.edges || 0} edges`;
  $("graphStats").textContent = `nodes: ${stats.nodes || 0} · edges: ${stats.edges || 0} · ${JSON.stringify(stats.by_type || {})}`;

  // brains
  const bl = $("brainList");
  bl.innerHTML = "";
  for (const b of brains.brains || []) {
    const card = document.createElement("div");
    card.className = "card";
    const stateCls = b.health_state === "online" ? "ok" : b.health_state === "degraded" ? "warn" : "info";
    card.innerHTML = `
      <div class="card-title">
        🧬 ${esc(b.name)} <span class="muted small">(${esc(b.id)})</span>
        <span class="pill ${stateCls}">${esc(b.health_state || "unknown")}</span>
        <span class="pill ${b.status === "active" ? "ok" : "err"}">${esc(b.status)}</span>
        <span class="pill info">v${b.version || 1}</span>
      </div>
      <div class="card-meta">
        <span>model: <b>${esc(b.model)}</b></span>
        <span>role: ${esc(b.role)}</span>
        <span>tools: ${b.tools === "all" ? "all" : esc((b.tools || []).join(", "))}</span>
      </div>
      <div class="card-meta">
        <span>success: <b>${b.success_rate != null ? (b.success_rate * 100).toFixed(0) + "%" : "—"}</b></span>
        <span>latency: ${b.avg_latency_ms != null ? b.avg_latency_ms.toFixed(0) + " ms" : "—"}</span>
        <span>error rate: ${b.error_rate != null ? (b.error_rate * 100).toFixed(0) + "%" : "—"}</span>
        <span>tasks: ${b.tasks || 0}</span>
        <span>last active: ${b.last_active ? timeAgo(b.last_active) : "—"}</span>
      </div>`;
    bl.appendChild(card);
  }
  if (!(brains.brains || []).length) bl.innerHTML = `<div class="card muted">No brains registered.</div>`;

  // proposals
  const pl = $("proposalList");
  pl.innerHTML = "";
  for (const p of proposals.proposals || []) {
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `
      <div class="card-title">💡 ${esc(p.title)}
        <span class="pill ${p.status === "deployed" ? "ok" : p.status === "proposed" ? "warn" : "info"}">${esc(p.status)}</span>
        <span class="pill ${p.risk === "high" ? "err" : p.risk === "medium" ? "warn" : "info"}">${esc(p.risk)} risk</span>
        <span class="pill info">${esc(p.kind)}</span>
      </div>
      <div class="card-body small">${esc(p.description)}</div>
      <div class="card-meta">by ${esc(p.created_by)} · ${timeAgo(p.created_at)}${p.result ? " · " + esc(String(p.result).slice(0, 120)) : ""}</div>
      <div class="card-actions">
        ${p.status === "proposed" ? `<button class="mini-btn" onclick="approveProposal('${p.id}')">Approve & deploy</button>
          <button class="mini-btn" onclick="rejectProposal('${p.id}')">Reject</button>` : ""}
      </div>`;
    pl.appendChild(card);
  }
  if (!(proposals.proposals || []).length) pl.innerHTML = `<div class="card muted">No proposals yet — ask Evolution to run a self-audit.</div>`;

  // snapshots
  const sl = $("snapshotList");
  sl.innerHTML = "";
  for (const s of snapshots.snapshots || []) {
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `
      <div class="card-title">📸 ${esc(s.label)} <span class="muted small">${esc(s.id.slice(0, 8))}</span>
        <span class="pill info">${esc(s.kind)}</span>
        ${s.restored_at ? `<span class="pill warn">restored ${timeAgo(s.restored_at)}</span>` : ""}
      </div>
      <div class="card-meta">${timeAgo(s.created_at)}${s.previous_id ? ` · prev: ${esc(s.previous_id.slice(0, 8))}` : ""}</div>
      <div class="card-actions"><button class="mini-btn" onclick="restoreSnapshot('${s.id}')">Restore (rollback)</button></div>`;
    sl.appendChild(card);
  }
  if (!(snapshots.snapshots || []).length) sl.innerHTML = `<div class="card muted">No snapshots yet.</div>`;
}
async function approveProposal(id) {
  if (!confirm("Approve & deploy this proposal? A pre-change snapshot will be created.")) return;
  const res = await api(`/api/proposals/${id}/approve`);
  toast(`Proposal ${res.status}`);
  loadHealth();
}
async function rejectProposal(id) { await api(`/api/proposals/${id}/reject`); loadHealth(); }
async function createSnapshot() { await api("/api/snapshots", { body: { label: "manual snapshot", description: "from Health dashboard" } }); toast("Snapshot created"); loadHealth(); }
async function restoreSnapshot(id) {
  if (!confirm("Restore this snapshot? Current configuration will be overwritten.")) return;
  await api(`/api/snapshots/${id}/restore`, { body: { reason: "manual rollback from Health dashboard" } });
  toast("Snapshot restored");
  loadHealth();
}
async function trackGraphNode() {
  const nodeType = $("graphNodeType").value.trim();
  const label = $("graphNodeLabel").value.trim();
  if (!nodeType || !label) { toast("node type + label required"); return; }
  const res = await api("/api/graph/track", { body: { node_type: nodeType, label } });
  toast(`Tracked ${nodeType}: ${label}`);
  $("graphNodeLabel").value = "";
  loadHealth();
}
async function runLoopTask() {
  const objective = $("loopObjective").value.trim();
  if (!objective) { toast("objective required"); return; }
  const res = await api("/api/loops/run", { body: {
    agent: $("loopAgent").value,
    objective,
    max_iterations: parseInt($("loopIters").value, 10) || 4,
    failure_threshold: parseInt($("loopFail").value, 10) || 2,
    rollback: true,
  }});
  $("loopResult").classList.remove("hidden");
  $("loopResult").textContent = `Loop task started: ${res.name} (${res.id}) — status ${res.status}. Watch the Tasks view.`;
  toast("Loop task launched");
}
async function runSelfAudit() {
  const res = await api("/api/evolution/audit", { body: { period: "daily" } });
  const card = document.createElement("div");
  card.className = "card";
  card.innerHTML = `<div class="card-title">📋 Daily self-audit</div><div class="card-body mono small">${esc(res.report)}</div>`;
  $("proposalList").prepend(card);
  toast("Self-audit generated");
}

/* ============================== AUDIT ============================== */
async function loadAuditEvents() {
  const res = await api("/api/audit/events");
  const sel = $("auditEvent");
  sel.innerHTML = `<option value="">All events</option>` +
    (res.events || []).map((e) => `<option value="${esc(e)}">${esc(e)}</option>`).join("");
}
async function loadAudit() {
  const agent = $("auditAgent").value;
  const event = $("auditEvent").value;
  const res = await api(`/api/audit?agent=${encodeURIComponent(agent)}&event=${encodeURIComponent(event)}&limit=300`);
  const list = $("auditList");
  const rows = res.events || [];
  list.innerHTML = "";
  if (!rows.length) { list.innerHTML = `<div class="card muted">No audit events.</div>`; return; }
  for (const r of rows) {
    const d = r.detail || {};
    const evCls = r.event.includes("error") || r.event.includes("denied") ? "err"
      : r.event.includes("approved") ? "ok" : r.event.includes("requested") || r.event.includes("started") ? "warn" : "info";
    const card = document.createElement("div");
    card.className = "card";
    const detailText = JSON.stringify(d, null, 2).slice(0, 1200);
    card.innerHTML = `
      <div class="card-title">
        <span class="pill ${evCls}">${esc(r.event)}</span>
        ${AGENTS[r.agent]?.emoji || ""} <span class="muted">${esc(r.agent || "*")}</span>
        <span class="muted small">${esc(r.ts || "")}</span>
        ${r.latency_ms != null ? `<span class="muted small">${Number(r.latency_ms).toFixed(0)} ms</span>` : ""}
        ${r.model ? `<span class="muted small">${esc(r.model)}</span>` : ""}
      </div>
      <div class="card-body mono small">${esc(detailText)}</div>`;
    list.appendChild(card);
  }
}

/* ============================== PERMISSIONS ============================== */
async function loadPermissions() {
  const agent = $("permAgent").value;
  const res = await api("/api/permissions");
  const rows = (res.agents && res.agents[agent]) || [];
  const list = $("permTable");
  list.innerHTML = "";
  const head = document.createElement("div");
  head.className = "card perm-row head";
  head.innerHTML = `<div class="col">tool</div><div class="col">default</div><div class="col">effective</div><div class="col">source</div><div class="col">override</div><div class="col">description</div>`;
  list.appendChild(head);
  for (const t of rows) {
    const card = document.createElement("div");
    card.className = "card perm-row";
    card.innerHTML = `
      <div class="col mono">${esc(t.tool)}</div>
      <div class="col"><span class="pill info">${esc(t.default)}</span></div>
      <div class="col"><span class="pill ${t.effective === "blocked" ? "err" : t.effective === "confirm_required" ? "warn" : t.effective === "read_only" ? "ok" : "info"}">${esc(t.effective)}</span></div>
      <div class="col small muted">${esc(t.source)}</div>
      <div class="col">
        <select class="perm-override" data-tool="${esc(t.tool)}">
          <option value="">default</option>
          ${["read_only", "safe_action", "confirm_required", "high_risk", "blocked"]
            .map((l) => `<option value="${l}" ${t.override === l ? "selected" : ""}>${l}</option>`).join("")}
        </select>
      </div>
      <div class="col small muted">${esc(t.description)}</div>`;
    list.appendChild(card);
  }
  list.querySelectorAll(".perm-override").forEach((sel) => {
    sel.onchange = async () => {
      const tool = sel.dataset.tool;
      const level = sel.value;
      if (!level) await api("/api/permissions", { body: { agent, tool, delete: true } });
      else await api("/api/permissions", { body: { agent, tool, level } });
      toast(`Permission updated: ${tool} → ${level || "default"}`);
      loadPermissions();
    };
  });
}

/* ============================== SETTINGS ============================== */
async function loadSettings() {
  const res = await api("/api/settings");
  const keys = res.keys || {};
  const body = $("settingsBody");
  const sections = [];

  for (const agentId of ["phantom", "coded"]) {
    const k = keys[agentId] || {};
    const meta = AGENTS[agentId];
    sections.push(`
      <div class="settings-section">
        <h3>${meta.emoji} ${meta.name} — API & Model</h3>
        <div class="row"><label>NVIDIA API key (${esc(k.env || "")})</label>
          <input type="password" id="key-${agentId}" placeholder="${k.configured ? "configured — type to replace" : "not set"}" value="">
          <button class="btn" onclick="saveKey('${agentId}')">Save</button>
          ${k.configured ? `<span class="muted small">${esc(k.masked || "")}</span>` : ""}
        </div>
        <div class="row"><label>Model</label>
          <input type="text" id="model-${agentId}" value="">
        </div>
        <div class="row"><label>Temperature</label>
          <input type="text" id="temp-${agentId}" style="width:80px">
          <span class="muted small">max tokens</span>
          <input type="text" id="maxtok-${agentId}" style="width:80px">
        </div>
        <div class="row"><label>Context window (messages)</label>
          <input type="text" id="window-${agentId}" style="width:80px">
        </div>
        <div class="row"><label>Confirmation timeout (s)</label>
          <input type="text" id="conf-${agentId}" style="width:80px">
        </div>
      </div>`);
  }

  sections.push(`
    <div class="settings-section">
      <h3>🔁 Heartbeat & Quiet hours</h3>
      <div class="row"><label>Quiet hours start (UTC)</label><input type="text" id="quietStart" placeholder="22:00"></div>
      <div class="row"><label>Quiet hours end (UTC)</label><input type="text" id="quietEnd" placeholder="07:00"></div>
      <div class="row"><label>Max concurrent tasks</label><input type="text" id="taskMax" style="width:80px"></div>
      <div class="row"><label>Delegation max loops / 30 min</label><input type="text" id="delLoops" style="width:80px"></div>
    </div>
    <div class="settings-section">
      <h3>🗓️ Schedules</h3>
      <div id="schedList"></div>
      <div class="row" style="margin-top:10px">
        <select id="schedAgent">${Object.entries(AGENTS).map(([a, m]) => `<option value="${a}">${m.emoji} ${m.name}</option>`).join("")}</select>
        <input type="text" id="schedName" placeholder="name" style="width:120px">
        <input type="text" id="schedExpr" placeholder="every 30m | hourly | daily at 09:00" style="width:190px">
      </div>
      <div class="row">
        <input type="text" id="schedPrompt" placeholder="prompt for the agent" style="width:430px">
        <button class="btn btn-primary" onclick="addSchedule()">Add schedule</button>
      </div>
    </div>
    <div class="settings-section">
      <h3>🎙️ Voice</h3>
      <div class="row"><label>STT provider</label>
        <select id="sttProvider"><option value="browser">browser (Web Speech, push-to-talk)</option><option value="openai-compatible">openai-compatible (server)</option></select>
      </div>
      <div class="row"><label>TTS provider</label>
        <select id="ttsProvider"><option value="browser">browser speechSynthesis</option><option value="openai-compatible">openai-compatible (server)</option></select>
      </div>
    </div>
    <div class="settings-section">
      <h3>⬆ App updates</h3>
      <div class="row"><label>Desktop app</label>
        <span id="updateState" class="muted">checking…</span>
        <button id="updateCheckBtn" class="btn">Check now</button>
        <button id="updateInstallBtn" class="btn btn-primary hidden">Restart &amp; install</button>
      </div>
      <p class="muted small">The desktop app self-updates from GitHub Releases (latest.yml / latest-linux.yml). When the version in package.json is increased and a new release is published, the ⬆ button in the top bar offers the update. Packaged app only — dev mode and the browser show the status only.</p>
    </div>
    <div class="settings-section">
      <h3>📱 Mobile / remote backend</h3>
      <div class="row"><label>Backend URL (for the Android app)</label>
        <input type="text" id="apiBase" placeholder="http://192.168.1.50:8000">
        <button class="btn" onclick="saveApiBase()">Save</button>
        <span class="muted small">Where the app can reach the PHANTOM + CODED backend. Leave empty in the desktop/browser build.</span>
      </div>
    </div>
    <div class="settings-section">
      <h3>🛡️ Safety</h3>
      <div class="row"><label>Access token (PHAI_ACCESS_TOKEN)</label><span class="muted small">set via environment variable before start</span></div>
    </div>`);

  body.innerHTML = sections.join("");

  // populate values
  for (const agentId of ["phantom", "coded"]) {
    const s = (res.settings && res.settings[agentId]) || {};
    $("model-" + agentId).value = s.model || "";
    $("temp-" + agentId).value = s["model.temperature"] ?? 0.4;
    $("maxtok-" + agentId).value = s["model.max_tokens"] ?? 2048;
    $("window-" + agentId).value = s["context.window"] ?? 24;
    $("conf-" + agentId).value = s["confirmation.timeout"] ?? 900;
  }
  const g = res.settings && res.settings["*"] ? res.settings["*"] : {};
  $("quietStart").value = g["quiet.start"] || "";
  $("quietEnd").value = g["quiet.end"] || "";
  $("taskMax").value = g["task.max_concurrent"] ?? 4;
  $("delLoops").value = g["delegation.max_loops"] ?? 3;
  $("apiBase").value = localStorage.getItem("phai.apiBase") || "";
  await loadSchedules();
}

function saveApiBase() {
  const val = $("apiBase").value.trim();
  localStorage.setItem("phai.apiBase", val);
  toast(val ? `Backend URL set — reloading…` : "Backend URL cleared — reloading…");
  setTimeout(() => location.reload(), 600);
}
async function saveSetting(agent, key, value, el) {
  await api("/api/settings", { body: { agent, key, value } });
  toast(`Saved ${key}`);
}
async function saveKey(agentId) {
  const val = $(`key-${agentId}`).value.trim();
  if (!val) return;
  await api("/api/settings", { body: { agent: agentId, key: "nvidia_api_key", value: val } });
  $(`key-${agentId}`).value = "";
  toast(`Key saved for ${AGENTS[agentId].name} — provider rebuilt`);
  loadSettings();
  loadStatus();
}

async function loadSchedules() {
  const res = await api("/api/schedules");
  const list = $("schedList");
  const rows = res.schedules || [];
  list.innerHTML = rows.length ? "" : `<div class="muted small">No schedules yet.</div>`;
  for (const s of rows) {
    const div = document.createElement("div");
    div.className = "card";
    div.innerHTML = `
      <div class="card-title">${AGENTS[s.agent]?.emoji || ""} ${esc(s.name)}
        <span class="pill ${s.enabled ? "ok" : "err"}">${s.enabled ? "enabled" : "disabled"}</span>
        <span class="pill info">${esc(s.expression)}</span>
        ${s.quiet_start ? `<span class="pill warn">quiet ${esc(s.quiet_start)}–${esc(s.quiet_end)}</span>` : ""}
      </div>
      <div class="card-meta"><span>next: ${esc(s.next_run_at || "pending")}</span>
        ${s.last_status ? `<span>last: <b>${esc(s.last_status)}</b></span>` : ""}</div>
      <div class="card-body small">${esc(s.prompt)}</div>
      <div class="card-actions">
        <button class="mini-btn" onclick="toggleSchedule('${s.id}', ${s.enabled ? 0 : 1})">${s.enabled ? "Disable" : "Enable"}</button>
        <button class="mini-btn" onclick="deleteSchedule('${s.id}')">Delete</button>
      </div>`;
    list.appendChild(div);
  }
}
async function addSchedule() {
  const body = {
    agent: $("schedAgent").value,
    name: $("schedName").value.trim() || "Scheduled check",
    expression: $("schedExpr").value.trim(),
    prompt: $("schedPrompt").value.trim(),
    quiet_start: $("quietStart").value.trim(),
    quiet_end: $("quietEnd").value.trim(),
  };
  if (!body.expression || !body.prompt) { toast("Need expression + prompt"); return; }
  try {
    await api("/api/schedules", { body });
    toast("Schedule added");
    $("schedExpr").value = ""; $("schedPrompt").value = "";
    loadSchedules();
  } catch (e) { toast("Error: " + e.message); }
}
async function toggleSchedule(id, enabled) { await api(`/api/schedules/${id}`, { body: { enabled: !!enabled } }); loadSchedules(); }
async function deleteSchedule(id) { await api(`/api/schedules/${id}`, { method: "DELETE" }); loadSchedules(); }

/* ============================== NOTIFICATIONS ============================== */
async function refreshNotifications() {
  const res = await api("/api/notifications");
  const rows = res.notifications || [];
  $("notifBadge").textContent = rows.filter((n) => !n.read).length || "";
  $("notifBadge").classList.toggle("hidden", !rows.filter((n) => !n.read).length);
  $("notifList").innerHTML = rows.length ? "" : `<div class="notif-item muted">No notifications.</div>`;
  for (const n of rows) {
    const div = document.createElement("div");
    div.className = "notif-item" + (n.read ? " muted" : "");
    div.innerHTML = `
      <div class="n-title">${esc(n.title)}</div>
      <div class="n-body">${esc(n.body)}</div>
      <div class="n-actions">
        <button class="mini-btn" onclick="readNotif('${n.id}')">Mark read</button>
        <button class="mini-btn" onclick="dismissNotif('${n.id}')">Dismiss</button>
      </div>`;
    $("notifList").appendChild(div);
  }
}
async function readNotif(id) { await api(`/api/notifications/${id}/read`); refreshNotifications(); }
async function dismissNotif(id) { await api(`/api/notifications/${id}/dismiss`); refreshNotifications(); }

/* ============================== TOOL LAB ============================== */
async function loadToolLab() {
  const res = await api(`/api/tools?agent=${state.agent}`);
  const tools = res.tools || [];
  $("labTool").innerHTML = tools.map((t) => `<option value="${esc(t.name)}">${esc(t.name)}</option>`).join("");
  if (tools[0]) $("labToolDesc").textContent = tools[0].description;
  $("labTool").onchange = () => {
    const t = tools.find((x) => x.name === $("labTool").value);
    $("labToolDesc").textContent = t ? `${t.description} — permission: ${t.permission}` : "";
  };
}
async function runLabTool() {
  const name = $("labTool").value;
  let args = {};
  try { args = JSON.parse($("labArgs").value || "{}"); }
  catch (e) { toast("Arguments must be valid JSON"); return; }
  $("labOutput").classList.remove("hidden");
  $("labOutput").textContent = "running…";
  try {
    const res = await api(`/api/tools/${name}/run`, { body: { agent: state.agent, arguments: args } });
    $("labOutput").textContent = (res.success ? "✓ " : "✗ ") + (res.result || res.error || JSON.stringify(res.data, null, 2));
  } catch (err) {
    $("labOutput").textContent = "✗ " + err.message;
  }
}

/* ============================== ACTIVITY ============================== */
function addActivity(kind, text, cls) {
  const feed = $("activityFeed");
  const div = document.createElement("div");
  div.className = `activity-item ${cls || ""}`;
  div.innerHTML = `<div class="a-time">${new Date().toLocaleTimeString()}</div><div class="a-text">${esc(text)}</div>`;
  feed.prepend(div);
  while (feed.children.length > 80) feed.lastChild.remove();
}

/* ============================== STATUS / KILL ============================== */
let statusTimer = null;
async function loadStatus() {
  try {
    const res = await api("/api/status");
    const providers = res.providers || {};
    const p = providers[state.agent] || {};
    const dot = $("statusDot");
    dot.className = "dot " + (p.ok ? "ok" : "bad");
    $("statusText").textContent = p.ok
      ? `${AGENTS[state.agent].name}: ${p.model || ""} · connected`
      : `${AGENTS[state.agent].name}: ${(p.detail || "offline").slice(0, 46)}`;
    setKillState(res.killswitch && res.killswitch.engaged);
    const pending = res.pending_confirmations || {};
    const totalPending = Object.values(pending).reduce((a, b) => a + b, 0);
    if (totalPending) toast(`⏳ ${totalPending} action(s) awaiting approval`);
  } catch (e) {}
}
function setKillState(engaged) {
  state.killEngaged = !!engaged;
  $("killBanner").classList.toggle("hidden", !engaged);
  $("killSwitch").style.opacity = engaged ? 0.4 : 1;
  $("disengageBtn").onclick = () => api("/api/killswitch/disengage").then(loadStatus);
}
async function toggleKill() {
  if (state.killEngaged) return;
  if (!confirm("Engage the KILL SWITCH?\n\nThis stops all background activity, pending tasks and agent runs. You can disarm it anytime.")) return;
  await api("/api/killswitch/engage", { body: { reason: "manual (UI button)" } });
  toast("⛔ Kill switch engaged");
  loadStatus();
}

/* ============================== VOICE ============================== */
let recognition = null;
let listening = false;
function initVoice() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) {
    $("pttBtn").title = "STT not supported in this browser";
    $("pttBtn").style.opacity = 0.35;
    return;
  }
  recognition = new SR();
  recognition.lang = "en-US";
  recognition.interimResults = true;
  recognition.continuous = false;
  recognition.onresult = (ev) => {
    let text = "";
    for (let i = ev.resultIndex; i < ev.results.length; i++) text += ev.results[i][0].transcript;
    $("input").value = text;
    $("sttStatus").textContent = "listening… press 🎙️ to stop";
  };
  recognition.onend = () => { listening = false; $("pttBtn").classList.remove("listening"); $("sttStatus").textContent = ""; };
  recognition.onerror = (ev) => { $("sttStatus").textContent = "STT error: " + ev.error; listening = false; $("pttBtn").classList.remove("listening"); };
}
function togglePTT() {
  if (!recognition) return;
  if (listening) { recognition.stop(); listening = false; $("pttBtn").classList.remove("listening"); return; }
  listening = true;
  $("pttBtn").classList.add("listening");
  try { recognition.start(); } catch (e) {}
}
function speak(text) {
  try {
    speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(text.replace(/[#*`>\-]/g, "").slice(0, 1200));
    u.rate = 1.05;
    speechSynthesis.speak(u);
  } catch (e) {}
}

/* ============================== UPDATER ============================== */
const updater = window.phaiUpdater || null;
let updaterState = { state: "idle" };
function renderUpdater() {
  const btn = $("updateBtn");
  const installBtn = $("updateInstallBtn");
  const stateEl = $("updateState");
  if (!updater) {
    if (btn) btn.classList.add("hidden");
    if (stateEl) stateEl.textContent = "not available (run the packaged desktop app)";
    return;
  }
  const s = updaterState;
  if (s.state === "available" || s.state === "ready") {
    btn.classList.remove("hidden");
    btn.textContent = s.state === "ready" ? "🔄 Restart" : `⬆ v${s.version}`;
    btn.title = s.state === "ready" ? "Restart & install the update" : `Update to v${s.version}`;
    if (installBtn) {
      installBtn.classList.toggle("hidden", s.state !== "ready");
      if (s.state === "ready") installBtn.textContent = `Restart & install v${s.version}`;
    }
  } else {
    btn.classList.add("hidden");
  }
  if (stateEl) {
    stateEl.textContent = s.state === "checking" ? "checking for updates…"
      : s.state === "up-to-date" ? `up to date (v${s.version})`
      : s.state === "downloading" ? `downloading… ${s.percent || 0}%`
      : s.state === "error" ? `update error: ${s.message || "unknown"}`
      : s.state === "dev" ? "dev mode — updates only in the packaged app"
      : s.state === "available" ? `update available: v${s.version}`
      : s.state === "ready" ? `update ready: v${s.version} — restart to install`
      : "not checked yet";
  }
}
async function checkForUpdates() {
  if (!updater) return;
  updaterState = { state: "checking" };
  renderUpdater();
  const res = await updater.check().catch((e) => ({ state: "error", message: String(e) }));
  updaterState = res || { state: "error", message: "no response" };
  renderUpdater();
  if (updaterState.state === "available") {
    toast(`⬆ Update available: v${updaterState.version}`);
    updater.download();
  } else if (updaterState.state === "up-to-date") {
    toast("✅ You're on the latest version");
  } else if (updaterState.state === "dev") {
    toast("📦 Updates work in the packaged app (dev mode skipped)");
  }
}
async function installUpdate() {
  if (!updater) return;
  updater.install();
}

/* ============================== VIEW SWITCH ============================== */
function switchView(view) {
  state.view = view;
  document.querySelectorAll(".view-btn").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${view}`));
  if (view === "memory") loadMemories();
  if (view === "health") loadHealth();
  if (view === "tasks") loadTasks();
  if (view === "audit") { loadAuditEvents(); loadAudit(); }
  if (view === "permissions") loadPermissions();
  if (view === "settings") loadSettings();
}

/* ============================== INIT ============================== */
function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => t.classList.add("hidden"), 3500);
}

document.addEventListener("DOMContentLoaded", async () => {
  // agent tabs
  document.querySelectorAll(".agent-tab").forEach((b) => b.onclick = () => switchAgent(b.dataset.agent));

  // view switching
  document.querySelectorAll(".view-btn").forEach((b) => b.onclick = () => switchView(b.dataset.view));

  // chat controls
  $("sendBtn").onclick = sendMessage;
  $("input").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); sendMessage(); }
    ev.target.style.height = "auto";
    ev.target.style.height = Math.min(ev.target.scrollHeight, 180) + "px";
  });
  $("stopRun").onclick = () => { if (state.runId) api(`/api/runs/${state.runId}/cancel`); };
  $("newConv").onclick = newConversation;
  $("pttBtn").onclick = togglePTT;
  $("voiceToggle").onclick = () => {
    state.voiceOn = !state.voiceOn;
    $("voiceToggle").style.opacity = state.voiceOn ? 1 : 0.45;
    toast(state.voiceOn ? "🔊 Replies will be spoken" : "🔇 Voice replies off");
  };

  // kill switch
  $("killSwitch").onclick = toggleKill;

  // confirmation modal
  $("confirmApprove").onclick = () => decideConfirmation(true);
  $("confirmDeny").onclick = () => decideConfirmation(false);

  // notifications
  $("notifBtn").onclick = () => $("notifPanel").classList.toggle("hidden");
  $("notifClose").onclick = () => $("notifPanel").classList.add("hidden");

  // right panel tabs
  document.querySelectorAll(".rp-tab").forEach((b) => b.onclick = () => {
    document.querySelectorAll(".rp-tab").forEach((x) => x.classList.toggle("active", x === b));
    document.querySelectorAll(".rp-body").forEach((x) => x.classList.toggle("active", x.id === `rp-${b.dataset.rp}`));
  });

  // memory controls
  $("memAgent").onchange = loadMemories;
  $("memKind").onchange = loadMemories;
  $("memSearchBtn").onclick = loadMemories;
  $("memAddBtn").onclick = addMemory;
  $("memNewContent").addEventListener("keydown", (ev) => { if (ev.key === "Enter") addMemory(); });

  // tasks controls
  $("taskAgent").onchange = loadTasks;
  $("taskStatus").onchange = loadTasks;

  // audit controls
  $("auditAgent").onchange = loadAudit;
  $("auditEvent").onchange = loadAudit;
  $("auditRefresh").onclick = () => { loadAuditEvents(); loadAudit(); };

  // permissions
  $("permAgent").onchange = loadPermissions;

  // health / evolution
  $("healthRefresh").onclick = loadHealth;
  $("auditRunBtn").onclick = runSelfAudit;

  // updater wiring
  if (updater) {
    updater.onStatus((s) => { updaterState = s || {}; renderUpdater(); });
    $("updateBtn").onclick = () => { if (updaterState.state === "ready") installUpdate(); else checkForUpdates(); };
    const checkBtn = $("updateCheckBtn");
    const installBtn = $("updateInstallBtn");
    if (checkBtn) checkBtn.onclick = checkForUpdates;
    if (installBtn) installBtn.onclick = installUpdate;
    renderUpdater();
    setTimeout(checkForUpdates, 2500); // auto-check shortly after start
  } else {
    renderUpdater();
  }

  // settings live-save
  document.addEventListener("change", (ev) => {
    const id = ev.target.id;
    const match = id && id.match(/^(model|temp|maxtok|window|conf)-(\w+)$/);
    if (match) {
      const key = { model: "model", temp: "model.temperature", maxtok: "model.max_tokens",
                    window: "context.window", conf: "confirmation.timeout" }[match[1]];
      const val = match[1] === "model" ? ev.target.value.trim()
        : match[1] === "window" || match[1] === "conf" ? parseInt(ev.target.value, 10) || 0
        : parseFloat(ev.target.value);
      if (match[1] !== "model" && !val) return;
      saveSetting(match[2], key, val);
    }
    if (id === "quietStart") saveSetting("*", "quiet.start", ev.target.value.trim());
    if (id === "quietEnd") saveSetting("*", "quiet.end", ev.target.value.trim());
    if (id === "taskMax") saveSetting("*", "task.max_concurrent", parseInt(ev.target.value, 10) || 4);
    if (id === "delLoops") saveSetting("*", "delegation.max_loops", parseInt(ev.target.value, 10) || 3);
    if (id === "sttProvider") saveSetting("*", "voice.stt", { provider: ev.target.value });
    if (id === "ttsProvider") saveSetting("*", "voice.tts", { provider: ev.target.value });
  });

  // tool lab
  $("labRun").onclick = runLabTool;

  await switchAgent("phantom");
  await newConversation();
  await loadStatus();
  await refreshNotifications();
  loadToolLab();
  connectWS();
  initVoice();
  statusTimer = setInterval(loadStatus, 15000);
  setInterval(() => { if (state.view === "audit") loadAudit(); }, 20000);
});
