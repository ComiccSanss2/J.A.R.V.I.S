/* Reactive holographic core (2D canvas).
   States: offline | muted | listening | thinking | speaking
   setLevel(0..1): audio level (microphone while listening, JARVIS's voice while speaking)
   setSpectrum(Uint8Array): audio spectrum for the radial spikes                      */
(function () {
  const PALETTE = {
    listening: [0, 240, 255],
    standby: [0, 170, 205],
    thinking: [120, 220, 255],
    speaking: [0, 240, 255],
    muted: [255, 153, 0],
    offline: [255, 77, 94],
  };

  class Core {
    constructor(canvas) {
      this.c = canvas;
      this.ctx = canvas.getContext("2d");
      this.state = "offline";
      this.level = 0;          // smoothed level
      this.target = 0;         // incoming level
      this.spectrum = null;
      this.t = 0;
      this.color = [...PALETTE.offline];
      this.resize = this.resize.bind(this);
      new ResizeObserver(this.resize).observe(canvas);
      this.resize();
      requestAnimationFrame((ts) => this.frame(ts));
    }
    resize() {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const r = this.c.getBoundingClientRect();
      this.c.width = Math.max(1, r.width * dpr);
      this.c.height = Math.max(1, r.height * dpr);
      this.dpr = dpr;
    }
    setState(s) { this.state = s; }
    setLevel(v) { this.target = Math.max(0, Math.min(1, v)); }
    setSpectrum(arr) { this.spectrum = arr; }

    rgba(a, mul = 1) {
      const [r, g, b] = this.color;
      return `rgba(${Math.min(255, r * mul) | 0},${Math.min(255, g * mul) | 0},${Math.min(255, b * mul) | 0},${a})`;
    }

    frame(ts) {
      const dt = Math.min(0.05, (ts - (this.last || ts)) / 1000);
      this.last = ts;
      this.t += dt;
      // colour transition
      const goal = PALETTE[this.state] || PALETTE.listening;
      for (let i = 0; i < 3; i++) this.color[i] += (goal[i] - this.color[i]) * Math.min(1, dt * 5);
      // level: fast attack, slow release
      const k = this.target > this.level ? 18 : 5;
      this.level += (this.target - this.level) * Math.min(1, dt * k);
      this.draw();
      requestAnimationFrame((t2) => this.frame(t2));
    }

    draw() {
      const { ctx } = this;
      const W = this.c.width, H = this.c.height;
      const cx = W / 2, cy = H / 2, R = Math.min(W, H) / 2 * 0.92;
      const t = this.t, st = this.state, lv = this.level;
      const dim = st === "muted" || st === "offline" ? 0.45 : st === "standby" ? 0.7 : 1;
      const speed = st === "thinking" ? 2.6 : st === "speaking" ? 1.2 : st === "muted" ? 0.25 : st === "standby" ? 0.35 : 0.6;
      ctx.clearRect(0, 0, W, H);
      ctx.lineCap = "round";

      // background halo
      const halo = ctx.createRadialGradient(cx, cy, R * 0.1, cx, cy, R);
      halo.addColorStop(0, this.rgba(0.18 * dim + lv * 0.25));
      halo.addColorStop(0.6, this.rgba(0.04 * dim));
      halo.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = halo;
      ctx.fillRect(0, 0, W, H);

      // 1) outer tick ring (reacts to level)
      const ticks = 96;
      for (let i = 0; i < ticks; i++) {
        const a = (i / ticks) * Math.PI * 2 + t * 0.08 * speed;
        let amp = 0;
        if (this.spectrum && (st === "speaking" || st === "listening")) {
          const idx = Math.floor((Math.abs(((i / ticks) * 2) - 1)) * (this.spectrum.length * 0.6));
          amp = (this.spectrum[idx] || 0) / 255;
        }
        const major = i % 8 === 0;
        const len = R * (major ? 0.05 : 0.025) + R * 0.11 * Math.max(amp * lv * 1.6, 0);
        const r0 = R * 0.9;
        ctx.strokeStyle = this.rgba((major ? 0.9 : 0.45) * dim);
        ctx.lineWidth = (major ? 2.2 : 1.2) * this.dpr;
        ctx.beginPath();
        ctx.moveTo(cx + Math.cos(a) * r0, cy + Math.sin(a) * r0);
        ctx.lineTo(cx + Math.cos(a) * (r0 - len), cy + Math.sin(a) * (r0 - len));
        ctx.stroke();
      }

      // 2) counter-rotating segmented arcs
      const segRing = (radius, count, gapFrac, rot, width, alpha) => {
        ctx.lineWidth = width * this.dpr;
        ctx.strokeStyle = this.rgba(alpha * dim);
        const seg = (Math.PI * 2) / count;
        for (let i = 0; i < count; i++) {
          const s = i * seg + rot;
          ctx.beginPath();
          ctx.arc(cx, cy, radius, s, s + seg * (1 - gapFrac));
          ctx.stroke();
        }
      };
      segRing(R * 0.76, 3, 0.18, -t * 0.35 * speed, 3, 0.75);
      segRing(R * 0.70, 24, 0.45, t * 0.22 * speed, 6, 0.28);
      segRing(R * 0.63, 6, 0.55, -t * 0.6 * speed, 1.5, 0.9);

      // 3) processing comets
      if (st === "thinking") {
        for (let j = 0; j < 3; j++) {
          const a0 = t * (3 + j) + (j * Math.PI * 2) / 3;
          const g = ctx.createConicGradient ? ctx.createConicGradient(a0 - 1.2, cx, cy) : null;
          ctx.lineWidth = 4 * this.dpr;
          if (g) {
            g.addColorStop(0, "rgba(0,0,0,0)");
            g.addColorStop(0.19, this.rgba(0.95, 1.3));
            g.addColorStop(0.2, "rgba(0,0,0,0)");
            ctx.strokeStyle = g;
          } else ctx.strokeStyle = this.rgba(0.9);
          ctx.beginPath();
          ctx.arc(cx, cy, R * (0.55 - j * 0.04), a0 - 1.2, a0);
          ctx.stroke();
        }
      }

      // 4) pulsing inner ring
      const pulse = st === "thinking" ? 0.5 + 0.5 * Math.sin(t * 6) : 0;
      ctx.lineWidth = (2 + lv * 4) * this.dpr;
      ctx.strokeStyle = this.rgba((0.55 + lv * 0.45 + pulse * 0.3) * dim, 1.2);
      ctx.shadowColor = this.rgba(0.9);
      ctx.shadowBlur = (10 + lv * 40 + pulse * 20) * this.dpr * dim;
      ctx.beginPath();
      ctx.arc(cx, cy, R * (0.44 + lv * 0.05), 0, Math.PI * 2);
      ctx.stroke();

      // 5) central energy sphere
      const breathe = st === "listening" || st === "muted" || st === "standby" ? Math.sin(t * 1.6) * 0.015 : 0;
      const coreR = R * (0.25 + lv * 0.11 + pulse * 0.03 + breathe);
      const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, coreR);
      g.addColorStop(0, `rgba(255,255,255,${0.95 * dim})`);
      g.addColorStop(0.28, this.rgba(0.9 * dim, 1.15));
      g.addColorStop(0.75, this.rgba(0.25 * dim));
      g.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = g;
      ctx.shadowBlur = (30 + lv * 60) * this.dpr * dim;
      ctx.beginPath();
      ctx.arc(cx, cy, coreR, 0, Math.PI * 2);
      ctx.fill();
      ctx.shadowBlur = 0;

      // 6) radial wave while speaking
      if (st === "speaking" && this.spectrum) {
        ctx.beginPath();
        const n = 64, base = R * 0.34;
        for (let i = 0; i <= n; i++) {
          const a = (i / n) * Math.PI * 2;
          const idx = Math.floor((Math.abs((i / n) * 2 - 1)) * (this.spectrum.length * 0.5));
          const amp = (this.spectrum[idx] || 0) / 255;
          const r = base + amp * R * 0.13;
          const x = cx + Math.cos(a) * r, y = cy + Math.sin(a) * r;
          i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
        }
        ctx.strokeStyle = this.rgba(0.8, 1.3);
        ctx.lineWidth = 1.5 * this.dpr;
        ctx.stroke();
      }

      // 7) crosshair
      ctx.strokeStyle = this.rgba(0.35 * dim);
      ctx.lineWidth = 1 * this.dpr;
      for (const a of [0, Math.PI / 2, Math.PI, Math.PI * 1.5]) {
        ctx.beginPath();
        ctx.moveTo(cx + Math.cos(a) * R * 0.95, cy + Math.sin(a) * R * 0.95);
        ctx.lineTo(cx + Math.cos(a) * R * 1.0, cy + Math.sin(a) * R * 1.0);
        ctx.stroke();
      }
    }
  }

  window.JarvisCore = Core;
})();
