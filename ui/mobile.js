/* Phantom Companion — full phone app (tabs: Chat / Today / Settings).
 * Modes: auto | pc | portable (cloud). Honest online/offline state with
 * health polling + reconnection; chat works on PC (agent core) or cloud. */
"use strict";

const S = {
  base: localStorage.getItem("phai.companion.url") || "",
  token: localStorage.getItem("phai.companion.token") || "",
  cloudBase: localStorage.getItem("phai.companion.cloud") || "",
  cloudToken: localStorage.getItem("phai.companion.cloudtoken") || "",
  mode: "auto",
  agent: "phantom",
  ws: null,
  wsRetries: 0,
  connected: false,
  connLabel: "",
  healthTimer: null,
  recording: false,
  mediaRec: null,
  chunks: [],
  history: [],            // chat messages [{role,text}]
  convByAgent: {},        // pc-mode conversation ids
  polling: false,
};

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function effectiveBase() { return S.mode === "portable" ? S.cloudBase : S.base; }
function effectiveToken() { return S.mode === "portable" ? S.cloudToken : S.token; }

async function api(path, opts = {}) {
  const base = effectiveBase();
  if (!base) throw new Error("no backend configured");
  const headers = { ...(opts.headers || {}) };
  const tok = effectiveToken();
  if (tok) headers["X-Access-Token"] = tok;
  if (S.mode === "portable" && opts.raw) {
    const res = await fetch(base + path, { headers, method: opts.method || "POST", body: opts.body });
    return res;
  }
  const res = await fetch(base + path, { headers, method: opts.method || (opts.body ? "POST" : "GET"), body: opts.body });
  if (!res.ok) {
    let d = res.statusText;
    try { const j = await res.json(); d = j.detail || j.error || d; } catch (e) {}
    if (res.status === 401 && S.mode === "portable") {
      setStatus(false);
      switchTab("settings");
      toastHint("Cloud needs its token — paste it in Settings");
      throw new Error("unauthorized — add the cloud token in Settings");
    }
    throw new Error(d || `HTTP ${res.status}`);
  }
  return res.json();
}

