// V3 LINK ACTIVITY (docs/HACKATHON_PLAN.md section 5): one hairline tick per datagram, time
// scrolling down (newest at the top), width proportional to bytes on the link. Uplink extends right
// of the spine, downlink (acks) left, on one scale. Dropped datagrams are dotted --ink-3 with a
// small x; keyframes carry a short end cap, so the heartbeat reads as a rhythm. Beside it, H.264 on
// the same scale: 1200 B datagrams at its bitrate, which is a solid bar. A missed keyframe (a device
// starts coasting) and the resync are rules across the strip; a dead link is a faint wash.
// Everything is drawn against the snapshot clock, so a frozen mock gives a frozen strip.
import { fmtBps, linkDown } from './link';
import type { PacketEvent, Snapshot } from './types';

const css = (name: string, fallback: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
const PX_PER_S = 36, KEEP_MS = 60_000, VIDEO_DATAGRAM = 1200, TOP = 18;

interface Mark { t: number; kind: 'missed' | 'resync'; dev: string }

export class Waterfall {
  private packets: PacketEvent[] = [];
  private marks = new Map<string, Mark>();
  private coasting = new Map<string, boolean>();
  private down: { from: number; to: number | null }[] = [];
  private now = 0;
  private ink = css('--ink', '#e9ecef'); private ink2 = css('--ink-2', '#9aa3ad');
  private ink3 = css('--ink-3', '#4a525b'); private ink4 = css('--ink-4', '#1a1e23');
  private mono = css('--mono', 'monospace');

  constructor(private canvas: HTMLCanvasElement) {}

  /** Record a snapshot's datagrams and link events (cheap; call on every snapshot, drawn or not). */
  push(snap: Snapshot) {
    this.now = snap.t;
    for (const p of snap.packets ?? []) this.packets.push(p);
    for (const d of snap.devices) {
      if (d.coasting === undefined) continue; // older server: no heartbeat state
      const key = d.key ?? `id:${d.deviceId}`, dev = d.provisional ? '?' : String(d.deviceId);
      const was = this.coasting.get(key) ?? false;
      // The heartbeat was declared missed one coast period after the last datagram. Derived from the
      // snapshot rather than from a transition, so a viewer that joins mid-blackout still shows it.
      if (d.coasting && d.cadence) this.marks.set(`${key} missed ${d.lastSeenMs}`, { t: d.lastSeenMs + d.cadence.coastMs, kind: 'missed', dev });
      if (was && !d.coasting) this.marks.set(`${key} resync ${d.lastSeenMs}`, { t: d.lastSeenMs, kind: 'resync', dev });
      this.coasting.set(key, d.coasting);
    }
    const isDown = linkDown(snap), last = this.down[this.down.length - 1];
    if (isDown && (!last || last.to !== null)) this.down.push({ from: snap.t, to: null });
    if (!isDown && last && last.to === null) last.to = snap.t;
    const cut = snap.t - KEEP_MS;
    if (this.packets.length && this.packets[0].t < cut) this.packets = this.packets.filter(p => p.t >= cut);
    for (const [k, m] of this.marks) if (m.t < cut) this.marks.delete(k);
    while (this.down.length && this.down[0].to !== null && this.down[0].to < cut) this.down.shift();
  }

  /** Draw at the canvas's current size; `video` is the H.264 reference for the solid bar. */
  draw(video: { bps: number; label: string; measured: boolean }) {
    const c = this.canvas, ctx = c.getContext('2d')!;
    const dpr = Math.min(devicePixelRatio || 1, 2), W = c.clientWidth, H = c.clientHeight;
    if (!W || !H) return;
    if (c.width !== Math.round(W * dpr) || c.height !== Math.round(H * dpr)) { c.width = Math.round(W * dpr); c.height = Math.round(H * dpr); }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    ctx.lineWidth = 1;
    // Lanes: down | spine | up | gap | video. Up and video share one scale: 1200 B = the lane width.
    const gap = 12, downW = Math.round(W * 0.2), upW = Math.floor((W - downW - 2 * gap) / 2);
    const spine = downW + 0.5, videoX = W - upW, k = upW / VIDEO_DATAGRAM;
    const y = (t: number) => TOP + (this.now - t) / 1000 * PX_PER_S;
    const px = (v: number) => Math.round(v) + 0.5;

    ctx.font = `400 8.5px ${this.mono}`; ctx.textBaseline = 'top';
    ctx.fillStyle = this.ink3;
    ctx.textAlign = 'right'; ctx.fillText('◂ DOWN', spine - 4, 2);
    ctx.textAlign = 'left'; ctx.fillText('UP ▸', spine + 4, 2);
    ctx.fillText(video.label.toUpperCase(), videoX, 2);

    // A dead link: a faint wash across the MinBand lanes.
    ctx.fillStyle = this.ink; ctx.globalAlpha = 0.05;
    for (const d of this.down) {
      const y0 = Math.max(TOP, y(d.to ?? this.now)), y1 = Math.min(H, y(d.from));
      if (y1 > y0) ctx.fillRect(0, y0, videoX - gap, y1 - y0);
    }
    ctx.globalAlpha = 1;

    // Time grid every 5 s, labelled at the left edge.
    ctx.strokeStyle = this.ink4; ctx.beginPath();
    for (let s = 5; TOP + s * PX_PER_S < H; s += 5) { const yy = px(TOP + s * PX_PER_S); ctx.moveTo(0, yy); ctx.lineTo(videoX - gap, yy); }
    ctx.stroke();
    ctx.fillStyle = this.ink3; ctx.textAlign = 'left'; ctx.textBaseline = 'bottom';
    for (let s = 5; TOP + s * PX_PER_S < H; s += 5) ctx.fillText(`−${s} s`, 0, TOP + s * PX_PER_S - 2);

    // Video: a datagram of 1200 B every 1200 B / bitrate. At video rates that is solid.
    const step = PX_PER_S * VIDEO_DATAGRAM * 8 / Math.max(1, video.bps);
    ctx.fillStyle = this.ink3; ctx.globalAlpha = 0.5;
    if (step < 1.5) ctx.fillRect(videoX, TOP, upW, H - TOP);
    else for (let yy = TOP; yy < H; yy += step) ctx.fillRect(videoX, Math.round(yy), upW, 1);
    ctx.globalAlpha = 1;
    ctx.fillStyle = this.ink2; ctx.textBaseline = 'bottom'; ctx.textAlign = 'left';
    ctx.fillText(fmtBps(video.bps), videoX + 4, H - (video.measured ? 4 : 16));
    if (!video.measured) ctx.fillText('configured', videoX + 4, H - 4);

    // Spine.
    ctx.strokeStyle = this.ink3; ctx.beginPath(); ctx.moveTo(spine, TOP); ctx.lineTo(spine, H); ctx.stroke();

    // Datagrams.
    for (const p of this.packets) {
      const yy = Math.round(y(p.t));
      if (yy < TOP || yy > H) continue;
      const w = Math.max(2, Math.round(p.bytes * k)), up = p.dir === 'up';
      const x0 = up ? Math.ceil(spine) + 1 : Math.floor(spine) - w, x1 = up ? x0 + w : x0;
      if (p.dropped) {
        ctx.fillStyle = this.ink3;
        for (let x = x0; x < x0 + w; x += 3) ctx.fillRect(x, yy, 1, 1);
        const tip = up ? x0 + w + 2 : x0 - 5; // a small x where it would have ended
        ctx.fillRect(tip, yy - 1, 1, 1); ctx.fillRect(tip + 2, yy - 1, 1, 1); ctx.fillRect(tip + 1, yy, 1, 1); ctx.fillRect(tip, yy + 1, 1, 1); ctx.fillRect(tip + 2, yy + 1, 1, 1);
        continue;
      }
      ctx.fillStyle = up ? this.ink : this.ink2;
      ctx.fillRect(x0, yy, w, 1);
      if (p.kind === 'keyframe') ctx.fillRect(up ? x1 - 1 : x1, yy - 2, 1, 5);
    }

    // Link events: the missed keyframe is the one dashed rule in the strip. Devices that went
    // silent together (one cut link) share a rule: `NO KEYFRAME · DEV 101 102`.
    ctx.textAlign = 'left'; ctx.textBaseline = 'bottom';
    const groups: Mark[][] = [];
    for (const m of [...this.marks.values()].sort((a, b) => a.t - b.t)) {
      const g = groups.find(g => g[0].kind === m.kind && m.t - g[g.length - 1].t < 600);
      if (g) g.push(m); else groups.push([m]);
    }
    for (const g of groups) {
      const m = g[g.length - 1], yy = px(y(m.t));
      if (yy < TOP || yy > H) continue;
      ctx.strokeStyle = m.kind === 'missed' ? this.ink2 : this.ink3;
      ctx.setLineDash(m.kind === 'missed' ? [3, 3] : []);
      ctx.beginPath(); ctx.moveTo(0, yy); ctx.lineTo(videoX - gap, yy); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = m.kind === 'missed' ? this.ink : this.ink2;
      ctx.fillText(`${m.kind === 'missed' ? 'NO KEYFRAME' : 'RESYNC'} · DEV ${[...new Set(g.map(x => x.dev))].sort().join(' ')}`, 0, yy - 2);
    }
  }
}

/**
 * Optional soft click per uplink datagram that got through, like a Geiger counter: silent while
 * the world is predictable, clicking on turns. Off by default; the AudioContext is created in the
 * toggle's click handler, so browsers allow it.
 */
export class Clicker {
  private ctx: AudioContext | null = null;
  private out: AudioNode | null = null;
  private buf: AudioBuffer | null = null;
  on = false;

  /** Call from a user gesture. */
  enable() {
    if (!this.ctx) {
      const ctx = new AudioContext();
      const n = Math.round(ctx.sampleRate * 0.006), buf = ctx.createBuffer(1, n, ctx.sampleRate), d = buf.getChannelData(0);
      for (let i = 0; i < n; i++) d[i] = (Math.random() * 2 - 1) * Math.exp(-i / (ctx.sampleRate * 0.0008));
      const band = ctx.createBiquadFilter(); band.type = 'bandpass'; band.frequency.value = 2200; band.Q.value = 0.8;
      const master = ctx.createGain(); master.gain.value = 0.35;
      band.connect(master).connect(ctx.destination);
      this.ctx = ctx; this.out = band; this.buf = buf;
    }
    void this.ctx.resume();
    this.on = true;
  }

  disable() { this.on = false; void this.ctx?.suspend(); }

  play(packets: PacketEvent[] | undefined, now: number) {
    if (!this.on || !this.ctx || !this.out || !this.buf || this.ctx.state !== 'running' || !packets) return;
    const ups = packets.filter(p => p.dir === 'up' && !p.dropped && now - p.t < 1000);
    if (!ups.length || ups.length > 40) return; // a backlog (reconnect, mock jump): stay quiet rather than burst
    const t0 = ups[0].t, base = this.ctx.currentTime + 0.04;
    for (const p of ups) {
      const src = this.ctx.createBufferSource(), g = this.ctx.createGain();
      src.buffer = this.buf; g.gain.value = Math.min(0.6, 0.15 + p.bytes / 800); // keyframes a little louder
      src.connect(g).connect(this.out);
      src.start(base + Math.max(0, (p.t - t0) / 1000));
    }
  }
}
