/* Phantom Companion — the APK is a real remote for the PC. */
"use strict";

const S = {
  base: localStorage.getItem("phai.companion.url") || "",
  token: localStorage.getItem("phai.companion.token") || "",
  agent: "phantom",
  ws: null,
  connected: false,
  recording: false,
  mediaRec: null,
  chunks: [],
};

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (S.token) headers["X-Access-Token"] = S.token;
  const res = await fetch(S.base + path, { headers, method: opts.method || (opts.body ? "POST" : "GET"), body: opts.body });
  if (!res.ok) {
    let d = res.statusText;
    try { const j = await res.json(); d = j.detail || j.error || d; } catch (e) {}
    throw new Error(d || `HTTP ${res.status}`);
  }
  return res.json();
}

/* ---------- connect ---------- */
function showConnect() { $("mConnect").classList.remove("hidden"); $("mMain").classList.add("hidden"); $("mVoiceBar").classList.add("hidden"); }
function showMain() { $("mConnect").classList.add("hidden"); $("mMain").classList.remove("hidden"); $("mVoiceBar").classList.remove("hidden"); }

async function connect() {
  S.base = $("mUrl").value.trim().replace(/\/+$/, "");
  S.token = $("mToken").value.trim();
  if (!S.base) { $("mConnErr").textContent = "Enter your PC's address"; $("mConnErr").classList.remove("hidden"); return; }
  localStorage.setItem("phai.companion.url", S.base);
  localStorage.setItem("phai.companion.token", S.token);
  try {
    const st = await api("/api/companion/status");
    showMain();
    $("mConnErr").classList.add("hidden");
    connectWS();
    refreshAll();
  } catch (e) {
    $("mConnErr").textContent = "Can't reach your PC: " + e.message;
    $("mConnErr").classList.remove("hidden");
  }
}
$("mConnectBtn").onclick = connect;

