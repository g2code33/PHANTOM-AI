/* Phantom — real-time voice engine (voice-first interaction layer).
 *
 * States: IDLE LISTENING THINKING SPEAKING INTERRUPTED EXECUTING VERIFYING
 *         ERROR DISCONNECTED
 *
 * Providers (abstraction — add more later):
 *   STT: browser (Web Speech API, streaming interim results) | deepgram (WS)
 *   TTS: browser (speechSynthesis, streaming, cancellable)   | deepgram (WS)
 *
 * Features: streaming recognition, streaming synthesis with a speak queue,
 * energy-based VAD (end-of-turn + barge-in), interruption/cancellation,
 * reconnect handling, mic/speaker state, mute, kill switch.
 *
 * The Deepgram API key never touches the frontend: the server mints a short-
 * lived ephemeral token (POST /api/voice/deepgram-token).
 */
"use strict";

class PhantomVoice {
  constructor() {
    this.state = "IDLE";
    this.mode = "conversation";      // private | push | conversation
    this.sttProvider = "server";     // browser | deepgram | server (failover chain)
    this.ttsProvider = "server";     // browser | deepgram | server (failover chain)
    this.ttsVoice = "";
    this.voices = { phantom: "", coded: "" };  // per-persona voices (Phase 3)
    this.currentAgent = "phantom";   // whose voice to use when speaking
    this.proactiveSpeech = false;
    this.deepgramConfigured = false;
    this.groqConfigured = false;
    this.muted = false;              // master mute (no TTS)
    this.micEnabled = true;
    this.killEngaged = false;
    // multi-provider voice engine settings
    this.vadThreshold = 0.03;        // speech level threshold
    this.autoStopMs = 900;           // silence before an utterance is sent
    this.maxRecordMs = 15000;        // hard cap per utterance
    this.continuous = true;          // keep listening after each utterance
    this.lastSttProvider = "";       // which provider handled the last STT
    this.lastTtsProvider = "";       // which provider handled the last TTS
    this._srvRecording = false;
    this._srvChunks = [];
    this._srvSilenceMs = 0;
    this._srvLastTick = 0;
    this._srvSource = null;
    this._srvProc = null;
    this._srvFlushing = false;
    this._serverAudio = null;
    // TTS playback analyser — real audio data for the HUD spectrum (speaking)
    this._ttsAnalyser = null;
    this.ttsSpectrumAvailable = false;

    this._micStream = null;
    this._audioCtx = null;
    this._analyser = null;
    this._vadRaf = null;
    this._lastLevel = 0;

    this._recognition = null;
    this._recognitionActive = false;
    this._dgListenWs = null;
    this._dgSpeakWs = null;
    this._dgAudioQueue = [];
    this._dgPlaying = false;
    this._dgGain = null;

    this._speakQueue = [];
    this._speaking = false;
    this._utterance = null;

    this._vadBuf = [];
    this._endTurnTimer = null;
    this._interruptTimer = null;

    this.onState = null;      // (state) => void
    this.onInterim = null;    // (text) => void
    this.onFinal = null;      // (text) => void
    this.onAudioLevel = null; // (0..1) => void
    this.onSpeakStart = null;
    this.onSpeakEnd = null;
    this.onError = null;      // (message) => void
    this.onWakeWord = null;   // (agent) => void — wake word heard while sleeping
    this.onInterrupt = null;  // (void) => void — user barged in
    this.wakeEngine = "browser";
    this._wakeRec = null;
    this._wakeActive = false;
    this._hotFrames = 0;
  }

  // ---------------------------------------------------------------- config
  async init() {
    this._installGestureUnlock();
    try {
      const res = await fetch("/api/voice/config");
      const cfg = await res.json();
      this.mode = cfg.mode || "conversation";
      this.sttProvider = cfg.stt?.provider || "browser";
      // JARVIS: browser/system voices by default so speech ALWAYS works;
      // switch to 'server' only when a Deepgram key is configured (better
      // voices) — the UI can change this in Settings.
      this.ttsProvider = cfg.tts?.provider || "browser";
      this.ttsVoice = cfg.tts?.voice || "";
      this.voices = { phantom: cfg.voices?.phantom || "", coded: cfg.voices?.coded || "" };
      this.proactiveSpeech = !!cfg.proactive_speech;
      this.deepgramConfigured = !!cfg.deepgram_configured;
      this.groqConfigured = !!cfg.groq_configured;
      if (typeof cfg.vad_threshold === "number") this.vadThreshold = cfg.vad_threshold;
      if (typeof cfg.auto_stop_ms === "number") this.autoStopMs = cfg.auto_stop_ms;
      if (typeof cfg.max_record_ms === "number") this.maxRecordMs = cfg.max_record_ms;
      if (typeof cfg.continuous === "boolean") this.continuous = cfg.continuous;
      if (this.mode === "private") this.micEnabled = false;
    } catch (e) {
      this.onError?.("voice config unavailable: " + e.message);
    }
  }

