/**
 * Local verification of the portable Worker using a tiny KV shim + Node's
 * fetch, without deploying to Cloudflare. Run:
 *   node cloud/tests/test_worker.mjs
 *
 * The Worker's `handle(request, env)` is pure and env-injectable, so we can
 * drive it directly. NVIDIA calls are replaced by a stub fetch (the Worker's
 * global fetch is monkeypatched for the chat test).
 */

import assert from "node:assert";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const workerSrc = join(__dirname, "..", "worker", "src", "index.js");

// ---- KV shim ----
function makeKV() {
  const m = new Map();
  return {
    async get(k) { const v = m.get(k); return v === undefined ? null : JSON.parse(v); },
    async put(k, v) { m.set(k, JSON.stringify(v)); },
    async delete(k) { m.delete(k); },
    _raw: m,
  };
}

// ---- load worker ----
const worker = await import(workerSrc);
const handle = worker.default.fetch;

function makeEnv() {
  return {
    PHANTOM_MEMORY: makeKV(),
    PHANTOM_PROFILE: makeKV(),
    PHANTOM_REMINDERS: makeKV(),
    PHANTOM_KEYS: makeKV(),
    NVIDIA_API_KEY: "nvapi-test",
    DEEPGRAM_API_KEY: "dg-test",
    PHANTOM_CLOUD_TOKEN: "",
    NVIDIA_MODEL: "nvidia/llama-3.3-70b-instruct",
  };
}

function req(url, { method = "GET", body, headers = {} } = {}) {
  const init = { method, headers: { ...headers } };
  if (body !== undefined) {
    init.body = typeof body === "string" ? body : JSON.stringify(body);
    init.headers["Content-Type"] = "application/json";
  }
  return new Request(url, init);
}

// ---- tests ----
const env = makeEnv();
let passed = 0;

async function t(name, fn) {
  try { await fn(); passed++; console.log("  ✓", name); }
  catch (e) { console.error("  ✗", name, "\n   ", e.message); process.exitCode = 1; }
}

// status
await t("status reports portable mode", async () => {
  const r = await handle(req("https://phantom.local/api/status"), env);
  const j = await r.json();
  assert.equal(j.mode, "portable");
  assert.equal(j.cloud, true);
  assert.equal(j.has_nvidia, true);
});

// auth: token required for DATA endpoints, but /api/status + / stay public
// (status only reports masked presence, so the phone can test connection)
await t("auth enforced when token set (data endpoints)", async () => {
  const e2 = makeEnv(); e2.PHANTOM_CLOUD_TOKEN = "secret123";
  const r = await handle(req("https://phantom.local/api/memory"), e2);
  assert.equal(r.status, 401);
  const ok = await handle(req("https://phantom.local/api/memory",
    { headers: { "X-Access-Token": "secret123" } }), e2);
  assert.equal(ok.status, 200);
});
await t("status public for health checks", async () => {
  const e2 = makeEnv(); e2.PHANTOM_CLOUD_TOKEN = "secret123";
  const r = await handle(req("https://phantom.local/api/status"), e2);
  assert.equal(r.status, 200);
  const j = await r.json();
  assert.equal(j.mode, "portable");
});

// cloud memory (the "save to cloud" store)
await t("cloud memory save + list", async () => {
  await handle(req("https://phantom.local/api/memory",
    { method: "POST", body: { content: "JOOJO prefers evenings for study", kind: "preference" } }), env);
  const r = await handle(req("https://phantom.local/api/memory"), env);
  const j = await r.json();
  assert.equal(j.memories.length, 1);
  assert.equal(j.memories[0].cloud, true);
  assert.match(j.memories[0].content, /evenings/);
});

// profile
await t("shared profile save + read", async () => {
  await handle(req("https://phantom.local/api/profile",
    { method: "PUT", body: { display_name: "JOOJO", program: "PharmD" } }), env);
  const r = await handle(req("https://phantom.local/api/profile"), env);
  const j = await r.json();
  assert.equal(j.profile.display_name, "JOOJO");
});

// reminders
await t("reminders set + list", async () => {
  await handle(req("https://phantom.local/api/reminders",
    { method: "POST", body: { text: "Call mom", when: "tomorrow 10:00" } }), env);
  const r = await handle(req("https://phantom.local/api/reminders"), env);
  const j = await r.json();
  assert.equal(j.reminders.length, 1);
  assert.match(j.reminders[0].text, /Call mom/);
});

// briefing
await t("briefing uses profile + reminders", async () => {
  const r = await handle(req("https://phantom.local/api/briefing"), env);
  const j = await r.json();
  assert.match(j.spoken, /JOOJO/);
  assert.match(j.spoken, /Call mom/);
});

