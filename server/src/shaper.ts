// In-process link impairment: Bernoulli loss, token bucket (bps, burstSec), fixed delay. Applied to
// every datagram before it reaches the receiver so resync is exercised for real. Clock, RNG and
// timer are injected so tests are deterministic.
//
// Order per datagram: loss (like dummynet's plr, before the queue) -> queue limit -> token bucket ->
// delay queue. The queue limit is netem's `limit`: at most `queue` datagrams held in the delay line
// (netem counts delayed packets against it too), so a burst on a long-delay link is tail-dropped
// like on the Pi link box instead of building up.
// Token bucket: capacity = bps/8 * burstSec bytes, refilled at bps/8 bytes/s, starts full. A
// datagram (payload + 28 B UDP/IPv4 header) is admitted whenever the bucket is positive and its
// size is taken out, possibly leaving the bucket in debt. The long-run rate is still exactly bps
// (debt is repaid before anything else passes), bursts are at most one bucket plus one datagram,
// and admission does not depend on size: with a strict "tokens >= size" rule a 160 B keyframe
// never fits a 2 kbps link's 125 B bucket while small deltas keep it near empty, so repair
// starves. This behaves like a drop-tail queue, which is what a narrow radio link does.
// With a queue limit set (every link profile), the rate is netem's `rate`, not the bucket: a
// datagram waits for the link, serialised at bps behind the ones ahead of it, and is delivered when
// its transmission ends plus the delay; only a full queue drops. Without it (the manual slider) the
// bucket below applies. The bucket alone has no queue: at 600 bit/s it holds 37.5 B, less than one
// datagram, so anything sent within a second of another datagram was dropped, which is not what
// the Pi box's netem does (a paced keyframe 0.2 s after a delta was lost every time).
// Delay is FIFO: a datagram is never delivered before one offered earlier, even if delayMs drops.
import type { ShaperConfig } from './types.js';

export const DEFAULT_SHAPER: ShaperConfig = { bps: 0, delayMs: 0, loss: 0, enabled: false, burstSec: 0.5, queue: 0 };
export const UDP_IP_OVERHEAD = 28;

export interface ShaperDeps {
  now?: () => number;
  rng?: () => number;
  schedule?: (fn: () => void, ms: number) => unknown;
  cancel?: (handle: unknown) => void;
}

export interface ShaperCounters {
  offered: number; offeredBytes: number;
  passed: number; passedBytes: number;     // admitted to the link (includes in flight)
  delivered: number; deliveredBytes: number;
  dropped: number; droppedBytes: number;
  droppedLoss: number; droppedCap: number; droppedQueue: number;
  inFlight: number;
}

