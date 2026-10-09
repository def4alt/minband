// Dot-screen rendering for anything photographic (docs/STYLE.md, Halftone): monochrome, 4..6 px
// cells, dot area proportional to luminance, ink dots on --bg. Three uses:
//  - live: the camera sampled at 320 px wide and redrawn at 15 fps (or the stand-in bust, drawn once);
//  - paint (V1): a frozen frame revealed row by row as its bits arrive over the link, the previous
//    frame dimmed below the row being written;
//  - chip (S23): the same picture as a coarse thumbnail, standing for a 150 B AI chip.

const SAMPLE_W = 320, FPS = 15;
const css = (name: string, fallback: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;

/** Luminance 0..1 at a point in CSS px of the panel. */
export type Lum = (x: number, y: number) => number;

export class Halftone {
  private ctx: CanvasRenderingContext2D;
  private sample = document.createElement('canvas');
  private sctx = this.sample.getContext('2d', { willReadFrequently: true })!;
  private video: HTMLVideoElement | null = null;
  private raf = 0;
  private last = 0;
  private lo = 0.05; private hi = 0.9; // smoothed auto-levels
  private ink = css('--ink', '#e9ecef');
  private ink2 = css('--ink-2', '#9aa3ad');
  private bg = css('--bg', '#070809');
  /** What to draw again after a resize while not live (the painting, or the still). */
  private redraw: (() => void) | null = null;
  /** V1 frame in progress: the frame being written, the one it replaces, and the written row (CSS px). */
  private painting: { next: Lum; prev: Lum | null; cut: number } | null = null;

  constructor(private canvas: HTMLCanvasElement) {
    this.ctx = canvas.getContext('2d')!;
    new ResizeObserver(() => { this.fit(); if (!this.raf) (this.redraw ?? (() => this.drawStatic()))(); }).observe(canvas);
  }

  /** The camera to halftone; null for the stand-in bust. Does not start or stop drawing. */
  setVideo(video: HTMLVideoElement | null) { this.video = video; }

  /** Live: halftone the camera at 15 fps until pause(), or draw the stand-in once. */
  live() {
    this.pause();
    this.redraw = null; this.painting = null;
    if (!this.video) { this.drawStatic(); return; }
    const tick = (t: number) => {
      this.raf = requestAnimationFrame(tick);
      if (t - this.last < 1000 / FPS - 2) return;
      this.last = t;
      const f = this.frame();
      if (f) this.screen(f);
    };
    this.raf = requestAnimationFrame(tick);
  }

  pause() { cancelAnimationFrame(this.raf); this.raf = 0; }

  /** The picture right now, frozen: the current camera frame, else the stand-in bust. */
  capture(): Lum { return this.frame() ?? this.bust(); }

  /**
   * V1: `next` above the row being written (`progress` 0..1 of the frame's bits), the previous frame
   * dimmed below it, and a hairline at the row. Draws the whole panel; use
   * paintTo() to advance the same frame (it redraws only the rows the cut crossed).
   */
  paint(next: Lum, prev: Lum | null, progress: number) {
    this.pause();
    this.painting = { next, prev, cut: progress * this.canvas.clientHeight };
    this.redraw = () => { if (this.painting) this.paint(this.painting.next, this.painting.prev, this.painting.cut / Math.max(1, this.canvas.clientHeight)); };
    if (!this.fit()) return;
    dots(this.canvas, this.ctx, this.cell(), this.ink, this.bg, this.paintLum());
    this.scanLine();
  }

  /** Advance the frame started by paint() to `progress`, redrawing only the rows between the two cuts. */
  paintTo(progress: number) {
    const p = this.painting;
    if (!p || this.raf || !this.fit()) return;
    const cut = progress * this.canvas.clientHeight;
    if (Math.abs(cut - p.cut) < 0.5) return;
    const from = Math.min(p.cut, cut) - 2, to = Math.max(p.cut, cut) + 2; // the old hairline's band too
    p.cut = cut;
    dots(this.canvas, this.ctx, this.cell(), this.ink, this.bg, this.paintLum(), from, to);
    this.scanLine();
  }

  private paintLum(): Lum {
    const { next, prev, cut } = this.painting!;
    return (x, y) => y < cut ? next(x, y) : prev ? 0.12 * prev(x, y) : 0;
  }
  private scanLine() {
    const cut = this.painting!.cut, H = this.canvas.clientHeight;
    if (cut <= 0 || cut >= H) return;
    const ctx = this.ctx, yy = Math.round(cut) + 0.5;
    ctx.strokeStyle = this.ink2; ctx.lineWidth = 1; ctx.globalAlpha = 0.8;
    ctx.beginPath(); ctx.moveTo(0, yy); ctx.lineTo(this.canvas.clientWidth, yy); ctx.stroke();
    ctx.globalAlpha = 1;
  }

  /** S23: the panel's picture as a coarse square chip on another canvas (centre crop, 6 px cells). */
  chip(target: HTMLCanvasElement, lum: Lum) {
    const W = target.clientWidth, H = target.clientHeight, PW = this.canvas.clientWidth, PH = this.canvas.clientHeight;
    if (!W || !H || !PW || !PH) return;
    const s = Math.min(PW / W, PH / H), ox = (PW - W * s) / 2, oy = (PH - H * s) / 2;
    dots(target, target.getContext('2d')!, 6, this.ink, this.bg, (x, y) => lum(ox + x * s, oy + y * s));
  }

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

  private screen(lum: Lum) {
    if (!this.fit()) return;
    dots(this.canvas, this.ctx, this.cell(), this.ink, this.bg, lum);
  }

  /** Sample the camera now; null when there is no frame yet. */
  private frame(): Lum | null {
    const v = this.video;
    if (!v || v.readyState < 2 || !v.videoWidth) return null;
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
    return (x, y) => {
      // object-fit: cover, from panel CSS px to sample px (at the panel size of the moment)
      const W = this.canvas.clientWidth, H = this.canvas.clientHeight;
      const s = Math.max(W / sw, H / sh), ox = (W - sw * s) / 2, oy = (H - sh * s) / 2;
      const u = Math.min(sw - 2, Math.max(0, (x - ox) / s)), w = Math.min(sh - 2, Math.max(0, (y - oy) / s));
      const i = (w | 0) * sw + (u | 0);
      const l = (Y[i] + Y[i + 1] + Y[i + sw] + Y[i + sw + 1]) / 4;
      const n = Math.min(1, Math.max(0, (l - lo) / span));
      return 0.9 * Math.pow(n, 1.4); // darker mids: dots, not a wall of white
    };
  }

  private drawStatic() { this.redraw = null; this.painting = null; this.screen(this.bust()); }

  /** A lit bust-like blob (head + shoulders) on black: the stand-in "photograph". */
  private bust(): Lum {
    const L = [-0.55, -0.62, 0.56]; const ln = Math.hypot(...L); const lx = L[0] / ln, ly = L[1] / ln, lz = L[2] / ln;
    return (x, y) => {
      const W = this.canvas.clientWidth, H = this.canvas.clientHeight;
      const m = Math.min(W, H), cx = W / 2, cy = H * 0.52;
      const blobs = [
        { x: cx, y: cy - m * 0.12, rx: m * 0.17, ry: m * 0.21 },     // head
        { x: cx, y: cy + m * 0.5, rx: m * 0.46, ry: m * 0.36 },      // shoulders
        { x: cx, y: cy + m * 0.12, rx: m * 0.08, ry: m * 0.12 },     // neck
      ];
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
    };
  }
}

/**
 * Draw a dot screen over a canvas (sized here to its CSS box). Rows are offset by half a cell (a
 * hexagonal screen reads closer to print than a square one). With `from`/`to` (CSS px), only the
 * rows whose bands touch that range are cleared and redrawn; bands meet on device pixels, so
 * partial redraws leave no seams.
 */
function dots(canvas: HTMLCanvasElement, ctx: CanvasRenderingContext2D, c: number, ink: string, bg: string, lum: Lum, from = -Infinity, to = Infinity) {
  const dpr = Math.min(devicePixelRatio || 1, 2), W = canvas.clientWidth, H = canvas.clientHeight;
  if (!W || !H) return;
  if (canvas.width !== Math.round(W * dpr) || canvas.height !== Math.round(H * dpr)) { canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr); }
  const rowH = c * 0.866, all = from === -Infinity && to === Infinity;
  const j0 = all ? 0 : Math.max(0, Math.floor(from / rowH)), j1 = all ? Infinity : Math.ceil(to / rowH);
  if (all) { ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.fillStyle = bg; ctx.fillRect(0, 0, canvas.width, canvas.height); }
  else {
    // Band of row j: [j * rowH, (j + 1) * rowH) in CSS px, snapped to device pixels.
    const y0 = Math.round(j0 * rowH * dpr), y1 = Math.min(canvas.height, Math.round(j1 * rowH * dpr));
    ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.fillStyle = bg; ctx.fillRect(0, y0, canvas.width, Math.max(0, y1 - y0));
    ctx.save(); ctx.beginPath(); ctx.rect(0, y0, canvas.width, Math.max(0, y1 - y0)); ctx.clip();
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = ink;
  ctx.beginPath();
  const rMax = c * 0.46; // dots never merge: the texture stays a screen
  for (let j = j0, y = (j0 + 0.5) * rowH; y < H + rowH && j < j1; j++, y += rowH) {
    for (let x = (j & 1 ? c : c / 2); x < W + c; x += c) {
      const L = lum(x, y);
      if (L <= 0.015) continue;
      const r = rMax * Math.sqrt(L);
      if (r < 0.35) continue;
      ctx.moveTo(x + r, y); ctx.arc(x, y, r, 0, Math.PI * 2);
    }
  }
  ctx.fill();
  if (!all) ctx.restore();
}
