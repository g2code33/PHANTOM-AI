/**
 * Headless smoke test: load the REAL index.html + voice.js + hud.js + app.js
 * in jsdom and catch runtime errors (module-scope crashes that would blank
 * the app). Stubs browser-only APIs; fetch returns canned JSON.
 */
const fs = require("fs");
const path = require("path");
const { JSDOM, VirtualConsole } = require("jsdom");

const ROOT = require("path").join(__dirname, "..", "ui");
const html = fs.readFileSync(path.join(ROOT, "index.html"), "utf8");
const voiceJs = fs.readFileSync(path.join(ROOT, "voice.js"), "utf8");
const hudJs = fs.readFileSync(path.join(ROOT, "hud.js"), "utf8");
const consoleJs = fs.readFileSync(path.join(ROOT, "console.js"), "utf8");
const appJs = fs.readFileSync(path.join(ROOT, "app.js"), "utf8");

const errors = [];
const virtualConsole = new VirtualConsole();
virtualConsole.on("jsdomError", (e) => errors.push("jsdomError: " + (e.detail || e.message || e)));
virtualConsole.on("error", (...a) => errors.push("console.error: " + a.join(" ")));

const dom = new JSDOM(html, {
  url: "http://127.0.0.1:47611/",
  runScripts: "outside-only",
  pretendToBeVisual: true,
  virtualConsole,
});
const { window } = dom;

// ---- browser API stubs ----
window.requestAnimationFrame = (cb) => setTimeout(() => cb(Date.now()), 16);
window.cancelAnimationFrame = (id) => clearTimeout(id);
window.AudioContext = class { constructor(){ this.sampleRate=48000; this.state="running"; this.destination={}; } createAnalyser(){ return { fftSize:512, frequencyBinCount:256, smoothingTimeConstant:0.8, getByteFrequencyData(a){ for(let i=0;i<a.length;i++) a[i]=64; } }; } createMediaStreamSource(){ return { connect(){} }; } createMediaElementSource(){ return { connect(){} }; } createBufferSource(){ return { connect(){}, start(){}, buffer:null }; } decodeAudioData(){ return Promise.resolve({}); } resume(){ return Promise.resolve(); } close(){ return Promise.resolve(); } createGain(){ return { connect(){}, gain:{ value:0 } }; } createOscillator(){ return { connect(){ return { connect(){} }; }, start(){}, stop(){}, type:"sine", frequency:{ setValueAtTime(){}, exponentialRampToValueAtTime(){} } }; } createScriptProcessor(){ return { connect(){}, disconnect(){} }; } }
window.webkitAudioContext = window.AudioContext;
window.speechSynthesis = { getVoices: () => [], cancel(){}, speak(){} };
window.SpeechRecognition = undefined;
window.webkitSpeechRecognition = undefined;
Object.defineProperty(window.navigator, "mediaDevices", { value: { getUserMedia: () => Promise.reject(new Error("no mic in test")) } });
window.Audio = class { constructor(){ this.volume=1; } play(){ return Promise.resolve(); } pause(){} addEventListener(){} };

// ---- fetch stub: canned API responses ----
const canned = {
  "/api/settings": { settings: { "*": { "ui.onboarded": true, "ui.theme": "midnight" } }, keys: {}, voice: {} },
  "/api/status": { version: "0.4.9", agents: [], providers: {} },
  "/api/hud": { cpu: { per_core: [12, 45, 8], total: 22, cores: 3 }, memory: { percent: 61, used_bytes: 6e9, total_bytes: 16e9 }, disk: { read_bps: 0, write_bps: 0 }, net: { down_bps: 0, up_bps: 0 }, battery: { available: false, reason: "no battery" }, process: { name: "phantom-backend", pid: 1, cpu_percent: 3.2 }, process_count: 212 },
  "/api/hud/weather": { available: false, reason: "offline" },
  "/api/presence": { state: "listening", active_agent: "phantom" },
  "/api/voice/config": { mode: "conversation", stt: { provider: "server" }, tts: { provider: "server" }, voices: {}, deepgram_configured: false, groq_configured: false, stt_priority: [], tts_priority: [], vad_threshold: 0.03, auto_stop_ms: 900, max_record_ms: 15000, continuous: true },
  "/api/cloud/config": { url: "", token_configured: false, url_masked: "" },
  "/api/notifications": { notifications: [] },
  "/api/briefing/config": { time: "07:30" },
  "/api/brains": { brains: [] },
  "/api/graph": {},
  "/api/proposals": [],
  "/api/snapshots": [],
  "/api/audit/events": { events: [] },
  "/api/permissions": [],
};
window.fetch = async (url) => {
  const u = String(url).split("?")[0];
  const body = canned[u];
  if (body !== undefined) return { ok: true, status: 200, json: async () => body };
  // unknown GET → benign empty (keeps init chain alive)
  return { ok: true, status: 200, json: async () => ({}) };
};
// catch unhandled rejections so they don't kill the harness
window.addEventListener("unhandledrejection", (e) => errors.push("unhandledrejection: " + (e && e.reason && e.reason.message || e.reason || e)));
window.WebSocket = class { constructor(){ this.readyState = 3; } close(){} send(){} addEventListener(){} };

// ---- run the real scripts ----
try {
  window.eval(voiceJs);
  window.eval(hudJs);
  window.eval(consoleJs);
  window.eval(appJs);
} catch (e) {
  errors.push("eval threw: " + (e && e.stack || e));
}

// let module-scope init + DOMContentLoaded run
setTimeout(() => {
  // verify the critical chat helpers exist in the app source (the bug that
  // broke chat: agentRun etc. were referenced but never defined)
  for (const fn of ["agentRun", "setAgentRunning", "setAgentDone", "anyRunning", "startReplyWatcher"]) {
    if (!new RegExp("function " + fn + "\\(").test(appJs)) {
      problems.push("MISSING FUNCTION: " + fn);
    }
  }
  const problems = errors.filter((e) => !/favicon|404/.test(e));
  console.log("jsdom errors:", errors.length ? errors.slice(0, 10) : "none");
  const body = window.document.body;
  const panelIds = ["sessionBadge", "clockTime", "cpuHist", "memHist", "hudCpuBars", "hudDiskVal",
                    "weatherBody", "moonBody", "netHist", "sysBody", "specStrip", "hudFooter"];
  const missing = panelIds.filter((id) => !window.document.getElementById(id));
  console.log("missing panels:", missing.length ? missing : "none");
  console.log("sysBody rendered:", (window.document.getElementById("sysBody") || {}).innerHTML || "(empty)");
  console.log("moon rendered:", (window.document.getElementById("moonBody") || {}).innerHTML || "(empty)");
  console.log("onboarding visible:", !window.document.getElementById("onboarding").classList.contains("hidden"));
  process.exit(problems.length ? 1 : 0);
}, 600);