/* ---------- WS ---------- */
function connectWS() {
  const proto = S.base.startsWith("https") ? "wss" : "ws";
  const url = proto + "://" + S.base.replace(/^https?:\/\//, "") + "/ws" + (S.token ? "?token=" + encodeURIComponent(S.token) : "");
  S.ws = new WebSocket(url);
  S.ws.onmessage = (ev) => { try { handle(JSON.parse(ev.data)); } catch (e) {} };
  S.ws.onclose = () => { setStatus("disconnected"); setTimeout(connectWS, 3000); };
  S.ws.onopen = () => { setStatus("connected"); };
}
function handle(ev) {
  if (ev.event === "presence.state") applyPresence(ev.data);
  if (ev.event === "notification.new") refreshAll();
  if (ev.event === "confirmation.requested" || ev.event === "confirmation.decided") refreshAll();
  if (ev.event === "task.update") refreshAll();
  if (ev.event === "voice.speak" && document.hidden === false) {
    // phone speaks proactive messages too (companion TTS)
    try { speechSynthesis.cancel(); speechSynthesis.speak(new SpeechSynthesisUtterance(String(ev.data?.text || "").replace(/[#*`_>]/g, "").slice(0, 600))); } catch (e) {}
  }
}
function setStatus(s) {
  S.connected = s === "connected";
  const p = $("mPresence");
  p.textContent = s === "connected" ? "connected" : "offline";
  p.className = "mPill " + (s === "connected" ? "on" : "warn");
}
function applyPresence(p) {
  const el = $("mPresence");
  if (p.state === "listening") { el.textContent = (p.active_agent === "coded" ? "💻 Coded" : "👻 Phantom") + " awake"; el.className = "mPill on"; }
  else if (p.state === "silenced") { el.textContent = "🤫 silent"; el.className = "mPill warn"; }
  else { el.textContent = "💤 sleeping"; el.className = "mPill"; }
}

/* ---------- refresh ---------- */
async function refreshAll() {
  try {
    const st = await api("/api/companion/status");
    applyPresence(st.presence);
    renderBriefing(st.briefing);
    renderConfirmations(st.pending_confirmations);
    renderTasks(st.active_tasks);
    renderNotifs(st.notifications);
  } catch (e) { setStatus("disconnected"); }
}
function renderBriefing(b) {
  const el = $("mBriefing");
  if (!b || !b.spoken) { el.innerHTML = `<div class="mEmpty">No briefing yet.</div>`; return; }
  let html = `<b>${esc(b.spoken.split(".")[0])}.</b>`;
  const parts = b.spoken.split(". ").slice(1);
  html += parts.map((p) => `<div style="margin-top:6px">${esc(p)}</div>`).join("");
  el.innerHTML = html;
}
function renderConfirmations(list) {
  const sec = $("mConfirm");
  const el = $("mConfirmList");
  el.innerHTML = "";
  const items = list || [];
  sec.classList.toggle("hidden", !items.length);
  for (const c of items) {
    const d = document.createElement("div");
    d.className = "mItem";
    d.innerHTML = `<div class="t">🔔 ${esc(c.tool)}</div>
      <div class="d">${esc(c.what || "")}</div>
      <div class="meta">${esc(c.action)}</div>
      <div class="btnRow">
        <button class="btnApprove" onclick="decide('${c.id}', true)">Approve</button>
        <button class="btnDeny" onclick="decide('${c.id}', false)">Deny</button>
      </div>`;
    el.appendChild(d);
  }
}
window.decide = async (id, approve) => {
  try { await api(`/api/companion/confirmations/${id}/${approve ? "approve" : "deny"}`); refreshAll(); }
  catch (e) { alert(e.message); }
};
function renderTasks(list) {
  const el = $("mTaskList");
  el.innerHTML = "";
  const items = list || [];
  if (!items.length) { el.innerHTML = `<div class="mEmpty">No active tasks.</div>`; return; }
  for (const t of items) {
    const d = document.createElement("div");
    d.className = "mItem";
    d.innerHTML = `<div class="t">${esc(t.name)}</div><div class="meta"><span class="pill warn">${esc(t.status)}</span></div>`;
    el.appendChild(d);
  }
}
function renderNotifs(list) {
  const el = $("mNotifList");
  el.innerHTML = "";
  const items = list || [];
  if (!items.length) { el.innerHTML = `<div class="mEmpty">No notifications.</div>`; return; }
  for (const n of items) {
    const d = document.createElement("div");
    d.className = "mItem";
    d.innerHTML = `<div class="t">${esc(n.title)}</div><div class="d">${esc(n.body)}</div>`;
    el.appendChild(d);
  }
}
$("mRefresh").onclick = refreshAll;

/* ---------- voice (tap to talk → PC transcribes → agent runs) ---------- */
async function startRec() {
  if (S.recording) return;
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const rec = new MediaRecorder(stream);
    S.mediaRec = rec;
    S.chunks = [];
    rec.ondataavailable = (ev) => S.chunks.push(ev.data);
    rec.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop());
      const blob = new Blob(S.chunks, { type: "audio/webm" });
      await sendVoice(blob);
    };
    rec.start();
    S.recording = true;
    $("mVoiceBtn").classList.add("listening");
    $("mVoiceHint").textContent = "Listening… tap again to send";
  } catch (e) { $("mVoiceHint").textContent = "Mic unavailable: " + e.message; }
}
async function stopRec() {
  if (!S.recording) return;
  S.recording = false;
  $("mVoiceBtn").classList.remove("listening");
  $("mVoiceBtn").classList.add("sending");
  $("mVoiceHint").textContent = "Sending to " + (S.agent === "coded" ? "Coded" : "Phantom") + "…";
  S.mediaRec.stop();
}
async function sendVoice(blob) {
  try {
    const buf = await blob.arrayBuffer();
    const res = await fetch(S.base + `/api/companion/voice?agent=${S.agent}`, {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream", ...(S.token ? { "X-Access-Token": S.token } : {}) },
      body: buf,
    });
    const j = await res.json();
    $("mVoiceBtn").classList.remove("sending");
    if (!res.ok) { $("mVoiceHint").textContent = "✗ " + (j.detail || j.error || "error"); return; }
    $("mVoiceHint").textContent = "“" + j.text + "”";
    try { speechSynthesis.cancel(); speechSynthesis.speak(new SpeechSynthesisUtterance(String(j.reply || "").replace(/[#*`_>]/g, "").slice(0, 600))); } catch (e) {}
    setTimeout(() => { $("mVoiceHint").textContent = ""; }, 6000);
  } catch (e) {
    $("mVoiceBtn").classList.remove("sending");
    $("mVoiceHint").textContent = "✗ " + e.message;
  }
}
$("mVoiceBtn").onclick = () => S.recording ? stopRec() : startRec();

/* agent switch */
document.querySelectorAll(".mAgent").forEach((b) => b.onclick = () => {
  S.agent = b.dataset.a;
  document.querySelectorAll(".mAgent").forEach((x) => x.classList.toggle("active", x === b));
});

/* kill switch */
$("mKill").onclick = async () => {
  if (!confirm("Engage the PC kill switch? Stops voice, mic, tasks, all agents.")) return;
  try { await api("/api/killswitch/engage", { body: "{}", headers: { "Content-Type": "application/json" } }); alert("Kill switch engaged on PC"); } catch (e) { alert(e.message); }
};

/* boot */
(function boot() {
  // PWA service worker (secure context only: https or localhost)
  if ("serviceWorker" in navigator && (location.protocol === "https:" || location.hostname === "localhost")) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }
  if (S.base) { $("mUrl").value = S.base; $("mToken").value = S.token; connect(); }
  else showConnect();
})();
