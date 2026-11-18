/* Voice engine: continuous listening (VAD), transcription, synthesis and echo guard.
 *
 * STT
 *  - "browser": Web Speech API (Chrome/Edge) in continuous mode, auto-restart.
 *  - "server" : adaptive energy VAD in the browser -> 16 kHz WAV -> /api/stt (Whisper/Deepgram/local).
 * TTS
 *  - /api/tts (ElevenLabs / OpenAI / edge-tts) through an audio analyser that drives the core,
 *    falling back to the browser's speechSynthesis.
 * Echo guard: hardware echoCancellation + transcription suspended while J.A.R.V.I.S. speaks,
 *             resumed automatically as soon as he finishes.
 */
(function () {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const t = (k) => (window.JarvisI18n ? window.JarvisI18n.t(k) : k);
  // Brave exposes the Web Speech API but it always fails with "network" (no Google speech service)
  const IS_BRAVE = !!navigator.brave;

  const WORKLET_SRC = `
    class JarvisCapture extends AudioWorkletProcessor {
      constructor() { super(); this.buf = new Float32Array(2048); this.n = 0; }
      process(inputs) {
        const ch = inputs[0] && inputs[0][0];
        if (ch) {
          for (let i = 0; i < ch.length; i++) {
            this.buf[this.n++] = ch[i];
            if (this.n === this.buf.length) { this.port.postMessage(this.buf.slice(0)); this.n = 0; }
          }
        }
        return true;
      }
    }
    registerProcessor("jarvis-capture", JarvisCapture);`;

  // Partial transcript unchanged for this long = the user has finished the sentence
  const EARLY_COMMIT_MS = 750;

  const VAD = {
    minThreshold: 0.010,   // minimum RMS treated as voice
    floorFactor: 3.2,      // threshold = noise floor * factor
    startFrames: 3,        // ~130 ms above threshold to start
    endSilenceMs: 600,     // silence that ends the utterance
    prerollFrames: 8,      // ~350 ms of audio before the onset
    minSpeechMs: 350,
    maxSpeechMs: 25000,
  };

  function encodeWav(chunks, inRate, outRate = 16000) {
    const total = chunks.reduce((n, c) => n + c.length, 0);
    const input = new Float32Array(total);
    let off = 0;
    for (const c of chunks) { input.set(c, off); off += c.length; }
    const ratio = inRate / outRate;
    const outLen = Math.floor(total / ratio);
    const pcm = new Int16Array(outLen);
    for (let i = 0; i < outLen; i++) {
      const pos = i * ratio, i0 = Math.floor(pos), frac = pos - i0;
      const s = (input[i0] || 0) * (1 - frac) + (input[i0 + 1] || 0) * frac;
      pcm[i] = Math.max(-1, Math.min(1, s)) * 0x7fff;
    }
    const buf = new ArrayBuffer(44 + pcm.byteLength);
    const v = new DataView(buf);
    const w = (o, s) => [...s].forEach((ch, i) => v.setUint8(o + i, ch.charCodeAt(0)));
    w(0, "RIFF"); v.setUint32(4, 36 + pcm.byteLength, true); w(8, "WAVE"); w(12, "fmt ");
    v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
    v.setUint32(24, outRate, true); v.setUint32(28, outRate * 2, true); v.setUint16(32, 2, true);
    v.setUint16(34, 16, true); w(36, "data"); v.setUint32(40, pcm.byteLength, true);
    new Int16Array(buf, 44).set(pcm);
    return new Blob([buf], { type: "audio/wav" });
  }

  class VoiceEngine extends EventTarget {
    constructor({ lang, sttServer, sttFallback, ttsChain, apiFetch, forceServerStt, bargeIn }) {
      super();
      this.lang = lang || "en-GB";
      this.apiFetch = apiFetch;
      this.ttsChain = ttsChain || [];
      // bargeIn: keep transcribing while J.A.R.V.I.S. talks, so the user can interrupt him
      this.bargeIn = !!bargeIn;
      this.sttFallback = sttFallback || null;   // server-side STT usable when the browser's fails
      const browserOk = SR && !forceServerStt && !IS_BRAVE;
      const server = sttServer || (!browserOk ? this.sttFallback : null);
      this.sttMode = server ? "server" : browserOk ? "browser" : "none";
      this.sttLabel = server ? `${server.toUpperCase()} (server)` : browserOk ? "WEB SPEECH" : "UNAVAILABLE";
      this.active = false;      // microphone on (not muted)
      this.suspended = false;   // echo-guard pause while speaking
      this.speaking = false;
      this.ttsProvider = "—";
      this.ctx = null;
      this.fakeLevel = 0;
      this._speakId = 0;
      this._queue = []; this._draining = false; this._turn = null; this._turnDone = true;
      this._rec = null; this._recRunning = false; this._recBackoff = 300;
      this._shownErrors = new Set();
      // Watchdog: if recognition silently stopped while we should be listening, start it again
      setInterval(() => {
        if (this.sttMode === "browser" && this.listening && !this._recRunning) this._startRec();
      }, 4000);
      this._vad = { speaking: false, above: 0, silenceMs: 0, chunks: [], preroll: [], floor: 0.004, speechMs: 0 };
    }

    emit(type, detail) { this.dispatchEvent(new CustomEvent(type, { detail })); }

    /* Call inside a user gesture (unlocks AudioContext/autoplay). */
    async init() {
      if (this.ctx) return;
      this.ctx = new (window.AudioContext || window.webkitAudioContext)();
      await this.ctx.resume();
      this.outAnalyser = this.ctx.createAnalyser();
      this.outAnalyser.fftSize = 256;
      this.outAnalyser.smoothingTimeConstant = 0.6;
      this.outAnalyser.connect(this.ctx.destination);
      this._spec = new Uint8Array(this.outAnalyser.frequencyBinCount);
      this._td = new Float32Array(256);
      if (window.speechSynthesis) speechSynthesis.getVoices();
    }

    // ------------------------------------------------------------ microphone
    async start() {
      await this.init();
      if (this.active) return true;
      if (this.sttMode === "none") {
        this.emit("error", t(IS_BRAVE ? "brave_unsupported" : "stt_unavailable"));
        return false;
      }
      try {
        this.stream = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
        });
      } catch (err) {
        const insecure = !window.isSecureContext ? t("need_https") : "";
        this.emit("error", `${t("mic_denied")}${insecure}: ${err.message || err.name}`);
        return false;
      }
      this.src = this.ctx.createMediaStreamSource(this.stream);
      this.micAnalyser = this.ctx.createAnalyser();
      this.micAnalyser.fftSize = 256;
      this.micAnalyser.smoothingTimeConstant = 0.5;
      this.src.connect(this.micAnalyser);
      this._micSpec = new Uint8Array(this.micAnalyser.frequencyBinCount);

      if (this.sttMode === "server") await this._startCapture();
      this.active = true;
      this._syncRecognition();
      this._emitMic();
      return true;
    }

    stop() {
      this.active = false;
      this._syncRecognition();
      this._resetVad();
      if (this.worklet) { this.worklet.port.onmessage = null; this.worklet.disconnect(); this.worklet = null; }
      if (this.src) { this.src.disconnect(); this.src = null; }
      if (this.stream) { this.stream.getTracks().forEach((t) => t.stop()); this.stream = null; }
      this.micAnalyser = null;
      this.emit("interim", "");
      this._emitMic();
    }

    async toggle() { return this.active ? (this.stop(), false) : this.start(); }

    /* Switch recognition language (e.g. en-GB <-> it-IT) without muting the microphone. */
    setLang(locale) {
      if (!locale || locale === this.lang) return;
      this.lang = locale;
      if (this._rec) {
        const old = this._rec;
        this._rec = null;
        old.onend = null;
        this._recRunning = false;
        try { old.abort(); } catch { /* ignore */ }
      }
      this._syncRecognition();
    }

    suspend() { this.suspended = true; clearTimeout(this._commitTimer); this._resetVad(); this._syncRecognition(); this.emit("interim", ""); this._emitMic(); }
    resume() { this.suspended = false; this._syncRecognition(); this._emitMic(); }

    get listening() { return this.active && !this.suspended; }
    _emitMic() { this.emit("mic", this.active ? (this.suspended ? "paused" : "listening") : "muted"); }

    // ------------------------------------------------------------ STT browser
    _syncRecognition() {
      if (this.sttMode !== "browser") return;
      if (this.listening) this._startRec(); else this._stopRec();
    }

    _startRec() {
      if (this._recRunning) return;
      if (!this._rec) {
        const rec = new SR();
        rec.lang = this.lang;
        rec.continuous = true;
        rec.interimResults = true;
        rec.maxAlternatives = 1;
        rec.onresult = (e) => {
          this._shownErrors.clear();
          if (!this.listening) return;
          let interim = "";
          for (let i = e.resultIndex; i < e.results.length; i++) {
            const r = e.results[i];
            if (r.isFinal) {
              clearTimeout(this._commitTimer);
              const text = r[0].transcript.trim();
              if (text) this.emit("final", text);
            } else interim += r[0].transcript;
          }
          this.emit("interim", interim.trim());
          this._armEarlyCommit(interim.trim());
        };
        rec.onerror = (e) => {
          if (e.error === "no-speech" || e.error === "aborted") return;
          if (e.error === "not-allowed" || e.error === "service-not-allowed") {
            this.emit("error", t("rec_blocked"));
            this.stop();
            return;
          }
          if (e.error === "network" && this.sttFallback) { this._switchToServer(); return; }
          if (e.error === "network") this._recBackoff = Math.min(this._recBackoff * 2, 8000);
          // Tell the user once per error type, otherwise the mic looks "on" but nothing happens
          const key = e.error === "network" ? "rec_network" : e.error === "audio-capture" ? "rec_audio" : null;
          if (key && !this._shownErrors.has(key)) { this._shownErrors.add(key); this.emit("error", t(key)); }
          this.emit("warn", `Speech recognition: ${e.error}`);
        };
        rec.onend = () => {
          this._recRunning = false;
          clearTimeout(this._commitTimer);
          if (this.listening) setTimeout(() => this._startRec(), this._recBackoff);
        };
        rec.onstart = () => { this._recBackoff = 300; };
        this._rec = rec;
      }
      try { this._rec.start(); this._recRunning = true; } catch { /* already started */ }
    }

    /* Browsers wait ~1 s of silence before declaring a phrase final. If the partial transcript has not
       changed for EARLY_COMMIT_MS the user has stopped talking: send it now and restart recognition,
       which discards the late "final" duplicate. */
    _armEarlyCommit(text) {
      clearTimeout(this._commitTimer);
      if (!text) return;
      this._commitTimer = setTimeout(() => {
        if (!this.listening || this.sttMode !== "browser") return;
        this.emit("final", text);
        this.emit("interim", "");
        if (this._rec) { try { this._rec.abort(); } catch { /* ignore */ } } // onend restarts it at once
      }, EARLY_COMMIT_MS);
    }

    /* The browser's recognition service is unreachable: switch to server-side STT (e.g. local Whisper). */
    async _switchToServer() {
      if (this.sttMode === "server") return;
      this.sttMode = "server";
      this.sttLabel = `${this.sttFallback.toUpperCase()} (server)`;
      if (this._rec) { this._rec.onend = null; try { this._rec.abort(); } catch { /* ignore */ } this._rec = null; }
      this._recRunning = false;
      if (this.active && this.src && !this.worklet) await this._startCapture();
      this.emit("mode", this.sttLabel);
    }

    _stopRec() {
      if (this._rec && this._recRunning) { try { this._rec.abort(); } catch { /* ignore */ } }
    }

    // ------------------------------------------------------------ STT server (VAD)
    async _startCapture() {
      if (!this._workletLoaded) {
        const url = URL.createObjectURL(new Blob([WORKLET_SRC], { type: "application/javascript" }));
        await this.ctx.audioWorklet.addModule(url);
        this._workletLoaded = true;
      }
      this.worklet = new AudioWorkletNode(this.ctx, "jarvis-capture");
      const sink = this.ctx.createGain();
      sink.gain.value = 0;
      this.src.connect(this.worklet);
      this.worklet.connect(sink).connect(this.ctx.destination);
      this.worklet.port.onmessage = (e) => this._onFrame(e.data);
    }

    _resetVad() {
      Object.assign(this._vad, { speaking: false, above: 0, silenceMs: 0, chunks: [], preroll: [], speechMs: 0 });
    }

    _onFrame(frame) {
      if (!this.listening) return;
      const v = this._vad;
      const frameMs = (frame.length / this.ctx.sampleRate) * 1000;
      let sum = 0;
      for (let i = 0; i < frame.length; i++) sum += frame[i] * frame[i];
      const rms = Math.sqrt(sum / frame.length);
      const thr = Math.max(VAD.minThreshold, v.floor * VAD.floorFactor);
      const loud = rms > thr;

      if (!v.speaking) {
        v.floor = v.floor * 0.95 + Math.min(rms, 0.05) * 0.05;   // adapt to background noise
        v.preroll.push(frame);
        if (v.preroll.length > VAD.prerollFrames) v.preroll.shift();
        v.above = loud ? v.above + 1 : 0;
        if (v.above >= VAD.startFrames) {
          v.speaking = true;
          v.chunks = [...v.preroll];
          v.silenceMs = 0;
          v.speechMs = v.chunks.length * frameMs;
          this.emit("interim", "…");
        }
        return;
      }
      v.chunks.push(frame);
      v.speechMs += frameMs;
      v.silenceMs = loud ? 0 : v.silenceMs + frameMs;
      if (v.silenceMs >= VAD.endSilenceMs || v.speechMs >= VAD.maxSpeechMs) {
        const chunks = v.chunks, voiced = v.speechMs - v.silenceMs;
        this._resetVad();
        this.emit("interim", "");
        if (voiced >= VAD.minSpeechMs) this._transcribe(encodeWav(chunks, this.ctx.sampleRate));
      }
    }

    async _transcribe(wav) {
      this.emit("interim", t("transcribing"));
      try {
        const r = await this.apiFetch("/api/stt", { method: "POST", body: wav, headers: { "Content-Type": "audio/wav" } });
        const data = await r.json();
        this.emit("interim", "");
        if (!r.ok) throw new Error(data.error || r.status);
        if (data.text && data.text.trim()) this.emit("final", data.text.trim());
      } catch (err) {
        this.emit("interim", "");
        this.emit("warn", `STT failed: ${err.message || err}`);
      }
    }

    // ------------------------------------------------------------ TTS (sentence queue)
    /* Replies arrive sentence by sentence while Claude is still writing. Each sentence is sent to the
       TTS server immediately (in parallel) and played in order, so J.A.R.V.I.S. starts talking as soon
       as the first sentence is ready. The microphone stays paused until the whole turn has been said. */
    enqueue(text, turn) {
      if (!text || turn === this._cancelledTurn) return;   // the user interrupted this answer
      if (turn !== this._turn) {            // a new answer: drop whatever was still playing
        this.stopSpeaking(true);
        this._turn = turn;
        this._turnDone = false;
      }
      this._queue.push({ text, audio: this._fetchTts(text) });
      if (!this.speaking) {
        this.speaking = true;
        if (!this.bargeIn) this.suspend();   // without barge-in the mic pauses so he can't hear himself
        this.emit("speak", true);
      }
      if (!this._draining) this._drain();
    }

    /* The server finished the answer: once the queue is empty the microphone comes back. */
    endTurn(turn) {
      if (turn !== this._turn) return;
      this._turnDone = true;
      if (!this._draining) this._finish();
    }

    /* Speak a whole text at once (used for local messages). */
    speak(text) {
      const turn = `local-${Date.now()}`;
      this.enqueue(text, turn);
      this.endTurn(turn);
    }

    async _fetchTts(text) {
      if (!this.ttsChain.length) return null;
      try {
        const r = await this.apiFetch("/api/tts", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }),
        });
        if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.status);
        this.ttsProvider = (r.headers.get("X-TTS-Provider") || "server").toUpperCase();
        return await r.blob();
      } catch (err) {
        this.emit("warn", `Server TTS unavailable (${err.message || err}), using the browser voice.`);
        return null;
      }
    }

    async _drain() {
      await this.init();
      this._draining = true;
      const id = this._speakId;
      while (this._queue.length && id === this._speakId) {
        const item = this._queue.shift();
        const blob = await item.audio;
        if (id !== this._speakId) break;
        if (blob) {
          await this._playBlob(blob);
        } else {
          this.ttsProvider = "BROWSER";
          await this._speakBrowser(item.text);
        }
      }
      this._draining = false;
      if (id === this._speakId && this._turnDone) this._finish();
    }

    _finish() {
      if (!this.speaking) return;
      this.speaking = false;
      this.emit("speak", false);
      const id = this._speakId;
      setTimeout(() => { if (id === this._speakId && !this.speaking) this.resume(); }, 350);
    }

    _playBlob(blob) {
      return new Promise((resolve) => {
        const url = URL.createObjectURL(blob);
        const audio = new Audio(url);
        this._audio = audio;
        const node = this.ctx.createMediaElementSource(audio);
        node.connect(this.outAnalyser);
        let done = false;
        const end = () => {
          if (done) return;
          done = true;
          node.disconnect(); URL.revokeObjectURL(url); this._audio = null; this._endPlayback = null; resolve();
        };
        this._endPlayback = end;
        audio.onended = end;
        audio.onerror = end;
        audio.play().catch(end);
      });
    }

    _pickVoice() {
      const voices = speechSynthesis.getVoices();
      const short = this.lang.slice(0, 2);
      const same = voices.filter((v) => v.lang && v.lang.toLowerCase().startsWith(short));
      const pref = ["ryan", "thomas", "george", "daniel", "arthur", "uk english male", "diego", "henri", "remy", "paul", "male"];
      for (const p of pref) {
        const hit = same.find((v) => v.name.toLowerCase().includes(p));
        if (hit) return hit;
      }
      return same[0] || null;
    }

    _speakBrowser(text) {
      return new Promise((resolve) => {
        if (!window.speechSynthesis) return resolve();
        const u = new SpeechSynthesisUtterance(text.replace(/[*_#`]/g, ""));
        u.lang = this.lang;
        const v = this._pickVoice();
        if (v) u.voice = v;
        u.rate = 1.04;
        u.pitch = 0.9;
        u.onboundary = () => { this.fakeLevel = 0.55 + Math.random() * 0.4; };
        const end = () => { this._endPlayback = null; resolve(); };
        u.onend = end;
        u.onerror = end;
        this._endPlayback = () => { speechSynthesis.cancel(); end(); };
        speechSynthesis.speak(u);
      });
    }

    /* Short two-tone "I'm listening" sound, played when the wake word is heard. */
    chime() {
      if (!this.ctx) return;
      const now = this.ctx.currentTime;
      [[880, 0], [1320, 0.09]].forEach(([freq, delay]) => {
        const osc = this.ctx.createOscillator();
        const gain = this.ctx.createGain();
        osc.type = "sine";
        osc.frequency.value = freq;
        gain.gain.setValueAtTime(0.0001, now + delay);
        gain.gain.exponentialRampToValueAtTime(0.12, now + delay + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, now + delay + 0.16);
        osc.connect(gain).connect(this.ctx.destination);
        osc.start(now + delay);
        osc.stop(now + delay + 0.18);
      });
    }

    /* Soft descending "done" sound: a command was executed without any spoken answer. */
    doneTone() {
      if (!this.ctx) return;
      const now = this.ctx.currentTime;
      [[1046, 0], [784, 0.08]].forEach(([freq, delay]) => {
        const osc = this.ctx.createOscillator();
        const gain = this.ctx.createGain();
        osc.type = "sine";
        osc.frequency.value = freq;
        gain.gain.setValueAtTime(0.0001, now + delay);
        gain.gain.exponentialRampToValueAtTime(0.08, now + delay + 0.015);
        gain.gain.exponentialRampToValueAtTime(0.0001, now + delay + 0.14);
        osc.connect(gain).connect(this.ctx.destination);
        osc.start(now + delay);
        osc.stop(now + delay + 0.16);
      });
    }

    /* Stop talking now. `silent` = a new answer replaces this one, so do not resume the mic yet. */
    stopSpeaking(silent = false) {
      if (!silent) this._cancelledTurn = this._turn;   // ignore the rest of this answer if it is still arriving
      this._speakId++;
      this._queue = [];
      this._draining = false;
      if (this._audio) { this._audio.pause(); }
      if (this._endPlayback) this._endPlayback();
      if (window.speechSynthesis) speechSynthesis.cancel();
      if (this.speaking && !silent) {
        this.speaking = false;
        this.emit("speak", false);
        setTimeout(() => { if (!this.speaking) this.resume(); }, 300);
      }
    }

    // ------------------------------------------------------------ levels for the core
    levels() {
      if (!this.ctx) return { level: 0, spectrum: null };
      if (this.speaking) {
        if (this._audio) {
          this.outAnalyser.getFloatTimeDomainData(this._td);
          this.outAnalyser.getByteFrequencyData(this._spec);
          return { level: Math.min(1, this._rms(this._td) * 5), spectrum: this._spec };
        }
        this.fakeLevel *= 0.92;  // browser voice: level simulated from word events
        return { level: 0.25 + this.fakeLevel * 0.6, spectrum: null };
      }
      if (this.listening && this.micAnalyser) {
        this.micAnalyser.getFloatTimeDomainData(this._td);
        this.micAnalyser.getByteFrequencyData(this._micSpec);
        return { level: Math.min(1, this._rms(this._td) * 7), spectrum: this._micSpec };
      }
      return { level: 0, spectrum: null };
    }

    _rms(arr) {
      let s = 0;
      for (let i = 0; i < arr.length; i++) s += arr[i] * arr[i];
      return Math.sqrt(s / arr.length);
    }
  }

  window.VoiceEngine = VoiceEngine;
})();
