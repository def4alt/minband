// In-process link impairment: token bucket (bps), fixed delay, Bernoulli loss. Applied to every
// datagram before it reaches the receiver so resync is exercised for real.
import type { ShaperConfig } from './types.js';

export class Shaper {
  config: ShaperConfig = { bps: 0, delayMs: 0, loss: 0, enabled: false };
  private tokens = 0;
  private lastRefill = Date.now();
  dropped = 0;
  passed = 0;

  constructor(private rng: () => number = Math.random) {}

  set(c: Partial<ShaperConfig>) { this.config = { ...this.config, ...c }; }

  /** Returns true if the datagram is delivered (after delay via callback), false if dropped. */
  offer(bytes: number, deliver: () => void): boolean {
    const c = this.config;
    if (!c.enabled) { this.passed++; deliver(); return true; }
    if (c.loss > 0 && this.rng() < c.loss) { this.dropped++; return false; }
    if (c.bps > 0) {
      const now = Date.now();
      this.tokens = Math.min(c.bps / 8 * 0.5, this.tokens + (now - this.lastRefill) / 1000 * c.bps / 8); // burst = 0.5 s
      this.lastRefill = now;
      const wire = bytes + 28; // UDP + IPv4 headers
      if (this.tokens < wire) { this.dropped++; return false; }
      this.tokens -= wire;
    }
    this.passed++;
    if (c.delayMs > 0) setTimeout(deliver, c.delayMs); else deliver();
    return true;
  }
}
