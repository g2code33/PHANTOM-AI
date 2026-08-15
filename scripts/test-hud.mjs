/**
 * Node-side verification of the HUD's pure helpers (no DOM needed).
 * Run:  node scripts/test-hud.mjs
 */
import assert from "node:assert";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { hudTier, avgBins, formatRate } = require("../ui/hud.js");

// ---- rendering tiers: sleeping is the single low-power trigger ----
assert.strictEqual(hudTier("idle", true), "low", "sleeping → low tier");
assert.strictEqual(hudTier("listening", true), "low", "silenced+sleeping → low tier");
assert.strictEqual(hudTier("idle", false), "full", "awake idle → full tier");
assert.strictEqual(hudTier("listening", false), "full", "listening → full tier");
assert.strictEqual(hudTier("speaking", false), "full", "speaking → full tier");
assert.strictEqual(hudTier("thinking", false), "full", "thinking → full tier");

// ---- spectrum binning is a pure reduction of REAL data ----
assert.deepStrictEqual(avgBins([], 48), [], "empty data → empty (no fake bars)");
assert.deepStrictEqual(avgBins(null, 48), [], "null data → empty");
const freq = new Array(128).fill(128); // flat real spectrum
const bars = avgBins(freq, 48);
assert.strictEqual(bars.length, 48, "128 bins → 48 bars");
for (const b of bars) assert.strictEqual(b, 128, "flat input stays flat");
const peak = avgBins(new Array(128).fill(0).map((_, i) => (i === 64 ? 255 : 0)), 48);
assert.ok(Math.max(...peak) > 0 && Math.max(...peak) <= 255, "peak survives binning");
assert.strictEqual(peak.length, 48);

// ---- rate formatting ----
assert.strictEqual(formatRate(0), "0 B/s");
assert.strictEqual(formatRate(500), "500 B/s");
assert.strictEqual(formatRate(2048), "2 KB/s");
assert.strictEqual(formatRate(1536 * 1024), "1.5 MB/s");
assert.strictEqual(formatRate("nope"), "0 B/s", "non-numeric → honest 0");

console.log("HUD pure-helper checks passed (tier contract + real-data shaping).");
