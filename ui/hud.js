/* Phantom HUD — full radial Stark-style layout with real telemetry.
 *
 * Rules (see docs/JARVIS.md):
 *  - Rendering tiers come from the ONE existing state machine (voice state +
 *    presence sleeping class) via hudTier() — no second state system.
 *  - Every panel shows a real reading or an honest "unavailable" — never a
 *    static mock or a fake number.
 *  - Weather = Open-Meteo via the backend proxy (/api/hud/weather, no key).
 *  - Moon phase = computed client-side from the actual date (astronomical).
 *  - CPU/mem/net history = rolling buffers fed by real /api/hud polls.
 *  - Spectrum strip = real AnalyserNode data (mic while listening, TTS while
 *    speaking) — only live in the full tier.
 *
 * Pure helpers (hudTier, avgBins, formatRate, moonPhase, sparkPath) work in
 * Node for tests; HudGauges + renderers need a DOM.
 */
(function () {
  "use strict";

  // Sleeping (or silenced) → low-power tier; everything else → full tier.
  function hudTier(state, sleeping) {
    return sleeping ? "low" : "full";
  }

  // Average a real frequency-domain byte array (0..255) into `outCount` bars.
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

  // Human-friendly byte-rate.
  function formatRate(bps) {
    const v = Number(bps) || 0;
    if (v >= 1024 * 1024) return (v / 1024 / 1024).toFixed(1) + " MB/s";
    if (v >= 1024) return Math.round(v / 1024) + " KB/s";
    return Math.round(v) + " B/s";
  }

  // Moon phase from a real date (synodic month; 0 = new moon).
  // Known new moon: 2000-01-06 18:14 UTC.
  function moonPhase(date) {
    const synodic = 29.53058867;
    const knownNew = Date.UTC(2000, 0, 6, 18, 14);
    const days = (date.getTime() - knownNew) / 86400000;
    const phase = ((days % synodic) + synodic) % synodic;
    const frac = phase / synodic;
    const names = ["New Moon", "Waxing Crescent", "First Quarter", "Waxing Gibbous",
                   "Full Moon", "Waning Gibbous", "Last Quarter", "Waning Crescent"];
    const icons = ["🌑", "🌒", "🌓", "🌔", "🌕", "🌖", "🌗", "🌘"];
    const idx = Math.floor(frac * 8) % 8;
    return { name: names[idx], icon: icons[idx], illumination: Math.round((1 - Math.cos(2 * Math.PI * frac)) * 50) };
  }

  // Build an SVG polyline path for a rolling buffer (area/line chart data).
  function sparkPath(values, w, h, max) {
    if (!values || !values.length) return "";
    const n = values.length;
    const step = w / Math.max(n - 1, 1);
    const scale = max > 0 ? h / max : 1;
    let d = "";
    for (let i = 0; i < n; i++) {
      const x = (n === 1 ? 0 : i * step).toFixed(1);
      const y = (h - Math.min(values[i], max) * scale).toFixed(1);
      d += (i === 0 ? "M" : "L") + x + "," + y;
    }
    return d;
  }

  // Rolling buffer helper (fixed length, push newest).
  class Ring {
    constructor(max) { this.max = max; this.items = []; }
    push(v) { this.items.push(v); if (this.items.length > this.max) this.items.shift(); }
    get() { return this.items; }
    clear() { this.items = []; }
  }

  // ---------------------------------------------------------------------
  // Gauge renderer: polls /api/hud (2s awake, 8s sleeping), keeps rolling
  // buffers, draws history graphs, renders all panels.
  // ---------------------------------------------------------------------
  class HudGauges {
    constructor() {
      this.tier = "full";
      this._timer = null;
      this._last = null;
      this._rendering = false;
      this.cpuHist = new Ring(90);   // ~3 min at 2s
      this.memHist = new Ring(90);
      this.netDown = new Ring(90);
      this.netUp = new Ring(90);
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
          const d = await res.json();
          this._last = d;
          this.cpuHist.push(Number(d.cpu && d.cpu.total) || 0);
          this.memHist.push(Number(d.memory && d.memory.percent) || 0);
          if (d.net) {
            this.netDown.push(Number(d.net.down_bps) || 0);
            this.netUp.push(Number(d.net.up_bps) || 0);
          }
          this._render();
        }
      } catch (e) {
        // backend not ready — panels keep their placeholders
      } finally {
        this._rendering = false;
      }
    }

    _el(id) { return document.getElementById(id); }

    _render() {
      const d = this._last;
      if (!d) return;
      this._renderCpu(d.cpu);
      this._renderMemory(d.memory);
      this._renderDisk(d.disk);
      this._renderNet(d.net);
      this._renderBattery(d.battery);
      this._renderProcess(d.process, d.process_count);
      this._renderGraphs();
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

    _renderMemory(mem) {
      const label = this._el("memHistVal");
      if (!label) return;
      if (!mem || mem.available === false) { label.textContent = "unavailable"; return; }
      const used = (mem.used_bytes || 0) / 1024 / 1024 / 1024;
      const total = (mem.total_bytes || 0) / 1024 / 1024 / 1024;
      label.textContent = `${mem.percent}% · ${used.toFixed(1)}/${total.toFixed(1)} GB`;
    }

    _renderDisk(disk) {
      const el = this._el("hudDiskVal");
      if (!el) return;
      if (!disk || disk.available === false) { el.textContent = "unavailable"; return; }
      el.textContent = `R ${formatRate(disk.read_bps)}  W ${formatRate(disk.write_bps)}`;
    }

    _renderNet(net) {
      const el = this._el("hudNetVal");
      if (!el) return;
      if (!net || net.available === false) { el.textContent = "unavailable"; return; }
      el.textContent = `↓ ${formatRate(net.down_bps)}  ↑ ${formatRate(net.up_bps)}`;
    }

    _renderBattery(batt) {
      const el = this._el("sysBatt");
      if (!el) return;
      if (!batt || batt.available === false) {
        el.textContent = "battery: unavailable";
        el.title = (batt && batt.reason) || "no battery on this device";
        return;
      }
      el.textContent = `battery: ${batt.percent}%${batt.plugged ? " 🔌" : ""}`;
      el.title = `battery ${batt.percent}%`;
    }

    _renderProcess(proc, count) {
      const el = this._el("sysProc");
      if (el) {
        if (!proc || !proc.name) el.textContent = "top process: —";
        else if (proc.unavailable) el.textContent = "top process: unavailable";
        else el.textContent = `top: ${proc.name} ${proc.cpu_percent || 0}%`;
      }
      const cnt = this._el("sysCount");
      if (cnt) cnt.textContent = `processes: ${typeof count === "number" ? count : "—"}`;
    }

    _renderGraphs() {
      this._drawGraph("cpuHist", this.cpuHist.get(), 0, 100, "rgba(110,168,254,.8)");
      this._drawGraph("memHist", this.memHist.get(), 0, 100, "rgba(183,155,255,.8)");
      // network: auto-scaled to the max in the buffer
      const down = this.netDown.get();
      const up = this.netUp.get();
      const max = Math.max(1024, ...down, ...up);
      this._drawNetGraph("netHist", down, up, max);
    }

    _drawGraph(id, values, min, max, color) {
      const c = this._el(id);
      if (!c) return;
      const ctx = c.getContext && c.getContext("2d");
      if (!ctx) return;
      const w = c.width, h = c.height;
      ctx.clearRect(0, 0, w, h);
      if (!values.length) {
        ctx.fillStyle = "rgba(110,168,254,.25)";
        ctx.font = "10px sans-serif";
        ctx.fillText("waiting for data…", 8, h / 2 + 3);
        return;
      }
      const span = Math.max(max - min, 1);
      ctx.beginPath();
      for (let i = 0; i < values.length; i++) {
        const x = (values.length === 1 ? 0 : (i / (values.length - 1)) * w);
        const y = h - ((Math.min(Math.max(values[i], min), max) - min) / span) * (h - 4) - 2;
        i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
      }
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.6;
      ctx.stroke();
    }

    _drawNetGraph(id, down, up, max) {
      const c = this._el(id);
      if (!c) return;
      const ctx = c.getContext && c.getContext("2d");
      if (!ctx) return;
      const w = c.width, h = c.height;
      ctx.clearRect(0, 0, w, h);
      const draw = (vals, color) => {
        if (!vals.length) return;
        ctx.beginPath();
        for (let i = 0; i < vals.length; i++) {
          const x = (vals.length === 1 ? 0 : (i / (vals.length - 1)) * w);
          const y = h - (Math.min(vals[i], max) / max) * (h - 4) - 2;
          i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
        }
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.4;
        ctx.stroke();
      };
      draw(down, "rgba(110,168,254,.85)");
      draw(up, "rgba(46,204,113,.7)");
      if (!down.length && !up.length) {
        ctx.fillStyle = "rgba(110,168,254,.25)";
        ctx.font = "10px sans-serif";
        ctx.fillText("waiting for traffic…", 8, h / 2 + 3);
      }
    }

    _renderSpectrumSource() {
      const el = this._el("hudSpecVal");
      if (!el) return;
      const st = document.body.dataset.state || "";
      let text = "spectrum idle";
      if (st === "listening") text = "spectrum · mic audio";
      else if (st === "speaking") {
        const v = window.voice;
        text = (v && v.ttsSpectrumAvailable) ? "spectrum · TTS audio" : "spectrum · system voice";
      }
      el.textContent = text;
    }
  }

  // ---------------------------------------------------------------------
  // Panel renderers: clock, session badge, moon, weather.
  // ---------------------------------------------------------------------
  function startClock() {
    const timeEl = document.getElementById("clockTime");
    const dateEl = document.getElementById("clockDate");
    if (!timeEl) return;
    const tick = () => {
      const now = new Date();
      timeEl.textContent = now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
      if (dateEl) dateEl.textContent = now.toLocaleDateString([], { weekday: "short", year: "numeric", month: "short", day: "numeric" });
    };
    tick();
    setInterval(tick, 1000);
  }

  function renderSession() {
    const osEl = document.getElementById("sessionOs");
    const verEl = document.getElementById("sessionVer");
    const userEl = document.getElementById("sessionUser");
    const avatar = document.getElementById("sessionAvatar");
    if (osEl) osEl.textContent = "Phantom OS";
    const userName = (window.state && window.state.userName) ||
      localStorage.getItem("phantom.userName") || "JOOJO";
    if (userEl) userEl.textContent = userName;
    if (avatar) avatar.textContent = (userName[0] || "👤").toUpperCase();
    if (verEl) {
      verEl.textContent = "v" + (window.appVersion || "—");
    }
  }

  const WMO_CODES = {
    0: ["☀️", "Clear"], 1: ["🌤", "Mostly clear"], 2: ["⛅", "Partly cloudy"],
    3: ["☁️", "Overcast"], 45: ["🌫", "Fog"], 48: ["🌫", "Rime fog"],
    51: ["🌦", "Light drizzle"], 53: ["🌦", "Drizzle"], 55: ["🌧", "Heavy drizzle"],
    61: ["🌧", "Light rain"], 63: ["🌧", "Rain"], 65: ["🌧", "Heavy rain"],
    71: ["🌨", "Light snow"], 73: ["🌨", "Snow"], 75: ["❄️", "Heavy snow"],
    80: ["🌦", "Light showers"], 81: ["🌧", "Showers"], 82: ["⛈", "Violent showers"],
    95: ["⛈", "Thunderstorm"], 96: ["⛈", "Thunderstorm + hail"], 99: ["⛈", "Severe storm"],
  };
  function wmo(code) { return WMO_CODES[code] || ["🌡", "Unknown"]; }

  async function loadWeather() {
    const body = document.getElementById("weatherBody");
    if (!body) return;
    try {
      const res = await fetch("/api/hud/weather");
      const w = await res.json();
      if (!w.available) { body.innerHTML = `<span class="muted small">weather: ${(w.reason || "unavailable").toLowerCase()}</span>`; return; }
      const cur = w.current || {};
      const daily = (w.daily || {});
      const [icon, label] = wmo(cur.weather_code);
      const sun = (daily.sunrise && daily.sunrise[0]) || "";
      const set = (daily.sunset && daily.sunset[0]) || "";
      const fmt = (iso) => iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "—";
      let outlook = "";
      if (daily.time) {
        for (let i = 1; i < Math.min(daily.time.length, 5); i++) {
          const [ic, lab] = wmo((daily.weather_code || [])[i]);
          outlook += `<div class="wx-day"><span>${new Date(daily.time[i]).toLocaleDateString([], { weekday: "short" })}</span><span>${ic} ${(daily.temperature_2m_min || [])[i]}°/${(daily.temperature_2m_max || [])[i]}°</span></div>`;
        }
      }
      body.innerHTML = `
        <div class="wx-now">${icon} <b>${Math.round(cur.temperature_2m ?? 0)}°</b> ${label}</div>
        <div class="muted small">feels ${Math.round(cur.apparent_temperature ?? 0)}° · hum ${cur.relative_humidity_2m ?? "—"}% · wind ${Math.round(cur.wind_speed_10m ?? 0)} km/h · ${Math.round(cur.surface_pressure ?? 0)} hPa</div>
        <div class="muted small">☀ ${fmt(sun)} · ☾ ${fmt(set)}</div>
        <div class="wx-days">${outlook || ""}</div>`;
    } catch (e) {
      body.innerHTML = `<span class="muted small">weather: unavailable</span>`;
    }
  }

  function renderMoon() {
    const body = document.getElementById("moonBody");
    if (!body) return;
    const m = moonPhase(new Date());
    body.innerHTML = `<div class="moon-row">${m.icon} <b>${m.name}</b></div>
      <div class="muted small">illumination ${m.illumination}%</div>`;
  }

  // ---------------------------------------------------------------------
  // Spectrum strip: horizontal AnalyserNode bars (real audio only).
  // Runs ONLY in the full tier (awake). Static dim frame while sleeping.
  // ---------------------------------------------------------------------
  class SpectrumStrip {
    constructor() {
      this.canvas = document.getElementById("specStrip");
      this.ctx = this.canvas && this.canvas.getContext("2d");
      this.raf = null;
      this.active = false;
      this.tier = "full";
    }
    setTier(tier) {
      this.tier = tier === "low" ? "low" : "full";
      if (this.tier === "low") { this._stop(); this._drawStatic(); }
      else this._start();
    }
    _start() {
      if (this.active || !this.ctx) return;
      this.active = true;
      const loop = () => {
        if (!this.active) return;
        this._frame();
        this.raf = requestAnimationFrame(loop);
      };
      this.raf = requestAnimationFrame(loop);
    }
    _stop() { this.active = false; if (this.raf) { cancelAnimationFrame(this.raf); this.raf = null; } }
    _drawStatic() {
      if (!this.ctx || !this.canvas) return;
      const w = this.canvas.width = this.canvas.clientWidth || 600;
      const h = this.canvas.height = this.canvas.clientHeight || 40;
      this.ctx.clearRect(0, 0, w, h);
      this.ctx.fillStyle = "rgba(110,168,254,.12)";
      this.ctx.fillRect(0, h - 2, w, 2);
    }
    _frame() {
      if (!this.ctx || !this.canvas) return;
      const w = this.canvas.width = this.canvas.clientWidth || 600;
      const h = this.canvas.height = this.canvas.clientHeight || 40;
      this.ctx.clearRect(0, 0, w, h);
      const st = document.body.dataset.state || "";
      const sleeping = document.body.classList.contains("presence-sleeping");
      let freq = null;
      if (!sleeping) {
        const v = window.voice;
        if (st === "listening" && v && v.micEnabled) freq = v.getMicSpectrum ? v.getMicSpectrum(128) : null;
        else if (st === "speaking" && v) freq = v.getTtsSpectrum ? v.getTtsSpectrum(128) : null;
      }
      const bars = 64;
      const bins = avgBins(freq || [], bars);
      const bw = w / bars;
      const base = h - 6;
      for (let i = 0; i < bars; i++) {
        const amp = bins.length ? bins[i] / 255 : 0;
        const bh = Math.max(2, amp * (h - 10));
        const x = i * bw + bw * 0.18;
        this.ctx.fillStyle = amp > 0.5 ? "rgba(110,168,254,.9)" : "rgba(110,168,254,.45)";
        this.ctx.fillRect(x, base - bh, bw * 0.64, bh);
      }
      if (!bins.length) {
        this.ctx.fillStyle = "rgba(110,168,254,.22)";
        this.ctx.fillRect(0, base, w, 2);
      }
    }
  }

  const api = { hudTier, avgBins, formatRate, moonPhase, sparkPath, Ring,
                HudGauges, SpectrumStrip, startClock, renderSession, loadWeather, renderMoon };
  if (typeof window !== "undefined") window.PhantomHud = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})();
