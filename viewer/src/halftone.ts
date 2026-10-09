// Dot-screen rendering for anything photographic (docs/STYLE.md, Halftone): monochrome, 4..6 px
// cells, dot area proportional to luminance, ink dots on --bg. The video is sampled at 320 px wide
// and redrawn at 15 fps; the placeholder is a static procedural field drawn once per resize.

const SAMPLE_W = 320, FPS = 15;
const css = (name: string, fallback: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;

export class Halftone {
  private ctx: CanvasRenderingContext2D;
  private sample = document.createElement('canvas');
  private sctx = this.sample.getContext('2d', { willReadFrequently: true })!;
  private video: HTMLVideoElement | null = null;
  private raf = 0;
  private last = 0;
  private lo = 0.05; private hi = 0.9; // smoothed auto-levels
  private ink = css('--ink', '#e9ecef');
  private bg = css('--bg', '#070809');

  constructor(private canvas: HTMLCanvasElement) {
    this.ctx = canvas.getContext('2d')!;
    new ResizeObserver(() => { this.fit(); if (!this.video) this.drawStatic(); }).observe(canvas);
  }

  /** Halftone the live video until stop() or still(). */
  play(video: HTMLVideoElement) {
    this.video = video;
    cancelAnimationFrame(this.raf);
    const tick = (t: number) => {
      this.raf = requestAnimationFrame(tick);
      if (t - this.last < 1000 / FPS - 2) return;
      this.last = t;
      this.drawVideo();
    };
    this.raf = requestAnimationFrame(tick);
  }

  /** Static placeholder field (camera refused or missing). */
  still() { this.stop(); this.drawStatic(); }

  stop() { cancelAnimationFrame(this.raf); this.raf = 0; this.video = null; }

  private fit(): boolean {
    const dpr = Math.min(devicePixelRatio || 1, 2);
    const w = Math.round(this.canvas.clientWidth * dpr), h = Math.round(this.canvas.clientHeight * dpr);
    if (!w || !h) return false;
    if (this.canvas.width !== w || this.canvas.height !== h) { this.canvas.width = w; this.canvas.height = h; }
    return true;
  }

  /** Cell size in CSS px: 4 on small panels, up to 6 on large ones (bounds the dot count). */
  private cell(): number {
    const w = this.canvas.clientWidth;
    return Math.max(4, Math.min(6, w / 120));
  }

  /**
   * Draw the dot screen. `lum(x, y)` returns luminance 0..1 for a point in CSS px of the panel.
   * Rows are offset by half a cell (a hexagonal screen reads closer to print than a square one).
   */
  private screen(lum: (x: number, y: number) => number) {
    if (!this.fit()) return;
    const ctx = this.ctx, dpr = this.canvas.width / Math.max(1, this.canvas.clientWidth);
    const W = this.canvas.clientWidth, H = this.canvas.clientHeight, c = this.cell(), rowH = c * 0.866;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = this.bg; ctx.fillRect(0, 0, W, H);
    ctx.fillStyle = this.ink;
    ctx.beginPath();
    const rMax = c * 0.46; // dots never merge: the texture stays a screen
    for (let j = 0, y = rowH / 2; y < H + rowH; j++, y += rowH) {
      for (let x = (j & 1 ? c : c / 2); x < W + c; x += c) {
        const L = lum(x, y);
        if (L <= 0.015) continue;
        const r = rMax * Math.sqrt(L);
        if (r < 0.35) continue;
        ctx.moveTo(x + r, y); ctx.arc(x, y, r, 0, Math.PI * 2);
      }
    }
    ctx.fill();
  }

  private drawVideo() {
    const v = this.video;
    if (!v || v.readyState < 2 || !v.videoWidth) return;
    const sw = SAMPLE_W, sh = Math.max(1, Math.round((SAMPLE_W * v.videoHeight) / v.videoWidth));
    if (this.sample.width !== sw || this.sample.height !== sh) { this.sample.width = sw; this.sample.height = sh; }
    this.sctx.drawImage(v, 0, 0, sw, sh);
    const px = this.sctx.getImageData(0, 0, sw, sh).data;
    const Y = new Float32Array(sw * sh), hist = new Uint32Array(64);
    for (let i = 0, k = 0; i < Y.length; i++, k += 4) {
      const l = (0.2126 * px[k] + 0.7152 * px[k + 1] + 0.0722 * px[k + 2]) / 255;
      Y[i] = l; hist[Math.min(63, (l * 64) | 0)]++;
    }
    // Auto-levels (2nd..98th percentile), smoothed so the screen does not pump.
    const pct = (q: number) => { let acc = 0; const n = Y.length * q; for (let b = 0; b < 64; b++) { acc += hist[b]; if (acc >= n) return b / 63; } return 1; };
    this.lo += (pct(0.02) - this.lo) * 0.2; this.hi += (Math.max(this.lo + 0.15, pct(0.98)) - this.hi) * 0.2;
    const lo = this.lo, span = Math.max(0.1, this.hi - this.lo);
    // object-fit: cover mapping from panel CSS px to sample px.
    const W = this.canvas.clientWidth, H = this.canvas.clientHeight;
    const s = Math.max(W / sw, H / sh), ox = (W - sw * s) / 2, oy = (H - sh * s) / 2;
    this.screen((x, y) => {
      const u = Math.min(sw - 2, Math.max(0, (x - ox) / s)), w = Math.min(sh - 2, Math.max(0, (y - oy) / s));
      const i = (w | 0) * sw + (u | 0);
      const l = (Y[i] + Y[i + 1] + Y[i + sw] + Y[i + sw + 1]) / 4;
      const n = Math.min(1, Math.max(0, (l - lo) / span));
      return 0.9 * Math.pow(n, 1.4); // darker mids: dots, not a wall of white
    });
  }

  /** A lit bust-like blob (head + shoulders) on black: the stand-in "photograph". */
  private drawStatic() {
    const W = this.canvas.clientWidth, H = this.canvas.clientHeight;
    if (!W || !H) return;
    const m = Math.min(W, H), cx = W / 2, cy = H * 0.52;
    const L = [-0.55, -0.62, 0.56]; const ln = Math.hypot(...L); const lx = L[0] / ln, ly = L[1] / ln, lz = L[2] / ln;
    const blobs = [
      { x: cx, y: cy - m * 0.12, rx: m * 0.17, ry: m * 0.21 },     // head
      { x: cx, y: cy + m * 0.5, rx: m * 0.46, ry: m * 0.36 },      // shoulders
      { x: cx, y: cy + m * 0.12, rx: m * 0.08, ry: m * 0.12 },     // neck
    ];
    this.screen((x, y) => {
      let best = -1, nx = 0, ny = 0, nz = 0;
      for (const b of blobs) {
        const dx = (x - b.x) / b.rx, dy = (y - b.y) / b.ry, d2 = dx * dx + dy * dy;
        if (d2 >= 1) continue;
        const z = Math.sqrt(1 - d2);
        if (z * Math.min(b.rx, b.ry) > best) { best = z * Math.min(b.rx, b.ry); const k = Math.hypot(dx, dy, z); nx = dx / k; ny = dy / k; nz = z / k; }
      }
      if (best < 0) return 0; // the subject on black, like the reference
      const diff = Math.max(0, nx * lx + ny * ly + nz * lz);
      const rim = Math.pow(1 - nz, 3) * 0.12;
      return Math.min(0.8, 0.03 + 0.7 * Math.pow(diff, 1.5) + rim);
    });
  }
}