// chat with stubbed NVIDIA fetch (tool loop: save_memory)
await t("chat runs cloud tool loop (save to cloud)", async () => {
  const realFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, init) => {
    if (String(url).includes("integrate.api.nvidia.com")) {
      const body = JSON.parse(init.body);
      calls.push(body);
      const last = body.messages[body.messages.length - 1];
      if (last && last.role === "tool") {
        return new Response(JSON.stringify({ choices: [{ message: { content: "Saved it to the cloud." } }] }));
      }
      return new Response(JSON.stringify({ choices: [{ message: {
        content: "",
        tool_calls: [{ id: "c1", type: "function",
                       function: { name: "save_memory", arguments: JSON.stringify({ content: "Remembers: prefers dark mode", kind: "preference" }) } }],
      } }] }));
    }
    return realFetch(url, init);
  };
  try {
    const r = await handle(req("https://phantom.local/api/chat",
      { method: "POST", body: { text: "remember I prefer dark mode" } }), env);
    const j = await r.json();
    assert.match(j.reply, /Saved it to the cloud/);
    assert.equal(j.mode, "portable");
    assert.ok(calls.length >= 2, "expected a tool loop (>=2 NVIDIA calls)");
    // memory persisted to cloud KV
    const mem = await env.PHANTOM_MEMORY.get("memories");
    assert.ok(JSON.stringify(mem).includes("dark mode"));
  } finally {
    globalThis.fetch = realFetch;
  }
});

// voice STT with a stubbed Deepgram response
await t("voice stt transcribes via Deepgram", async () => {
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    if (String(url).includes("api.deepgram.com")) {
      return new Response(JSON.stringify({ results: { channels: [{ alternatives: [{ transcript: "hello from portable phantom" }] }] } }),
        { status: 200, headers: { "Content-Type": "application/json" } });
    }
    return realFetch(url, init);
  };
  try {
    const r = await handle(req("https://phantom.local/api/voice/stt",
      { method: "POST", body: "fake-wav-bytes" }), env);
    const j = await r.json();
    assert.match(j.transcript, /hello from portable phantom/);
  } finally { globalThis.fetch = realFetch; }
});

console.log(`\n${passed} portable-worker checks passed.`);


// config/keys: set keys from the app, masked status
await t("config/keys sets + masks cloud keys", async () => {
  const e = makeEnv();
  const r = await handle(req("https://phantom.local/api/config/keys",
    { method: "POST", body: { nvidia_key: "nvapi-cloud-123", deepgram_key: "dg-cloud-456" } }), e);
  const j = await r.json();
  assert.equal(j.ok, true);
  assert.deepEqual(j.masked, { nvidia: "configured", deepgram: "configured",
                               model: "nvidia/llama-3.3-70b-instruct" });
  const st = await handle(req("https://phantom.local/api/status"), e);
  const sj = await st.json();
  assert.equal(sj.keys.nvidia, "configured");
  assert.equal(sj.keys.deepgram, "configured");
  // clear nvidia
  const c = await handle(req("https://phantom.local/api/config/keys",
    { method: "POST", body: { clear_nvidia: true } }), e);
  const cj = await c.json();
  assert.equal(cj.masked.nvidia, "not set");
  assert.equal(cj.masked.deepgram, "configured");
});

await t("config/keys auth enforced when token set", async () => {
  const e = makeEnv(); e.PHANTOM_CLOUD_TOKEN = "secret123";
  const r = await handle(req("https://phantom.local/api/config/keys",
    { method: "POST", body: { nvidia_key: "x" } }), e);
  assert.equal(r.status, 401);
});

await t("chat uses app-stored nvidia key", async () => {
  const e = makeEnv();
  await handle(req("https://phantom.local/api/config/keys",
    { method: "POST", body: { nvidia_key: "nvapi-from-app" } }), e);
  let used = "";
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    if (String(url).includes("integrate.api.nvidia.com")) {
      used = (init.headers || {}).Authorization || "";
      return new Response(JSON.stringify({ choices: [{ message: { content: "hi" } }] }));
    }
    return realFetch(url, init);
  };
  try {
    await handle(req("https://phantom.local/api/chat", { method: "POST", body: { text: "hi" } }), e);
    assert.match(used, /nvapi-from-app/);
  } finally { globalThis.fetch = realFetch; }
});

// cloud model override (set from the app) + the fixed-link mobile UI
await t("cloud model override + mobile UI served at root", async () => {
  const e = makeEnv();
  const set = await handle(req("https://phantom.local/api/config/keys",
    { method: "POST", body: { model: "nvidia/llama-3.1-8b-instruct" } }), e);
  const sj = await set.json();
  assert.equal(sj.masked.model, "nvidia/llama-3.1-8b-instruct");
  const st = await handle(req("https://phantom.local/api/status"), e);
  assert.equal((await st.json()).model, "nvidia/llama-3.1-8b-instruct");
  // chat must use the overridden model
  const realFetch = globalThis.fetch;
  let usedModel = "";
  globalThis.fetch = async (url, init) => {
    if (String(url).includes("integrate.api.nvidia.com")) {
      usedModel = JSON.parse(init.body).model;
      return new Response(JSON.stringify({ choices: [{ message: { content: "hi" } }] }));
    }
    return realFetch(url, init);
  };
  const chat = await handle(req("https://phantom.local/api/chat",
    { method: "POST", body: { text: "hello" } }), e);
  assert.equal(chat.status, 200);
  assert.equal(usedModel, "nvidia/llama-3.1-8b-instruct");
  globalThis.fetch = realFetch;
  // /mobile serves the embedded companion UI (html)
  const mob = await handle(req("https://phantom.local/mobile"), e);
  assert.equal(mob.status, 200);
  const html = await mob.text();
  assert.match(html, /<!doctype html/i);
  assert.match(html, /Connect to your PC/);
});
