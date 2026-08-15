/* Phantom HUD — real-telemetry helpers and gauge renderer.
 *
 * Rules (see docs/JARVIS-SPEC.md):
 *  - Rendering tiers come from the ONE existing state machine: voice states
 *    (body[data-state]) + presence sleeping class (body.presence-sleeping).
 *    hudTier() is a pure function of those two — no second state system.
 *  - Every gauge shows a real reading from /api/hud or an honest
 *    "unavailable" — never a simulated number.
 *  - The low tier pauses animation and polls on a slow interval.
 *
 * Pure helpers (hudTier, avgBins, formatRate) work in Node for tests;
 * HudGauges needs a DOM.
 */
(function () {
  "use strict";

  // Sleeping (or silenced) → low-power tier: minimal rendering, slow gauges.
  // Everything else (idle/listening/speaking/thinking/…) → full tier.
  function hudTier(state, sleeping) {
    if (sleeping) return "low";
    return "full";
  }

  // Average a real frequency-domain byte array (0..255) into `outCount` bars.
  // Returns an array of 0..255 values. Empty input → empty output (caller
  // renders a flat, honest "no audio data" state).
  function avgBins(freq, outCount) {
    if (!freq || !freq.length || !outCount || outCount <= 0) return [];
    const out = new Array(outCount).fill(0);
    const per = freq.length / outCount;
    for (let i = 0; i < outCount; i++) {
      const start = Math.floor(i * per);
      const end = Math.max(start + 1, Math.floor((i + 1) * per));
      let sum = 0;
      for (let j = start; j < end; j++) sum += freq[j] || 0;
      out[i] = sum / (end - start);
    }
    return out;
  }

  // Human-friendly byte-rate: "1.2 MB/s", "340 KB/s", "12 B/s".
  function formatRate(bps) {
    const v = Number(bps) || 0;
    if (v >= 1024 * 1024) return (v / 1024 / 1024).toFixed(1) + " MB/s";
    if (v >= 1024) return Math.round(v / 1024) + " KB/s";
    return Math.round(v) + " B/s";
  }

  // ---------------------------------------------------------------------
  // Gauge renderer: polls /api/hud on an interval that depends on the tier
  // (2s awake, 8s sleeping) and renders real readings.
  // ---------------------------------------------------------------------
  class HudGauges {
    constructor() {
      this.tier = "full";
      this._timer = null;
      this._last = null;
      this._rendering = false;
    }

    setTier(tier) {
      this.tier = tier === "low" ? "low" : "full";
      this._reschedule();
    }

    stop() {
      if (this._timer) { clearInterval(this._timer); this._timer = null; }
    }

    _reschedule() {
      if (this._timer) { clearInterval(this._timer); this._timer = null; }
      const ms = this.tier === "low" ? 8000 : 2000;
      this._poll();
      this._timer = setInterval(() => this._poll(), ms);
    }

    async _poll() {
      if (this._rendering) return;
      this._rendering = true;
      try {
        const res = await fetch("/api/hud");
        if (res.ok) {
          this._last = await res.json();
          this._render();
        }
      } catch (e) {
        // backend not ready yet — gauges keep their "—" placeholders
      } finally {
        this._rendering = false;
      }
    }

    _el(id) { return document.getElementById(id); }

    _render() {
      const d = this._last;
      if (!d) return;
      this._renderCpu(d.cpu);
      this._renderDisk(d.disk);
      this._renderNet(d.net);
      this._renderBattery(d.battery);
      this._renderProcess(d.process);
      this._renderSpectrumSource();
    }

    _renderCpu(cpu) {
      const box = this._el("hudCpuBars");
      const label = this._el("hudCpuVal");
      if (!box) return;
      if (!cpu || cpu.available === false) {
        box.innerHTML = `<span class="muted small">unavailable</span>`;
        if (label) label.textContent = cpu && cpu.reason ? cpu.reason : "unavailable";
        return;
      }
      const cores = cpu.per_core || [];
      const html = cores.map((p) => {
        const h = Math.max(3, Math.min(100, p));
        const hot = p >= 80 ? " hot" : p >= 50 ? " warm" : "";
        return `<div class="cpu-bar${hot}" style="height:${h}%"></div>`;
      }).join("");
      box.innerHTML = html;
      if (label) label.textContent = `total ${cpu.total || 0}% · ${cores.length} cores`;
    }

    _renderDisk(disk) {
      const el = this._el("hudDiskVal");
      if (!el) return;
      if (!disk || disk.available === false) { el.textContent = "unavailable"; return; }
      el.textContent = `R ${formatRate(disk.read_bps)} · W ${formatRate(disk.write_bps)}`;
    }

    _renderNet(net) {
      const el = this._el("hudNetVal");
      if (!el) return;
      if (!net || net.available === false) { el.textContent = "unavailable"; return; }
      el.textContent = `↓ ${formatRate(net.down_bps)} · ↑ ${formatRate(net.up_bps)}`;
    }

    _renderBattery(batt) {
      const el = this._el("hudBattVal");
      if (!el) return;
      if (!batt || batt.available === false) {
        el.textContent = "unavailable";
        el.title = (batt && batt.reason) || "no battery on this device";
        return;
      }
      const plug = batt.plugged ? " 🔌" : "";
      el.textContent = `${batt.percent}%${plug}`;
      el.title = `battery ${batt.percent}%${plug}`;
    }

    _renderProcess(proc) {
      const el = this._el("hudProcVal");
      if (!el) return;
      if (!proc || !proc.name) { el.textContent = "—"; return; }
      if (proc.unavailable) { el.textContent = "unavailable"; return; }
      el.textContent = `${proc.name} ${proc.cpu_percent || 0}%`;
      el.title = `PID ${proc.pid || "?"}`;
    }

    _renderSpectrumSource() {
      const el = this._el("hudSpecVal");
      if (!el) return;
      const st = document.body.dataset.state || "";
      let text = "—";
      if (st === "listening") text = "🎙 mic audio";
      else if (st === "speaking") {
        const v = window.voice;
        text = (v && v.ttsSpectrumAvailable) ? "🔊 TTS audio" : "🔊 system voice";
      }
      el.textContent = text;
    }
  }

  const api = { hudTier, avgBins, formatRate, HudGauges };
  if (typeof window !== "undefined") window.PhantomHud = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})();