/* ============================ TABS ============================ */
function switchTab(name) {
  document.querySelectorAll(".mTab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + name));
  document.querySelectorAll(".tabBtn").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
}
document.querySelectorAll(".tabBtn").forEach((b) => b.onclick = () => switchTab(b.dataset.tab));

/* ============================ STATUS / MODE ============================ */
function setStatus(connected, label) {
  S.connected = connected;
  S.connLabel = label || "";
  const p = $("mPresence");
  const b = $("mModeBanner");
  const cc = $("chatConn");
  if (!connected) {
    p.textContent = "offline";
    p.className = "mPill warn";
    b.textContent = "📡 offline — check settings";
    b.className = "mBanner off";
  } else {
    p.textContent = label;
    p.className = "mPill on";
    if (S.mode === "portable") { b.textContent = "☁️ portable cloud — PC not needed"; b.className = "mBanner portable"; }
    else { b.textContent = "🖥️ connected to PC"; b.className = "mBanner pc"; }
  }
  if (cc) cc.textContent = connected ? ("● " + (label || "")) : "○ offline";
  const send = $("chatSend"); const inp = $("chatInput");
  if (send) send.disabled = !connected;
  if (inp) inp.disabled = !connected;
}
function applyMode() {
  const t = $("mModeToggle");
  t.classList.toggle("active", S.mode === "portable");
  $("mModeLabel").textContent = S.mode === "portable" ? "cloud" : S.mode === "pc" ? "PC" : "auto";
  document.querySelectorAll(".modeBtn").forEach((b) => b.classList.toggle("active", b.dataset.mode === S.mode));
  const desc = $("modeDesc");
  if (desc) desc.textContent = S.mode === "auto" ? "Auto prefers your PC, falls back to cloud." : S.mode === "pc" ? "Uses your PC only (same Wi-Fi or tunnel)." : "Uses the cloud Worker — works with your PC off.";
}
function setMode(m) {
  S.mode = m;
  localStorage.setItem("phai.companion.mode", m);
  applyMode();
  detectAndConnect();
}
// hard refresh: bust the service-worker cache + reload fresh
async function hardRefresh() {
  toastHint("refreshing…");
  try {
    const regs = await (navigator.serviceWorker ? navigator.serviceWorker.getRegistrations() : []);
    await Promise.all(regs.map((r) => r.unregister()));
  } catch (e) {}
  try { caches.keys().then((ks) => ks.forEach((k) => caches.delete(k))); } catch (e) {}
  location.reload();
}
$("mRefreshBtn").onclick = hardRefresh;
$("mRefreshBtn2").onclick = hardRefresh;

$("mModeToggle").onclick = () => {
  const order = ["auto", "pc", "portable"];
  setMode(order[(order.indexOf(S.mode) + 1) % order.length]);
  toastHint("mode: " + S.mode);
};
document.querySelectorAll(".modeBtn").forEach((b) => b.onclick = () => setMode(b.dataset.mode));

// Cloud-mode Today data: briefing + reminders (needs the cloud token).
let portableTimer = null;
async function refreshPortable() {
  try {
    const b = await api("/api/briefing");
    renderBriefing({ spoken: b.spoken });
    const r = await api("/api/reminders");
    renderNotifs((r.reminders || []).map((x) => ({ title: "⏰ Reminder", body: x.text })));
    renderConfirmations([]);
    renderTasks([]);
  } catch (e) { /* token missing handled by api() */ }
}

// Check one endpoint; returns true when reachable.
async function checkHealth() {
  if (S.mode === "portable") {
    try { const st = await api("/api/status"); setStatus(true, "☁️ " + (st.profile_name || "portable")); refreshPortable(); return true; }
    catch (e) { setStatus(false); return false; }
  }
  // pc or auto: probe the PC (only when a PC URL exists)
  if (S.base) {
    try {
      const st = await api("/api/companion/status");
      setStatus(true, st.presence?.state === "listening" ? "🟢 awake" : "💤 sleeping");
      renderToday(st);
      return true;
    } catch (e) { /* pc down */ }
  }
  setStatus(false);
  return false;
}
// Connect with REAL fallback: auto prefers the PC when reachable, otherwise
// uses the cloud — even when no PC URL is configured (the bug: opening the
// workers.dev link sat on 'No PC URL' forever).
async function detectAndConnect() {
  if (S.mode === "auto") {
    let pcOk = false;
    if (S.base) pcOk = await checkHealth();
    if (pcOk) {
      connectWS();
      if (S.connected) refreshToday();
      return;
    }
    if (S.cloudBase) {
      S.mode = "portable";
      localStorage.setItem("phai.companion.mode", "portable");
      applyMode();
      toastHint("☁️ using portable cloud");
      await checkHealth();
      return;
    }
    setStatus(false);
    switchTab("settings");
    toastHint("Add your PC URL or cloud URL in Settings");
    return;
  }
  if (S.mode === "portable") {
    closeWS();
    if (!S.cloudBase) { setStatus(false); switchTab("settings"); toastHint("No cloud URL — add it in Settings"); return; }
    if (portableTimer) clearInterval(portableTimer);
    portableTimer = setInterval(refreshPortable, 20000);
    await checkHealth();
    return;
  }
  if (portableTimer) { clearInterval(portableTimer); portableTimer = null; }
  // pc mode
  if (!S.base) { setStatus(false); switchTab("settings"); toastHint("No PC URL — add it in Settings"); return; }
  const ok = await checkHealth();
  if (ok) { connectWS(); refreshToday(); }
}

/* ============================ TODAY ============================ */
async function refreshToday() {
  try {
    const st = await api("/api/companion/status");
    renderToday(st);
  } catch (e) { setStatus(false); }
}
function renderToday(st) {
  renderBriefing(st.briefing || { spoken: st.briefing?.spoken || "No briefing yet." });
  renderConfirmations(st.pending_confirmations || []);
  renderTasks(st.active_tasks || []);
  renderNotifs(st.notifications || []);
}
function renderBriefing(b) {
  const el = $("mBriefing");
  if (!b || !b.spoken) { el.innerHTML = `<div class="mEmpty">No briefing yet.</div>`; return; }
  const parts = String(b.spoken).split(". ").filter(Boolean);
  el.innerHTML = `<b>${esc(parts[0])}.</b>` + parts.slice(1).map((p) => `<div style="margin-top:6px">${esc(p)}</div>`).join("");
}
function renderConfirmations(list) {
  const sec = $("mConfirm"); const el = $("mConfirmList");
  el.innerHTML = "";
  sec.classList.toggle("hidden", !list.length);
  for (const c of list) {
    const d = document.createElement("div");
    d.className = "mItem";
    d.innerHTML = `<div class="t">🔔 ${esc(c.tool)}</div><div class="d">${esc(c.what || "")}</div>
      <div class="meta">${esc(c.action || "")}</div>
      <div class="btnRow"><button class="btnApprove" onclick="decide('${c.id}', true)">Approve</button>
      <button class="btnDeny" onclick="decide('${c.id}', false)">Deny</button></div>`;
    el.appendChild(d);
  }
}
window.decide = async (id, approve) => {
  try { await api(`/api/companion/confirmations/${id}/${approve ? "approve" : "deny"}`); refreshToday(); }
  catch (e) { toastHint(e.message); }
};
function renderTasks(list) {
  const el = $("mTaskList");
  el.innerHTML = list.length ? "" : `<div class="mEmpty">No active tasks.</div>`;
  for (const t of list) {
    const d = document.createElement("div");
    d.className = "mItem";
    d.innerHTML = `<div class="t">${esc(t.name)}</div><div class="meta"><span class="pill warn">${esc(t.status)}</span></div>`;
    el.appendChild(d);
  }
}
function renderNotifs(list) {
  const el = $("mNotifList");
  el.innerHTML = list.length ? "" : `<div class="mEmpty">No notifications.</div>`;
  for (const n of list) {
    const d = document.createElement("div");
    d.className = "mItem";
    d.innerHTML = `<div class="t">${esc(n.title)}</div><div class="d">${esc(n.body)}</div>`;
    el.appendChild(d);
  }
}
$("mRefresh").onclick = refreshToday;

/* ============================ CHAT ============================ */
function renderChat() {
  const list = $("chatList");
  list.innerHTML = "";
  if (!S.history.length) { list.innerHTML = `<div class="mEmpty">Say hi 👋 — type below or tap the mic.</div>`; return; }
  for (const m of S.history) {
    const div = document.createElement("div");
    div.className = "chatMsg " + (m.role === "user" ? "user" : "assistant");
    div.innerHTML = `<div class="bubble"><span class="who">${m.role === "user" ? "You" : (S.agent === "coded" ? "Coded" : "Phantom")}</span>${esc(m.text)}</div>`;
    list.appendChild(div);
  }
  list.scrollTop = list.scrollHeight;
}
function addChatMsg(role, text) {
  S.history.push({ role, text });
  renderChat();
}
function toastHint(t) { const h = $("mVoiceHint"); if (h) h.textContent = t; setTimeout(() => { if (h && h.textContent === t) h.textContent = ""; }, 4000); }

async function sendChat(text) {
  const clean = String(text || "").trim();
  if (!clean || !S.connected) { if (!S.connected) toastHint("Offline — reconnect in Settings"); return; }
  $("chatInput").value = "";
  addChatMsg("user", clean);
  if (S.mode === "portable") {
    try {
      const j = await api("/api/chat", { body: { text: clean } });
      addChatMsg("assistant", j.reply || "(no reply)");
      try { speakReply(j.reply); } catch (e) {}
    } catch (e) { addChatMsg("assistant", "⚠️ " + e.message); }
    return;
  }
  // pc mode: start a run + poll the conversation for the reply.
  // MULTI-TASK: each agent polls its own run — Phantom can be replying while
  // you send Coded a separate task.
  addChatMsg("assistant", "");
  const list = $("chatList");
  if (list.lastElementChild) list.lastElementChild.classList.add("typing");
  try {
    const cid = S.convByAgent[S.agent] || "";
    const started = await api(`/api/agents/${S.agent}/chat`, { body: { text: clean, conversation_id: cid, session_id: "mobile" } });
    S.convByAgent[S.agent] = started.conversation_id;
    await pollReply(S.agent, started.conversation_id, started.run_id);
  } catch (e) {
    replaceLastAssistant("⚠️ " + e.message);
  }
}
async function pollReply(agent, cid, runId) {
  if (S.pollingByAgent && S.pollingByAgent[agent]) return;
  S.pollingByAgent = S.pollingByAgent || {};
  S.pollingByAgent[agent] = true;
  const startedAt = Date.now();
  try {
    while (Date.now() - startedAt < 60000) {
      await sleep(1400);
      const conv = await api(`/api/conversations/${cid}`);
      const msgs = conv.messages || [];
      const newAsst = msgs.filter((m) => m.role === "assistant" && m.content && m.content.trim());
      const last = newAsst[newAsst.length - 1];
      if (last) {
        const reply = String(last.content || "").trim();
        if (agent === S.agent) {
          // replace the typing bubble (only when this agent is the visible one)
          const list = $("chatList");
          const bubbles = list.querySelectorAll(".chatMsg.assistant .bubble");
          const typing = bubbles[bubbles.length - 1];
          if (typing) {
            const who = typing.querySelector(".who");
            typing.parentElement.classList.remove("typing");
            typing.innerHTML = (who ? who.outerHTML : "") + esc(reply);
          } else {
            addChatMsg("assistant", reply);
          }
          S.history[S.history.length - 1] = { role: "assistant", text: reply };
        }
        try { speakReply(reply); } catch (e) {}
        S.pollingByAgent[agent] = false;
        return;
      }
      // stop if the run errored
      const run = await api(`/api/runs/${runId}`).catch(() => null);
      if (run && run.status === "error") {
        if (agent === S.agent) replaceLastAssistant("⚠️ " + (run.error || "run failed"));
        break;
      }
    }
  } catch (e) {
    if (agent === S.agent) replaceLastAssistant("⚠️ " + e.message);
  }
  S.pollingByAgent[agent] = false;
}
function replaceLastAssistant(text) {
  const list = $("chatList");
  const bubbles = list.querySelectorAll(".chatMsg.assistant .bubble");
  const typing = bubbles[bubbles.length - 1];
  if (typing) {
    const who = typing.querySelector(".who");
    typing.parentElement.classList.remove("typing");
    typing.innerHTML = (who ? who.outerHTML : "") + esc(text);
  } else addChatMsg("assistant", text);
  S.history[S.history.length - 1] = { role: "assistant", text };
  renderChat();
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
$("chatSend").onclick = () => sendChat($("chatInput").value);
$("chatInput").addEventListener("keydown", (ev) => { if (ev.key === "Enter") sendChat($("chatInput").value); });

/* agent switch resets the visible chat (new conversation per agent) */
document.querySelectorAll(".mAgent").forEach((b) => b.onclick = () => {
  S.agent = b.dataset.a;
  document.querySelectorAll(".mAgent").forEach((x) => x.classList.toggle("active", x === b));
  S.history = [];
  renderChat();
});

/* ============================ VOICE ============================ */
async function startRec() {
  if (S.recording) return;
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const rec = new MediaRecorder(stream);
    S.mediaRec = rec; S.chunks = [];
    rec.ondataavailable = (ev) => S.chunks.push(ev.data);
    rec.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop());
      const blob = new Blob(S.chunks, { type: "audio/webm" });
      await sendVoice(blob);
    };
    rec.start(); S.recording = true;
    $("mVoiceBtn").classList.add("listening");
    toastHint("Listening… tap again to send");
  } catch (e) { toastHint("Mic unavailable: " + e.message); }
}
function stopRec() {
  if (!S.recording) return;
  S.recording = false;
  $("mVoiceBtn").classList.remove("listening");
  $("mVoiceBtn").classList.add("sending");
  toastHint("Sending to " + (S.agent === "coded" ? "Coded" : "Phantom") + "…");
  S.mediaRec.stop();
}
async function sendVoice(blob) {
  try {
    const buf = await blob.arrayBuffer();
    const isPortable = S.mode === "portable";
    if (isPortable) {
      const res = await api("/api/voice/stt", { method: "POST", raw: true, headers: { "Content-Type": "application/octet-stream" }, body: buf });
      const j = await res.json();
      $("mVoiceBtn").classList.remove("sending");
      if (!res.ok) { toastHint("✗ " + (j.error || "STT failed")); return; }
      const text = j.transcript || "";
      if (!text) { toastHint("no speech detected"); return; }
      toastHint("“" + text + "”");
      await sendChat(text);
      return;
    }
    // pc mode: companion voice (PC transcribes + runs agent)
    const res = await fetch(S.base + `/api/companion/voice?agent=${S.agent}`, {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream", ...(S.token ? { "X-Access-Token": S.token } : {}) },
      body: buf,
    });
    const j = await res.json();
    $("mVoiceBtn").classList.remove("sending");
    if (!res.ok) { toastHint("✗ " + (j.detail || j.error || "error")); return; }
    toastHint("“" + j.text + "”");
    addChatMsg("user", j.text);
    addChatMsg("assistant", j.reply || "(no reply)");
    try { speakReply(j.reply); } catch (e) {}
    setTimeout(() => toastHint(""), 6000);
  } catch (e) {
    $("mVoiceBtn").classList.remove("sending");
    toastHint("✗ " + e.message);
  }
}
// JARVIS on the phone too: speechSynthesis needs a prior user gesture on
// iOS — unlock on first tap so replies always speak.
let _mUnlocked = false;
function unlockPhoneAudio() {
  if (_mUnlocked) return;
  _mUnlocked = true;
  try { speechSynthesis.cancel(); speechSynthesis.getVoices(); } catch (e) {}
}
["pointerdown", "touchstart", "keydown"].forEach((ev) =>
  window.addEventListener(ev, unlockPhoneAudio, { passive: true }));