/** Deterministic PRNG (mulberry32) for reproducible loss patterns. */
export function seededRng(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6D2B79F5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** Validate a partial config. Returns the cleaned values or a list of errors. */
export function sanitizeShaper(c: Record<string, unknown>): { ok: Partial<ShaperConfig>; errors: string[] } {
  const ok: Partial<ShaperConfig> = {}; const errors: string[] = [];
  const num = (k: string, lo: number, hi: number): number | undefined => {
    if (c[k] === undefined || c[k] === null || c[k] === '') return undefined;
    const v = Number(c[k]);
    if (!Number.isFinite(v) || v < lo || v > hi) { errors.push(`${k} must be a number in [${lo}, ${hi}]`); return undefined; }
    return v;
  };
  const bps = num('bps', 0, 1e10); if (bps !== undefined) ok.bps = Math.round(bps);
  const delayMs = num('delayMs', 0, 60_000); if (delayMs !== undefined) ok.delayMs = delayMs;
  const loss = num('loss', 0, 1); if (loss !== undefined) ok.loss = loss;
  const burstSec = num('burstSec', 0.01, 10); if (burstSec !== undefined) ok.burstSec = burstSec;
  const queue = num('queue', 0, 100_000); if (queue !== undefined) ok.queue = Math.round(queue);
  if (c.enabled !== undefined && c.enabled !== '') {
    const e = c.enabled;
    if (e === true || e === 1 || e === '1' || e === 'true' || e === 'on') ok.enabled = true;
    else if (e === false || e === 0 || e === '0' || e === 'false' || e === 'off') ok.enabled = false;
    else errors.push('enabled must be 0/1/true/false');
  }
  return { ok, errors };
}

export class Shaper {
  config: ShaperConfig = { ...DEFAULT_SHAPER };
  readonly counters: ShaperCounters = {
    offered: 0, offeredBytes: 0, passed: 0, passedBytes: 0, delivered: 0, deliveredBytes: 0,
    dropped: 0, droppedBytes: 0, droppedLoss: 0, droppedCap: 0, droppedQueue: 0, inFlight: 0,
  };
  private readonly now: () => number;
  private readonly rng: () => number;
  private readonly schedule: (fn: () => void, ms: number) => unknown;
  private readonly cancel: (handle: unknown) => void;
  private tokens = Infinity; // full; clamped to capacity on first refill
  private lastRefill: number;
  private line: { due: number; bytes: number; deliver: () => void }[] = [];
  private timer: unknown = null;
  private timerDue = Infinity;
  private lastDue = -Infinity;
  /** When the link finishes sending the last datagram admitted in queue mode (ms). */
  private lastTxEnd = -Infinity;
  private revert: { prev: ShaperConfig; at: number; handle: unknown } | null = null;

  constructor(deps: ShaperDeps | (() => number) = {}) {
    const d: ShaperDeps = typeof deps === 'function' ? { rng: deps } : deps; // old signature: (rng)
    this.now = d.now ?? Date.now;
    this.rng = d.rng ?? Math.random;
    this.schedule = d.schedule ?? ((fn, ms) => setTimeout(fn, ms));
    this.cancel = d.cancel ?? (h => clearTimeout(h as ReturnType<typeof setTimeout>));
    this.lastRefill = this.now();
  }

  /** Legacy counters (kept for the WS log message). */
  get dropped(): number { return this.counters.dropped; }
  get passed(): number { return this.counters.passed; }

  /** Merge a partial config. Any explicit change ends a pending timed override (e.g. blackout). */
  set(c: Partial<ShaperConfig>): ShaperConfig {
    const { ok } = sanitizeShaper(c as Record<string, unknown>);
    if (this.revert) { this.config = this.revert.prev; this.cancel(this.revert.handle); this.revert = null; }
    this.config = { ...this.config, ...ok };
    return this.config;
  }

  /** Apply `c` for `ms`, then restore the config that was active before. */
  setFor(c: Partial<ShaperConfig>, ms: number): ShaperConfig {
    const prev = this.revert ? this.revert.prev : { ...this.config };
    this.set(c);
    const handle = this.schedule(() => {
      const r = this.revert;
      if (r && r.handle === handle) { this.config = r.prev; this.revert = null; }
    }, ms);
    this.revert = { prev, at: this.now() + ms, handle };
    return this.config;
  }

  /** Milliseconds until a timed override reverts, or null. */
  revertInMs(): number | null { return this.revert ? Math.max(0, this.revert.at - this.now()) : null; }

  /** Bytes the bucket can hold at the current config (Infinity when uncapped). */
  capacity(): number { return this.config.bps > 0 ? this.config.bps / 8 * this.config.burstSec : Infinity; }

  /** Returns true if the datagram was admitted (`deliver` runs now or after the delay), false if dropped. */
  offer(bytes: number, deliver: () => void): boolean {
    const c = this.config; const k = this.counters;
    const wire = bytes + UDP_IP_OVERHEAD;
    k.offered++; k.offeredBytes += wire;
    if (!c.enabled) return this.admit(wire, 0, deliver);
    if (c.loss > 0 && this.rng() < c.loss) { k.droppedLoss++; return this.drop(wire); }
    if (c.queue && this.line.length >= c.queue) { k.droppedQueue++; return this.drop(wire); }
    if (c.bps > 0 && c.queue) {
      const now = this.now();
      const txEnd = Math.max(now, this.lastTxEnd) + wire * 8 / c.bps * 1000;
      this.lastTxEnd = txEnd;
      return this.admit(wire, txEnd - now + c.delayMs, deliver);
    }
    if (c.bps > 0) {
      const now = this.now(); const cap = this.capacity();
      this.tokens = Math.min(cap, this.tokens + Math.max(0, now - this.lastRefill) / 1000 * c.bps / 8);
      this.lastRefill = now;
      if (this.tokens <= 0) { k.droppedCap++; return this.drop(wire); }
      this.tokens -= wire;
    }
    return this.admit(wire, c.delayMs, deliver);
  }

  private drop(wire: number): false {
    this.counters.dropped++; this.counters.droppedBytes += wire;
    return false;
  }

  private admit(wire: number, delayMs: number, deliver: () => void): true {
    const k = this.counters;
    k.passed++; k.passedBytes += wire;
    const now = this.now();
    const due = Math.max(now + delayMs, this.lastDue);
    this.lastDue = due;
    if (due <= now && this.line.length === 0) { this.deliverOne(wire, deliver); return true; }
    this.line.push({ due, bytes: wire, deliver });
    k.inFlight++;
    this.arm();
    return true;
  }

  private deliverOne(wire: number, deliver: () => void) {
    this.counters.delivered++; this.counters.deliveredBytes += wire;
    deliver();
  }

  private arm() {
    if (!this.line.length) return;
    const due = this.line[0].due;
    if (this.timer !== null && this.timerDue <= due) return;
    if (this.timer !== null) this.cancel(this.timer);
    this.timerDue = due;
    this.timer = this.schedule(() => this.drain(), Math.max(0, due - this.now()));
  }

  private drain() {
    this.timer = null; this.timerDue = Infinity;
    const now = this.now();
    while (this.line.length && this.line[0].due <= now) {
      const q = this.line.shift()!;
      this.counters.inFlight--;
      try { this.deliverOne(q.bytes, q.deliver); } catch { /* receiver errors are the receiver's business */ }
    }
    this.arm();
  }
}
