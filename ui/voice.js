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
    this.sttProvider = "browser";    // browser | deepgram
    this.ttsProvider = "browser";    // browser | deepgram
    this.ttsVoice = "";
    this.proactiveSpeech = false;
    this.deepgramConfigured = false;
    this.muted = false;              // master mute (no TTS)
    this.micEnabled = true;
    this.killEngaged = false;

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
  }

  // ---------------------------------------------------------------- config
  async init() {
    try {
      const res = await fetch("/api/voice/config");
      const cfg = await res.json();
      this.mode = cfg.mode || "conversation";
      this.sttProvider = cfg.stt?.provider || "browser";
      this.ttsProvider = cfg.tts?.provider || "browser";
      this.ttsVoice = cfg.tts?.voice || "";
      this.proactiveSpeech = !!cfg.proactive_speech;
      this.deepgramConfigured = !!cfg.deepgram_configured;
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

  _vadLoop() {
    const tick = () => {
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
    const SPEECH = 0.035, SILENCE = 0.014;
    if (level > SPEECH) {
      this._vadBuf.push(Date.now());
      if (this._vadBuf.length > 30) this._vadBuf.shift();
      // barge-in: user speaks while we speak → stop us immediately
      if (this.state === "SPEAKING" && this.micEnabled) {
        this.bargeIn();
      }
    } else if (level < SILENCE && this._vadBuf.length) {
      this._vadBuf = [];
      // end-of-turn while listening and idle (no agent running) → send final
      if (this.state === "LISTENING" && this._endTurnTimer === null) {
        this._endTurnTimer = setTimeout(() => {
          this._endTurnTimer = null;
        }, 0); // end-of-turn is handled by the STT engine's onend/final
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
    } else {
      this._startBrowserSTT();
    }
    this.setState("LISTENING");
  }

  stopListening() {
    this._stopBrowserSTT();
    this._closeDeepgramListen();
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
  speak(text, opts = {}) {
    if (this.muted || this.mode === "private" || this.killEngaged) return;
    const clean = String(text || "").replace(/[#*`_>]/g, "").trim();
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
    if (this.ttsProvider === "deepgram" && this.deepgramConfigured) {
      this._speakDeepgram(item.text);
    } else {
      this._speakBrowser(item.text);
    }
  }

  _speakBrowser(text) {
    const synth = window.speechSynthesis;
    if (!synth) {
      this.onError?.("speech synthesis not supported");
      this._speaking = false;
      this.setState(this.mode === "conversation" ? "LISTENING" : "IDLE");
      return;
    }
    synth.cancel();
    const u = new SpeechSynthesisUtterance(text);
    u.rate = 1.04;
    if (this.ttsVoice) {
      const v = synth.getVoices().find((x) => x.name === this.ttsVoice || x.lang === this.ttsVoice);
      if (v) u.voice = v;
    }
    u.onend = () => this._speakDone();
    u.onerror = () => this._speakDone();
    this._utterance = u;
    synth.speak(u);
  }

  async _speakDeepgram(text) {
    try {
      const res = await fetch("/api/voice/deepgram-token", { method: "POST" });
      const { token } = await res.json();
      const ws = new WebSocket(
        "wss://api.deepgram.com/v1/speak?model=aura-asteria-en",
        ["token", token],
      );
      this._dgSpeakWs = ws;
      ws.onopen = () => ws.send(JSON.stringify({ type: "Speak", text }));
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
      src.connect(this._audioCtx.destination);
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
    if (window.speechSynthesis) window.speechSynthesis.cancel();
    if (this._dgSpeakWs) { try { this._dgSpeakWs.close(); } catch (e) {} this._dgSpeakWs = null; }
    this._dgAudioQueue = [];
    this._dgPlaying = false;
    this._speaking = false;
    this._utterance = null;
    this.onSpeakEnd?.();
  }

  // ---------------------------------------------------------------- controls
  bargeIn() {
    if (this.state !== "SPEAKING") return;
    this.stopSpeaking();
    this.setState("INTERRUPTED");
    clearTimeout(this._interruptTimer);
    this._interruptTimer = setTimeout(() => {
      if (this.mode === "conversation" && this.micEnabled) {
        this.startListening();
      } else if (this.state === "INTERRUPTED") {
        this.setState("IDLE");
      }
    }, 300);
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
