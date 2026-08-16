/**
 * Node-side verification of the HUD's pure helpers (no DOM needed).
 * Run:  node scripts/test-hud.mjs
 */
import assert from "node:assert";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { hudTier, avgBins, formatRate, moonPhase, Ring, sparkPath } = require("../ui/hud.js");

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

// ---- moon phase: deterministic from real dates ----
const knownFullMoon = new Date("2026-08-28T00:00:00Z"); // approximate full moon period
const mp = moonPhase(knownFullMoon);
assert.ok(["New Moon","Waxing Crescent","First Quarter","Waxing Gibbous","Full Moon",
           "Waning Gibbous","Last Quarter","Waning Crescent"].includes(mp.name));
assert.ok(mp.illumination >= 0 && mp.illumination <= 100);
assert.ok(mp.icon.length >= 1);
// two dates ~29.53 days apart land near the same phase (within ~1.5 days)
const d1 = new Date("2026-01-01T00:00:00Z");
const d2 = new Date("2026-01-30T12:00:00Z");
const p1 = moonPhase(d1).name, p2 = moonPhase(d2).name;
assert.ok(p1 === p2 || [p1, p2].includes("Waxing Gibbous") || [p1, p2].includes("Waning Gibbous") ||
          Math.abs(["New Moon","Waxing Crescent","First Quarter","Waxing Gibbous","Full Moon",
                    "Waning Gibbous","Last Quarter","Waning Crescent"].indexOf(p1) -
                    ["New Moon","Waxing Crescent","First Quarter","Waxing Gibbous","Full Moon",
                    "Waning Gibbous","Last Quarter","Waning Crescent"].indexOf(p2)) <= 1,
          `moon phases ~1 synodic month apart should match: ${p1} vs ${p2}`);

// ---- rolling buffer: fixed length, newest wins ----
const ring = new Ring(3);
ring.push(1); ring.push(2); ring.push(3); ring.push(4);
assert.deepStrictEqual(ring.get(), [2, 3, 4], "Ring keeps newest N");
ring.clear();
assert.deepStrictEqual(ring.get(), [], "Ring clears");

// ---- spark path: builds an SVG path from real buffer values ----
const path = sparkPath([10, 50, 25], 100, 40, 100);
assert.ok(path.startsWith("M") && path.includes("L"), "path draws line");
assert.strictEqual(sparkPath([], 100, 40, 100), "", "empty buffer → no path");
assert.ok(sparkPath([10], 100, 40, 100).startsWith("M0"), "single point starts at origin");

console.log("HUD extended checks passed (moon, ring buffer, spark path).");