function speakReply(txt) {
  try {
    unlockPhoneAudio();
    speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(String(txt || "").replace(/[#*`_>]/g, "").slice(0, 600));
    speechSynthesis.speak(u);
  } catch (e) {}
}
$("mVoiceBtn").onclick = () => S.recording ? stopRec() : startRec();

/* ============================ WS (pc mode live) ============================ */
function connectWS() {
  if (!S.base) return;
  closeWS();
  const proto = S.base.startsWith("https") ? "wss" : "ws";
  const url = proto + "://" + S.base.replace(/^https?:\/\//, "") + "/ws" + (S.token ? "?token=" + encodeURIComponent(S.token) : "");
  let ws;
  try { ws = new WebSocket(url); } catch (e) { return; }
  S.ws = ws;
  ws.onmessage = (ev) => {
    try {
      const e = JSON.parse(ev.data);
      if (e.event === "presence.state") { const p = e.data; if (p) setStatus(true, p.state === "listening" ? "🟢 awake" : "💤 sleeping"); }
      if (e.event === "notification.new" || e.event === "confirmation.requested" || e.event === "task.update") refreshToday();
    } catch (e2) {}
  };
  ws.onopen = () => { S.wsRetries = 0; setStatus(true, S.connLabel || "connected"); };
  ws.onclose = () => { S.wsRetries += 1; if (S.mode !== "portable" && S.wsRetries < 6) setTimeout(connectWS, Math.min(1500 * Math.pow(2, S.wsRetries - 1), 15000)); };
  ws.onerror = () => {};
}
function closeWS() { if (S.ws) { try { S.ws.onclose = null; S.ws.close(); } catch (e) {} S.ws = null; } }

/* ============================ SETTINGS ============================ */
async function connect() {
  S.base = $("mUrl").value.trim().replace(/\/+$/, "");
  S.token = $("mToken").value.trim();
  S.cloudBase = $("mCloud").value.trim().replace(/\/+$/, "");
  S.cloudToken = $("mCloudToken").value.trim();
  localStorage.setItem("phai.companion.url", S.base);
  localStorage.setItem("phai.companion.token", S.token);
  localStorage.setItem("phai.companion.cloud", S.cloudBase);
  localStorage.setItem("phai.companion.cloudtoken", S.cloudToken);
  const err = $("mConnErr");
  if (!S.base && !S.cloudBase) { err.textContent = "Enter your PC and/or portable URL"; err.classList.remove("hidden"); return; }
  err.classList.add("hidden");
  toastHint("connecting…");
  switchTab("chat");
  await detectAndConnect();
}
$("mConnectBtn").onclick = connect;

$("mCloudKeysBtn").onclick = async () => {
  const nv = $("mCloudNvidia").value.trim();
  const dg = $("mCloudDeepgram").value.trim();
  const gq = $("mCloudGroq").value.trim();
  const model = $("mCloudModel").value.trim();
  const body = {};
  if (nv) body.nvidia_key = nv;
  if (dg) body.deepgram_key = dg;
  if (gq) body.groq_key = gq;
  if (model) body.model = model;
  if (!Object.keys(body).length) { $("mCloudLog").textContent = "Paste a key or model first."; return; }
  try {
    // portable mode: talk to the worker directly; pc mode: via the PC
    const r = S.mode === "portable" && S.cloudBase
      ? await (async () => {
          const res = await fetch(S.cloudBase + "/api/config/keys", {
            method: "POST", headers: { "Content-Type": "application/json", ...(S.cloudToken ? { "X-Access-Token": S.cloudToken } : {}) },
            body: JSON.stringify(body),
          });
          const j = await res.json();
          if (!res.ok) throw new Error(j.error || "cloud rejected");
          return { masked: j.masked };
        })()
      : await api("/api/cloud/keys", { body });
    $("mCloudNvidia").value = ""; $("mCloudDeepgram").value = ""; $("mCloudGroq").value = "";
    $("mCloudLog").textContent = "Saved (masked): " + JSON.stringify(r.masked || {});
    toastHint("Cloud config saved");
  } catch (e) { $("mCloudLog").textContent = "✗ " + e.message; }
};
$("mRedeployBtn").onclick = async () => {
  const log = $("mCloudLog");
  log.textContent = "Redeploying via PC…";
  try { const r = await api("/api/cloud/deploy", { body: {} }); log.textContent = "Redeploy started on your PC."; }
  catch (e) { log.textContent = "✗ " + e.message; }
};
function cloudLogLine(line) { const log = $("mCloudLog"); if (log) log.textContent += "\n" + line; }
function killPC() {
  if (!confirm("Engage the PC kill switch? Stops voice, mic, tasks, all agents.")) return;
  (async () => { try { await api("/api/killswitch/engage", { body: "{}", headers: { "Content-Type": "application/json" } }); alert("Kill switch engaged on PC"); } catch (e) { alert(e.message); } })();
}
$("mKill").onclick = killPC;
$("mKill2").onclick = killPC;

/* ============================ BOOT ============================ */
(function boot() {
  // PWA service worker (secure context only)
  if ("serviceWorker" in navigator && (location.protocol === "https:" || location.hostname === "localhost")) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }
  S.mode = localStorage.getItem("phai.companion.mode") || "auto";
  applyMode();
  if (S.base) $("mUrl").value = S.base;
  if (S.token) $("mToken").value = S.token;
  if (S.cloudBase) $("mCloud").value = S.cloudBase;
  if (S.cloudToken) $("mCloudToken").value = S.cloudToken;
  renderChat();
  if (S.base || S.cloudBase) {
    detectAndConnect();
    S.healthTimer = setInterval(checkHealth, 15000);
  } else {
    setStatus(false);
    switchTab("settings");
  }
})();