  setState(s) {
    this.state = s;
    document.body.dataset.state = s.toLowerCase();
    this.onState?.(s);
  }

  setMode(mode) {
    this.mode = mode;
    if (mode === "private") {
      this.micEnabled = false;
      this.stopListening();
      this.stopSpeaking();
    } else {
      this.micEnabled = true;
    }
    this.onState?.(this.state);
  }

  setMuted(m) { this.muted = m; if (m) this.stopSpeaking(); }
  setKill(k) {
    this.killEngaged = k;
    if (k) { this.stopAll(); this.setState("IDLE"); }
  }

  // ---------------------------------------------------------------- mic/VAD
  // unlock audio on the very first user gesture (click/keydown/touch)
  _installGestureUnlock() {
    if (this._unlockInstalled) return;
    this._unlockInstalled = true;
    const unlock = () => { this.unlockAudio(); };
    ["pointerdown", "keydown", "touchstart"].forEach((ev) =>
      window.addEventListener(ev, unlock, { once: false, passive: true }));
  }

  async startMic() {
    if (!this.micEnabled || this.killEngaged) return;
    if (this._micStream) return;
    try {
      this._micStream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true,
                 autoGainControl: true },
      });
      this._audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      this._analyser = this._audioCtx.createAnalyser();
      this._analyser.fftSize = 512;
      this._audioCtx.createMediaStreamSource(this._micStream).connect(this._analyser);
      this._vadLoop();
    } catch (e) {
      this.onError?.("microphone unavailable: " + e.message);
      this.setState("ERROR");
    }
  }

  stopMic() {
    this._micStream?.getTracks().forEach((t) => t.stop());
    this._micStream = null;
    if (this._audioCtx && this._audioCtx.state !== "closed") this._audioCtx.close().catch(() => {});
    this._audioCtx = null;
    this._analyser = null;
    if (this._vadRaf) cancelAnimationFrame(this._vadRaf);
  }

  // ------------------------------------------------ HUD spectrum (real data)
  // Returns the raw frequency-domain bytes from the LIVE mic analyser
  // (same MediaStream — no second mic stream) or null when not capturing.
  getMicSpectrum(n = 64) {
    if (!this._analyser) return null;
    const data = new Uint8Array(this._analyser.frequencyBinCount);
    this._analyser.getByteFrequencyData(data);
    return Array.from(data);
  }

  // Returns the frequency-domain bytes of the TTS playback audio (server or
  // Deepgram TTS routes through an AnalyserNode) or null when unavailable
  // (e.g. system speechSynthesis voices cannot be tapped — honest "no data").
  getTtsSpectrum(n = 64) {
    if (!this._ttsAnalyser || !this.ttsSpectrumAvailable) return null;
    const data = new Uint8Array(this._ttsAnalyser.frequencyBinCount);
    this._ttsAnalyser.getByteFrequencyData(data);
    return Array.from(data);
  }

  // Route a TTS audio element through an AnalyserNode so the HUD can draw
  // the REAL spectrum of what Phantom/Coded is saying.
  _wireTtsAnalyser(audio) {
    this.ttsSpectrumAvailable = false;
    try {
      if (this._audioCtx && this._audioCtx.state === "closed") this._audioCtx = null;
      if (!this._audioCtx) {
        this._audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      }
      if (this._audioCtx.state === "suspended") this._audioCtx.resume().catch(() => {});
      const ctx = this._audioCtx;
      this._ttsAnalyser = ctx.createAnalyser();
      this._ttsAnalyser.fftSize = 512;
      this._ttsAnalyser.smoothingTimeConstant = 0.8;
      const src = ctx.createMediaElementSource(audio);
      src.connect(this._ttsAnalyser);
      this._ttsAnalyser.connect(ctx.destination);
      this.ttsSpectrumAvailable = true;
    } catch (e) {
      // element-source failed — play plainly; HUD shows "system voice" honestly
      this._ttsAnalyser = null;
      this.ttsSpectrumAvailable = false;
    }
  }

  _vadLoop() {
    let _lv = 0;
    const tick = () => {
      if (performance.now() - _lv < 33) { this._vadRaf = requestAnimationFrame(tick); return; }  // ~30fps
      _lv = performance.now();
      if (!this._analyser) return;
      const buf = new Float32Array(this._analyser.fftSize);
      this._analyser.getFloatTimeDomainData(buf);
      let sum = 0;
      for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
      const rms = Math.sqrt(sum / buf.length);
      const level = Math.min(1, rms * 6);
      this._lastLevel = level;
      this.onAudioLevel?.(level);
      this._vad(level);
      this._vadRaf = requestAnimationFrame(tick);
    };
    this._vadRaf = requestAnimationFrame(tick);
  }

  _vad(level) {
    const SPEECH = this.vadThreshold || 0.030, SILENCE = 0.012;  // slightly more sensitive for barge
    // server STT: gate recording on speech, flush after auto-stop silence
    if (this.sttProvider === "server" && this.state === "LISTENING" && this._srvFlushing) {
      return;  // wait for the in-flight transcription to finish
    }
    if (this.sttProvider === "server" && this.state === "LISTENING" && this.micEnabled) {
      const now = Date.now();
      if (!this._srvLastTick) this._srvLastTick = now;
      if (level > SPEECH) {
        if (!this._srvRecording) { this._srvRecording = true; this._srvChunks = []; this._srvSilenceMs = 0; }
        this._srvSilenceMs = 0;
        this._srvLastTick = now;
      } else if (this._srvRecording) {
        this._srvSilenceMs += now - this._srvLastTick;
        this._srvLastTick = now;
        if (this._srvSilenceMs >= this.autoStopMs) { this._srvSilenceMs = 0; this._flushServerSTT(); }
      }
      return;
    }
    if (level > SPEECH) {
      this._vadBuf.push(Date.now());
      if (this._vadBuf.length > 30) this._vadBuf.shift();
      // barge-in: the moment the user speaks while we speak → stop and
      // listen for their redirect (like two people talking). One hot frame
      // = instant; jitter is filtered by the level threshold itself.
      if (this.state === "SPEAKING" && this.micEnabled) {
        if (this._hotFrames === undefined) this._hotFrames = 0;
        this._hotFrames += 1;
        if (this._hotFrames >= 1) {
          this._hotFrames = 0;
          this.bargeIn();
        }
      }
    } else {
      this._hotFrames = 0;
      if (level < SILENCE && this._vadBuf.length) {
        this._vadBuf = [];
        // end-of-turn while listening and idle (no agent running) → send final
        if (this.state === "LISTENING" && this._endTurnTimer === null) {
          this._endTurnTimer = setTimeout(() => {
            this._endTurnTimer = null;
          }, 0); // end-of-turn is handled by the STT engine's onend/final
        }
      }
    }
  }

  // ---------------------------------------------------------------- STT
  async startListening() {
    if (this.killEngaged || !this.micEnabled) return;
    if (this.state === "LISTENING" || this.state === "THINKING") return;
    await this.startMic();
    if (this.sttProvider === "deepgram" && this.deepgramConfigured) {
      await this._startDeepgramListen();
    } else if (this.sttProvider === "server") {
      this._startServerListen();
    } else {
      this._startBrowserSTT();
    }
    this.setState("LISTENING");
  }

  stopListening() {
    this._stopBrowserSTT();
    this._closeDeepgramListen();
    this._closeServerListen();
    if (this.state === "LISTENING") this.setState("IDLE");
  }

  _startBrowserSTT() {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) {
      this.onError?.("speech recognition not supported in this browser");
      this.setState("ERROR");
      return;
    }
    const rec = new SR();
    rec.lang = "en-US";
    rec.interimResults = true;
    rec.continuous = true;
    rec.onresult = (ev) => {
      let interim = "", final = "";
      for (let i = ev.resultIndex; i < ev.results.length; i++) {
        if (ev.results[i].isFinal) final += ev.results[i][0].transcript;
        else interim += ev.results[i][0].transcript;
      }
      if (interim) this.onInterim?.(interim);
      if (final.trim()) {
        this.onInterim?.("");
        this.onFinal?.(final.trim());
      }
    };
    rec.onerror = (ev) => {
      if (ev.error === "not-allowed" || ev.error === "service-not-allowed") {
        this.onError?.("microphone permission denied");
        this.setState("ERROR");
      }
    };
    rec.onend = () => {
      this._recognitionActive = false;
      // auto-restart if we should still be listening (reconnect handling)
      if (this.state === "LISTENING" && this.micEnabled && !this.killEngaged) {
        try { rec.start(); this._recognitionActive = true; } catch (e) { /* already started */ }
      }
    };
    this._recognition = rec;
    try { rec.start(); this._recognitionActive = true; } catch (e) { /* ignore */ }
  }

  _stopBrowserSTT() {
    if (this._recognition) {
      try { this._recognition.onend = null; this._recognition.stop(); } catch (e) {}
      this._recognition = null;
    }
    this._recognitionActive = false;
  }

  async _startDeepgramListen() {
    this._closeDeepgramListen();
    try {
      const res = await fetch("/api/voice/deepgram-token", { method: "POST" });
      if (!res.ok) throw new Error((await res.json()).detail || "deepgram token failed");
      const { token } = await res.json();
      const ws = new WebSocket(
        "wss://api.deepgram.com/v1/listen?model=nova-2&interim_results=true&encoding=linear16&sample_rate=48000&endpointing=400&vad_events=true",
        ["token", token],
      );
      ws.onopen = () => { this._dgListenWs = ws; this._pumpMicToDeepgram(); };
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          if (msg.type === "Results") {
            const alt = msg.channel?.alternatives?.[0];
            if (!alt) return;
            if (msg.is_final && alt.transcript.trim()) {
              this.onInterim?.("");
              this.onFinal?.(alt.transcript.trim());
            } else if (alt.transcript.trim()) {
              this.onInterim?.(alt.transcript.trim());
            }
          }
        } catch (e) { /* non-JSON */ }
      };
      ws.onerror = () => this.onError?.("deepgram STT connection error");
      ws.onclose = () => {
        if (this.state === "LISTENING" && !this.killEngaged) {
          this.setState("DISCONNECTED");
          setTimeout(() => { if (this.state === "DISCONNECTED") this.startListening(); }, 1500);
        }
      };
    } catch (e) {
      this.onError?.("deepgram STT failed (" + e.message + ") — falling back to browser");
      this.sttProvider = "browser";
      this._startBrowserSTT();
    }
  }

  _closeDeepgramListen() {
    if (this._dgListenWs) {
      try { this._dgListenWs.onclose = null; this._dgListenWs.close(); } catch (e) {}
      this._dgListenWs = null;
    }
  }

  // ------------------------------------------- server STT (failover chain)
  _startServerListen() {
    this._closeServerListen();
    if (!this._audioCtx || !this._micStream) {
      this.onError?.("microphone not ready");
      return;
    }
    this._srvRecording = false;
    this._srvChunks = [];
    this._srvSilenceMs = 0;
    this._srvLastTick = 0;
    this._srvFlushing = false;
    const ctx = this._audioCtx;
    const source = ctx.createMediaStreamSource(this._micStream);
    const proc = ctx.createScriptProcessor(4096, 1, 1);
    proc.onaudioprocess = (ev) => {
      if (!this._srvRecording) return;
      this._srvChunks.push(new Float32Array(ev.inputBuffer.getChannelData(0)));
      const dur = this._srvDurMs();
      if (dur >= this.maxRecordMs) this._flushServerSTT();
    };
    source.connect(proc);
    proc.connect(ctx.destination);
    this._srvSource = source;
    this._srvProc = proc;
  }

  _closeServerListen() {
    if (this._srvSource) { try { this._srvSource.disconnect(); } catch (e) {} this._srvSource = null; }
    if (this._srvProc) { try { this._srvProc.disconnect(); } catch (e) {} this._srvProc = null; }
    this._srvRecording = false;
    this._srvChunks = [];
    this._srvFlushing = false;
  }

  _srvDurMs() {
    // total captured audio duration at the current context sample rate
    const ctx = this._audioCtx;
    if (!ctx) return 0;
    let n = 0;
    for (const c of this._srvChunks) n += c.length;
    return (n / ctx.sampleRate) * 1000;
  }

  async _flushServerSTT() {
    if (this._srvFlushing || !this._srvChunks.length) return;
    this._srvFlushing = true;
    const ctx = this._audioCtx;
    this._srvRecording = false;
    const chunks = this._srvChunks;
    this._srvChunks = [];
    this._srvSilenceMs = 0;
    let len = 0;
    for (const c of chunks) len += c.length;
    const merged = new Float32Array(len);
    let off = 0;
    for (const c of chunks) { merged.set(c, off); off += c.length; }
    const wav = PhantomVoice.encodeWav(merged, ctx ? ctx.sampleRate : 48000);
    const fd = new FormData();
    fd.append("audio", wav, "utterance.wav");
    fd.append("language", "en");
    this.onInterim?.("");
    this.setState("THINKING");
    try {
      const res = await fetch("/api/voice/stt", { method: "POST", body: fd });
      if (!res.ok) {
        let detail = res.statusText;
        try { detail = (await res.json()).detail || detail; } catch (e) {}
        this.onError?.("🎙️ " + detail);
        this.setState(this.mode === "conversation" ? "LISTENING" : "IDLE");
        this._srvFlushing = false;
        if (this.mode === "conversation" && this.micEnabled) this._startServerListen();
        return;
      }
      const data = await res.json();
      this.lastSttProvider = data.provider || "";
      if (data.text && data.text.trim()) {
        this.onFinal?.(data.text.trim(), data);
      }
    } catch (e) {
      this.onError?.("🎙️ speech recognition failed: " + e.message);
    } finally {
      this._srvFlushing = false;
    }
  }

  _pumpMicToDeepgram() {
    // PCM16 capture via ScriptProcessor, sent straight to the Deepgram socket.
    if (!this._audioCtx || !this._dgListenWs) return;
    const ctx = this._audioCtx;
    const source = ctx.createMediaStreamSource(this._micStream);
    const processor = ctx.createScriptProcessor(2048, 1, 1);
    processor.onaudioprocess = (ev) => {
      if (!this._dgListenWs || this._dgListenWs.readyState !== WebSocket.OPEN) return;
      const input = ev.inputBuffer.getChannelData(0);
      const pcm = new Int16Array(input.length);
      for (let i = 0; i < input.length; i++) {
        const s = Math.max(-1, Math.min(1, input[i]));
        pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
      this._dgListenWs.send(pcm.buffer);
    };
    source.connect(processor);
    processor.connect(ctx.destination === ctx.destination ? ctx.destination : ctx.destination);
  }

  // ---------------------------------------------------------------- TTS
  // Browser autoplay policy blocks audio.play() without a prior user gesture
  // — the JARVIS fix: unlock the AudioContext on the first click/key/tap so
  // every reply can speak with NO interaction needed afterwards.
  unlockAudio() {
    try {
      if (this._audioCtx && this._audioCtx.state === "suspended") {
        this._audioCtx.resume().catch(() => {});
      }
    } catch (e) { /* ignore */ }
  }

  speak(text, opts = {}) {
    if (this.muted || this.mode === "private" || this.killEngaged) return;
    const clean = PhantomVoice._cleanSpeech(text);
    if (!clean) return;
    this._speakQueue.push({ text: clean, priority: opts.priority === "high" });
    this._drainQueue();
  }

  _drainQueue() {
    if (this._speaking || !this._speakQueue.length) return;
    const item = this._speakQueue.shift();
    if (item.priority) this._speakQueue = this._speakQueue.filter((q) => !q.priority);
    this._speaking = true;
    this.setState("SPEAKING");
    this.onSpeakStart?.();
    // FAST PATH: 'server' TTS without a Deepgram key would wait through a
    // failing chain — speak instantly with system voices instead.
    const wantsServer = (this.ttsProvider === "server" || this.ttsProvider === "deepgram") && this.deepgramConfigured;
    if (this.ttsProvider === "deepgram" && this.deepgramConfigured) {
      this._speakDeepgram(item.text);
    } else if (wantsServer) {
      this._speakServer(item.text);
    } else {
      this._speakBrowser(item.text);
    }
  }

  // strip markdown/symbols so the voice reads clean, natural speech
  static _cleanSpeech(text) {
    return String(text || "")
      .replace(/```[\s\S]*?```/g, " code block. ")
      .replace(/`([^`]*)`/g, " $1 ")
      .replace(/[*_~#>`]/g, "")
      .replace(/\[([^\]]*)\]\([^)]*\)/g, " $1 ")
      .replace(/\s+/g, " ").trim();
  }

  _speakBrowser(text) {
    // system voices can't be tapped by an AnalyserNode — honest "no spectrum"
    this._ttsAnalyser = null;
    this.ttsSpectrumAvailable = false;
    const synth = window.speechSynthesis;
    if (!synth) {
      this.onError?.("speech synthesis not supported");
      this._speaking = false;
      this.setState(this.mode === "conversation" ? "LISTENING" : "IDLE");
      return;
    }
    const clean = PhantomVoice._cleanSpeech(text);
    if (!clean) { this._speakDone(); return; }
    synth.cancel();
    const u = new SpeechSynthesisUtterance(clean);
    // slower + clearer = less choppy (browser voices stutter at 1.04)
    u.rate = 0.95;
    u.pitch = 1;
    u.volume = 1;
    // per-persona voice: use the active agent's configured voice, else global
    const pref = this.voices[this.currentAgent] || this.ttsVoice || "";
    if (pref) {
      const voices = synth.getVoices();
      const v = voices.find((x) => x.name === pref) || voices.find((x) => x.lang === pref);
      if (v) u.voice = v;
    }
    u.onend = () => this._speakDone();
    u.onerror = () => this._speakDone();
    this._utterance = u;
    synth.speak(u);
  }

  async _speakServer(text) {
    // Server-side TTS failover chain (Deepgram Aura -> cloud -> local).
    try {
      const voice = this.voices[this.currentAgent] || this.ttsVoice || "";
      const clean = PhantomVoice._cleanSpeech(text);
      const res = await fetch("/api/voice/tts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: clean || text, voice }),
      });
      if (!res.ok) {
        let detail = res.statusText;
        try { detail = (await res.json()).detail || detail; } catch (e) {}
        throw new Error(detail || "TTS failed");
      }
      const blob = await res.blob();
      this.lastTtsProvider = res.headers.get("X-TTS-Provider") || "";
      const url = URL.createObjectURL(blob);
      const audio = new Audio(url);
      this._serverAudio = audio;
      this._wireTtsAnalyser(audio); // real spectrum for the HUD while speaking
      audio.onended = () => { URL.revokeObjectURL(url); this._serverAudio = null; this._speakDone(); };
      audio.onerror = () => { URL.revokeObjectURL(url); this._serverAudio = null; this._fallbackBrowserTTS(text); };
      try { await audio.play(); }
      catch (e) {
        // autoplay may still be blocked on the first frame — resume + retry
        await this.unlockAudio();
        await new Promise((r) => setTimeout(r, 50));
        await audio.play();
      }
    } catch (e) {
      this._fallbackBrowserTTS(text);
      this.onError?.("🔊 " + e.message);
    }
  }

  _fallbackBrowserTTS(text) {
    // keep voice working even if every server TTS provider failed
    const synth = window.speechSynthesis;
    if (synth) {
      this.onError?.("🔊 Server TTS unavailable — using system voice");
      this._speakBrowser(text);
    } else {
      this._speaking = false;
      this.setState(this.mode === "conversation" ? "LISTENING" : "IDLE");
    }
  }

  async _speakDeepgram(text) {
    try {
      const res = await fetch("/api/voice/deepgram-token", { method: "POST" });
      const { token } = await res.json();
      // per-persona Deepgram aura voice (defaults: Phantom=orion, Coded=arcas)
      const dgVoice = this.voices[this.currentAgent] || this.ttsVoice ||
        (this.currentAgent === "coded" ? "aura-arcas-en" : "aura-orion-en");
      // Deepgram Aura speak: model=aura-2-english + the chosen aura voice
      // (previously the model param was set to the VOICE id — wrong, so the
      // selected voice never applied and it fell back to defaults)
      const ws = new WebSocket(
        `wss://api.deepgram.com/v1/speak?model=aura-2-english&voice=${encodeURIComponent(dgVoice)}`,
        ["token", token],
      );
      this._dgSpeakWs = ws;
      ws.onopen = () => ws.send(JSON.stringify({ type: "Speak", text: PhantomVoice._cleanSpeech(text) || text }));
      ws.onmessage = async (ev) => {
        if (typeof ev.data === "string") return;
        const blob = ev.data;
        const buf = await blob.arrayBuffer();
        this._dgAudioQueue.push(buf);
        if (!this._dgPlaying) this._playDeepgramAudio();
      };
      ws.onerror = () => this.onError?.("deepgram TTS error");
      ws.onclose = () => {
        // flush queue, then finish
        this._dgAudioQueue = [];
        this._speakDone();
      };
    } catch (e) {
      this.onError?.("deepgram TTS failed (" + e.message + ")");
      this._speaking = false;
      this.setState(this.mode === "conversation" ? "LISTENING" : "IDLE");
    }
  }

  async _playDeepgramAudio() {
    if (!this._dgAudioQueue.length) {
      this._dgPlaying = false;
      return;
    }
    this._dgPlaying = true;
    const buf = this._dgAudioQueue.shift();
    try {
      const audioBuf = await this._audioCtx.decodeAudioData(buf);
      const src = this._audioCtx.createBufferSource();
      src.buffer = audioBuf;
      // tap the real playback audio for the HUD spectrum
      this._ttsAnalyser = this._audioCtx.createAnalyser();
      this._ttsAnalyser.fftSize = 512;
      this._ttsAnalyser.smoothingTimeConstant = 0.8;
      src.connect(this._ttsAnalyser);
      this._ttsAnalyser.connect(this._audioCtx.destination);
      this.ttsSpectrumAvailable = true;
      src.onended = () => {
        if (this._dgAudioQueue.length) this._playDeepgramAudio();
        else { this._dgPlaying = false; this._speakDone(); }
      };
      src.start();
    } catch (e) {
      this._dgPlaying = false;
      if (this._dgAudioQueue.length) this._playDeepgramAudio();
      else this._speakDone();
    }
  }

  _speakDone() {
    this._speaking = false;
    this._utterance = null;
    this.ttsSpectrumAvailable = false;
    this.onSpeakEnd?.();
    if (this._dgSpeakWs) { try { this._dgSpeakWs.close(); } catch (e) {} this._dgSpeakWs = null; }
    if (this._speakQueue.length) {
      this._drainQueue();
    } else {
      this.setState(this.mode === "conversation" ? "LISTENING" : "IDLE");
    }
  }

  stopSpeaking() {
    this._speakQueue = [];
    if (window.speechSynthesis) { try { window.speechSynthesis.cancel(); } catch (e) {} }
    if (this._dgSpeakWs) { try { this._dgSpeakWs.close(); } catch (e) {} this._dgSpeakWs = null; }
    this._dgAudioQueue = [];
    this._dgPlaying = false;
    if (this._serverAudio) { try { this._serverAudio.pause(); this._serverAudio.src = ""; } catch (e) {} this._serverAudio = null; }
    this._speaking = false;
    this._utterance = null;
    this.ttsSpectrumAvailable = false;
    this.onSpeakEnd?.();
  }

  // ---------------------------------------------------------------- controls
  bargeIn() {
    if (this.state !== "SPEAKING") return;
    this.stopSpeaking();                 // cut audio immediately
    this.setState("INTERRUPTED");
    this.onInterrupt?.();
    clearTimeout(this._interruptTimer);
    this._interruptTimer = setTimeout(() => {
      if (this.mode === "conversation" && this.micEnabled) {
        this.startListening();           // fast return to listening
      } else if (this.state === "INTERRUPTED") {
        this.setState("IDLE");
      }
    }, 120);
  }

  async pushToTalkStart() {
    if (this.killEngaged) return;
    if (this.state === "SPEAKING") this.bargeIn();
    this.micEnabled = true;
    this.stopSpeaking();
    await this.startListening();
  }

  pushToTalkEnd() {
    if (this.mode === "push") {
      // give STT a moment to flush finals, then stop
      setTimeout(() => this.stopListening(), 250);
    }
  }

  stopAll() {
    this.stopSpeaking();
    this.stopListening();
    this._speakQueue = [];
  }

  // ------------------------------------------------- presence (Jarvis wake)
  playReadyCue() {
    // Siri-style ready chime + visual handled by the UI (this is the audio part)
    try {
      const ACtx = window.AudioContext || window.webkitAudioContext;
      const ctx = this._audioCtx || new ACtx();
      const o = ctx.createOscillator();
      const g = ctx.createGain();
      o.type = "sine";
      o.frequency.setValueAtTime(880, ctx.currentTime);
      o.frequency.exponentialRampToValueAtTime(1320, ctx.currentTime + 0.12);
      g.gain.setValueAtTime(0.0001, ctx.currentTime);
      g.gain.exponentialRampToValueAtTime(0.18, ctx.currentTime + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.4);
      o.connect(g).connect(ctx.destination);
      o.start();
      o.stop(ctx.currentTime + 0.45);
    } catch (e) { /* no audio ctx — fine */ }
  }

  setPresenceState(p) {
    // called from WS presence.state events
    if (p.kill_engaged) { this.killEngaged = true; this.stopAll(); }
    if (p.state === "sleeping" || p.state === "silenced") {
      this.stopAll();
      this.stopMic();
      this._stopWakeDetection();
    }
    if (p.state === "sleeping") this._startWakeDetection();
  }

  // ------------------------------------------- wake-word detection (client)
  // Electron has NO SpeechRecognition, so browser-STT wake never worked in
  // the app. Real approach that works everywhere: while sleeping, watch mic
  // energy (our VAD); when speech is detected, capture ~2.5s and run it
  // through the SERVER STT chain (Deepgram->Groq->Local); if the transcript
  // contains "phantom"/"coded" at a word boundary -> wake. STT only runs when
  // you actually speak while it sleeps (cheap, and works offline with local).
  async _startWakeDetection() {
    if (this._wakeActive || this.killEngaged || !this.micEnabled) return;
    this._wakeActive = true;
    this._wakeVadBuf = [];
    this._wakeSpeechFrames = 0;
    this._wakeCooldownUntil = 0;
    // lightweight mic stream for energy detection (shared analyser pattern)
    try {
      if (!this._micStream) await this.startMic();
    } catch (e) { this._wakeActive = false; return; }
    this._wakeEnergyLoop();
  }

  _wakeEnergyLoop() {
    if (!this._wakeActive) return;
    const tick = () => {
      if (!this._wakeActive) return;
      if (this._analyser) {
        const buf = new Float32Array(this._analyser.fftSize);
        this._analyser.getFloatTimeDomainData(buf);
        let sum = 0;
        for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
        const rms = Math.sqrt(sum / buf.length);
        const now = Date.now();
        if (rms > 0.045) {
          this._wakeSpeechFrames += 1;
          if (this._wakeSpeechFrames >= 14 && now >= this._wakeCooldownUntil) {
            this._wakeSpeechFrames = 0;
            this._wakeCooldownUntil = now + 6000;  // cooldown after each attempt
            this._checkWakeByStt();
          }
        } else {
          this._wakeSpeechFrames = 0;
        }
      }
      if (this._wakeActive) this._wakeRaf = requestAnimationFrame(tick);
    };
    this._wakeRaf = requestAnimationFrame(tick);
  }

  async _checkWakeByStt() {
    // capture ~2.5s of audio and check for the wake word via server STT
    try {
      const wav = await this.captureWav(2.5);
      if (!wav) return;
      const fd = new FormData();
      fd.append("audio", wav, "wake.wav");
      fd.append("language", "en");
      const res = await fetch("/api/voice/stt", { method: "POST", body: fd });
      if (!res.ok) return;
      const j = await res.json();
      const text = String(j.text || "").toLowerCase();
      const m = text.match(/(^|\s)(phantom|coded)(\s|$|\.|,|!|\?)/);
      if (m) this._handleWake(m[2].toLowerCase());
    } catch (e) { /* transient — try again next speech */ }
  }

  _stopWakeDetection() {
    this._wakeActive = false;
    if (this._wakeRaf) { cancelAnimationFrame(this._wakeRaf); this._wakeRaf = null; }
    if (this._wakeRec) { try { this._wakeRec.onend = null; this._wakeRec.stop(); } catch (e) {} this._wakeRec = null; }
    this._wakeVadBuf = [];
    this._wakeSpeechFrames = 0;
  }

  async _handleWake(word) {
    if (!this._wakeActive) return;
    this._stopWakeDetection();
    this.onWakeWord?.(word);
  }

  // ------------------------------------------- capture + WAV encode
  async captureWav(seconds = 2) {
    await this.startMic();
    return new Promise((resolve) => {
      const ctx = this._audioCtx;
      if (!ctx) return resolve(null);
      const chunks = [];
      const source = ctx.createMediaStreamSource(this._micStream);
      const rec = ctx.createScriptProcessor(4096, 1, 1);
      const start = ctx.currentTime;
      rec.onaudioprocess = (ev) => {
        if (ctx.currentTime - start >= seconds) {
          source.disconnect(); rec.disconnect();
          const len = chunks.reduce((a, c) => a + c.length, 0);
          const merged = new Float32Array(len);
          let off = 0;
          for (const c of chunks) { merged.set(c, off); off += c.length; }
          resolve(PhantomVoice.encodeWav(merged, ctx.sampleRate));
          return;
        }
        chunks.push(new Float32Array(ev.inputBuffer.getChannelData(0)));
      };
      source.connect(rec);
      rec.connect(ctx.destination);
    });
  }

  static encodeWav(samples, sampleRate) {
    // resample to 16 kHz mono PCM16 for the speaker verifier
    const targetRate = 16000;
    const ratio = sampleRate / targetRate;
    const outLen = Math.floor(samples.length / ratio);
    const out = new Float32Array(outLen);
    for (let i = 0; i < outLen; i++) out[i] = samples[Math.floor(i * ratio)];
    const pcm = new Int16Array(outLen);
    for (let i = 0; i < outLen; i++) {
      const s = Math.max(-1, Math.min(1, out[i]));
      pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    const buf = new ArrayBuffer(44 + pcm.length * 2);
    const dv = new DataView(buf);
    const wstr = (o, s) => { for (let i = 0; i < s.length; i++) dv.setUint8(o + i, s.charCodeAt(i)); };
    wstr(0, "RIFF"); dv.setUint32(4, 36 + pcm.length * 2, true); wstr(8, "WAVE");
    wstr(12, "fmt "); dv.setUint32(16, 16, true); dv.setUint16(20, 1, true);
    dv.setUint16(22, 1, true); dv.setUint32(24, targetRate, true);
    dv.setUint32(28, targetRate * 2, true); dv.setUint16(32, 2, true);
    dv.setUint16(34, 16, true); wstr(36, "data"); dv.setUint32(40, pcm.length * 2, true);
    for (let i = 0; i < pcm.length; i++) dv.setInt16(44 + i * 2, pcm[i], true);
    return new Blob([buf], { type: "audio/wav" });
  }

  async kill() {
    this.killEngaged = true;
    this.stopAll();
    this.stopMic();
    this.setState("IDLE");
  }

  disconnect() {
    this.stopAll();
    this.stopMic();
    this.setState("DISCONNECTED");
  }

  reconnect() {
    this.setState("IDLE");
  }
}

window.PhantomVoice = PhantomVoice;
