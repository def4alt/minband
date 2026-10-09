// Edge-clock -> local-clock offset estimator.
//
// Every datagram gives a sample s = localArrivalMs - edgeSendMs = trueOffset + transitDelay, with
// transitDelay >= 0. The minimum over recent samples estimates trueOffset (plus the smallest transit
// delay seen), which is what lets the twin extrapolate to "edge now" instead of "edge now minus
// queueing delay".
//
// Rules (all times are the caller's; nothing here reads a clock, so tests drive it directly):
//  1. A sample below the estimate lowers it at once: nothing arrives before it was sent.
//  2. The estimate never rises above the min over a sliding window (default 10 s).
//  3. While every sample in the window is above the estimate (edge clock running slow, or a
//     sustained increase in latency), the estimate rises toward the window min by at most
//     `slewMsPerSec` (default 1 ms/s). Crystal drift is 0.01-0.1 ms/s, well inside that, and a
//     latency increase stays compensated (the twin keeps targeting edge-now) for a long time.
//  4. Step: if every sample for `stepHoldMs` (2 s) is more than `stepMs` (1 s) above the
//     estimate, the edge clock jumped (phone slept, ARKit timestamps stalled). Re-sync to the min
//     of those samples at once; at 1 ms/s a 10 s pause would otherwise take ~3 h to absorb.

export interface ClockOptions {
  windowMs: number;
  slewMsPerSec: number;
  stepMs: number;
  stepHoldMs: number;
}

export const DEFAULT_CLOCK: ClockOptions = { windowMs: 10_000, slewMsPerSec: 1, stepMs: 1_000, stepHoldMs: 2_000 };

export class ClockOffset {
  readonly opts: ClockOptions;
  private est: number | null = null;
  private lastMs = 0;
  /** Monotonic deque (offsets strictly increasing front to back): front is the window min. */
  private win: { t: number; off: number }[] = [];
  private aboveSince: number | null = null;
  private aboveMin = Infinity;
  samples = 0;
  steps = 0;

  constructor(opts: Partial<ClockOptions> = {}) {
    this.opts = { ...DEFAULT_CLOCK, ...opts };
  }

  reset(): void {
    this.est = null; this.lastMs = 0; this.win = []; this.aboveSince = null; this.aboveMin = Infinity;
  }

  /** Current estimate of localMs - edgeMs, or null before the first sample. */
  get offsetMs(): number | null { return this.est; }

  /** Min over the sliding window, or null when empty. */
  get windowMinMs(): number | null { return this.win.length ? this.win[0].off : null; }

  /** Map a local time to the edge clock (ms). Null before the first sample. */
  edgeMsAt(localMs: number): number | null {
    return this.est === null ? null : localMs - this.est;
  }

  /** `localMs`: arrival time on our clock. `edgeMs`: the edge's send time on its clock. */
  sample(localMs: number, edgeMs: number): void {
    const off = localMs - edgeMs;
    this.samples++;
    while (this.win.length && this.win[this.win.length - 1].off >= off) this.win.pop();
    this.win.push({ t: localMs, off });
    const cutoff = localMs - this.opts.windowMs;
    while (this.win.length > 1 && this.win[0].t < cutoff) this.win.shift();

    if (this.est === null) { this.est = off; this.lastMs = localMs; return; }
    const dt = Math.max(0, localMs - this.lastMs);
    this.lastMs = localMs;

    // Rule 1.
    if (off <= this.est) { this.est = off; this.aboveSince = null; return; }

    // Rule 4.
    if (off - this.est > this.opts.stepMs) {
      if (this.aboveSince === null) { this.aboveSince = localMs; this.aboveMin = off; }
      else this.aboveMin = Math.min(this.aboveMin, off);
      if (localMs - this.aboveSince >= this.opts.stepHoldMs) {
        this.est = this.aboveMin;
        this.steps++;
        this.aboveSince = null;
        // Forget pre-step samples so the window min cannot pin us below the new level.
        while (this.win.length > 1 && this.win[0].off < this.est) this.win.shift();
        return;
      }
    } else {
      this.aboveSince = null;
    }

    // Rules 2 + 3: every sample in the window is above the estimate when its min is.
    const m = this.win[0].off;
    if (m > this.est) this.est = Math.min(m, this.est + this.opts.slewMsPerSec * dt / 1000);
  }
}
