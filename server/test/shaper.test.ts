import { test } from 'node:test';
import assert from 'node:assert/strict';
import { Shaper, sanitizeShaper, seededRng, UDP_IP_OVERHEAD } from '../src/shaper.js';
import { FakeClock } from './fake.js';

const mk = (rng: () => number = () => 0.99) => { const clock = new FakeClock(); return { clock, s: new Shaper({ now: clock.now, rng, schedule: clock.schedule, cancel: clock.cancel }) }; };

test('disabled: everything passes synchronously', () => {
  const { s } = mk(() => 0);
  s.set({ loss: 1, bps: 1, delayMs: 1000 }); // configured but not enabled
  let n = 0;
  for (let i = 0; i < 10; i++) assert.equal(s.offer(100, () => n++), true);
  assert.equal(n, 10);
  assert.equal(s.counters.dropped, 0);
});

test('loss: Bernoulli drops exactly where rng < loss, reproducibly', () => {
  const seq = [0.1, 0.5, 0.29, 0.31, 0.0, 0.99]; let i = 0;
  const { s } = mk(() => seq[i++]);
  s.set({ enabled: true, loss: 0.3 });
  const got = seq.map(() => s.offer(50, () => {}));
  assert.deepEqual(got, [false, true, false, true, false, true]);
  assert.equal(s.counters.droppedLoss, 3);

  const run = (seed: number) => { const { s } = mk(seededRng(seed)); s.set({ enabled: true, loss: 0.3 }); return Array.from({ length: 10_000 }, () => s.offer(50, () => {})); };
  const a = run(7), b = run(7);
  assert.deepEqual(a, b);
  const rate = a.filter(x => !x).length / a.length;
  assert.ok(Math.abs(rate - 0.3) < 0.02, `drop rate ${rate}`);
});

test('loss 1.0 is a blackout', () => {
  const { s } = mk(seededRng(1));
  s.set({ enabled: true, loss: 1 });
  let n = 0;
  for (let i = 0; i < 1000; i++) s.offer(10, () => n++);
  assert.equal(n, 0);
  assert.equal(s.counters.dropped, 1000);
});

test('cap: bucket starts full, then admits bps over time', () => {
  const { s, clock } = mk();
  s.set({ enabled: true, bps: 8_000, burstSec: 0.5 }); // 1000 B/s, 500 B bucket
  const wire = 100 + UDP_IP_OVERHEAD; // 128 B
  // Admitted while the bucket is positive: 500 -> 372 -> 244 -> 116 -> -12 (debt).
  const burst = [0, 1, 2, 3, 4].map(() => s.offer(100, () => {}));
  assert.deepEqual(burst, [true, true, true, true, false]);
  clock.advance(12); // repays the debt exactly: still not positive
  assert.equal(s.offer(100, () => {}), false);
  clock.advance(1);
  assert.equal(s.offer(100, () => {}), true);
  assert.equal(s.offer(100, () => {}), false);

  // Long run: offered 12.8 kB/s for 10 s, delivered ~= 1000 B/s + one bucket.
  const { s: s2, clock: c2 } = mk();
  s2.set({ enabled: true, bps: 8_000 });
  let bytes = 0;
  for (let t = 0; t < 10_000; t += 10) { s2.offer(100, () => { bytes += wire; }); c2.advance(10); }
  assert.ok(bytes <= 10_000 + 500 + wire, `delivered ${bytes} B`);
  assert.ok(bytes >= 10_000 - wire, `delivered ${bytes} B`);
  assert.equal(s2.counters.droppedCap, s2.counters.offered - s2.counters.passed);
});

test('cap: burstSec sets the bucket depth', () => {
  const { s } = mk();
  s.set({ enabled: true, bps: 8_000, burstSec: 2 }); // 2000 B bucket
  let n = 0;
  while (s.offer(100, () => {})) n++;
  assert.equal(n, Math.ceil(2000 / 128)); // one bucket plus at most one datagram
  assert.equal(s.capacity(), 2000);
});

test('cap: admission does not depend on size, so keyframes do not starve behind deltas', () => {
  const { s, clock } = mk();
  s.set({ enabled: true, bps: 2_000 }); // 250 B/s, 125 B bucket
  assert.equal(s.offer(140, () => {}), true); // 168 B wire > whole bucket: passes, bucket -43
  assert.equal(s.offer(10, () => {}), false); // in debt
  clock.advance(500); // +125 B -> 82 B
  assert.equal(s.offer(40, () => {}), true); // 68 B -> 14 B
  assert.equal(s.offer(140, () => {}), true); // positive: a keyframe gets through -> -154 B
  assert.equal(s.offer(10, () => {}), false);
  // Under sustained overload (small deltas every 100 ms, a keyframe every 2 s) both kinds pass,
  // and the delivered rate stays at the cap.
  const { s: s2, clock: c2 } = mk();
  s2.set({ enabled: true, bps: 2_000 });
  let kf = 0, small = 0, bytes = 0;
  for (let t = 0; t < 60_000; t += 100) {
    if (t % 2000 === 0 && s2.offer(140, () => { kf++; bytes += 168; })) { /* counted in deliver */ }
    s2.offer(40, () => { small++; bytes += 68; });
    c2.advance(100);
  }
  assert.ok(kf >= 10, `keyframes delivered: ${kf}/30`);
  assert.ok(small > 100, `deltas delivered: ${small}`);
  assert.ok(bytes <= 250 * 60 + 125 + 168, `delivered ${bytes} B in 60 s`);
});

test('delay: nothing early, FIFO order, no overtaking when the delay drops', () => {
  const { s, clock } = mk();
  s.set({ enabled: true, delayMs: 300 });
  const got: [number, number][] = [];
  for (let i = 0; i < 5; i++) { s.offer(10, () => got.push([i, clock.t])); clock.advance(10); }
  assert.equal(got.length, 0);
  assert.equal(s.counters.inFlight, 5);
  s.set({ delayMs: 0 });
  s.offer(10, () => got.push([5, clock.t])); // must wait behind the queue
  assert.equal(got.length, 0);
  const t0 = clock.t - 50;
  clock.advance(400);
  assert.deepEqual(got.map(g => g[0]), [0, 1, 2, 3, 4, 5]);
  got.slice(0, 5).forEach(([i, t]) => assert.equal(t, t0 + i * 10 + 300));
  assert.equal(got[5][1], got[4][1]);
  assert.equal(s.counters.inFlight, 0);
  assert.equal(s.counters.delivered, 6);
  // Queue drained: zero delay is synchronous again.
  let sync = false; s.offer(10, () => { sync = true; }); assert.equal(sync, true);
});

test('setFor: timed override reverts; an explicit change ends it early', () => {
  const { s, clock } = mk();
  s.set({ enabled: true, bps: 50_000, delayMs: 300, loss: 0.05 });
  s.setFor({ loss: 1 }, 10_000);
  assert.equal(s.config.loss, 1);
  assert.equal(s.revertInMs(), 10_000);
  clock.advance(9_999); assert.equal(s.config.loss, 1);
  clock.advance(1); assert.equal(s.config.loss, 0.05); assert.equal(s.revertInMs(), null);
  assert.equal(s.config.bps, 50_000);

  s.setFor({ loss: 1 }, 10_000);
  s.setFor({ loss: 1 }, 10_000); // re-arming keeps the original config to restore
  clock.advance(3_000);
  s.set({ delayMs: 800 }); // user touched a slider: blackout over, change applies on the old config
  assert.deepEqual([s.config.loss, s.config.delayMs], [0.05, 800]);
  clock.advance(20_000);
  assert.deepEqual([s.config.loss, s.config.delayMs], [0.05, 800]);
});

test('queue: at most `queue` datagrams in the delay line, like netem limit; 0 = unbounded', () => {
  const { s, clock } = mk();
  s.set({ enabled: true, delayMs: 300, queue: 4 });
  assert.deepEqual([0, 1, 2, 3, 4, 5].map(() => s.offer(20, () => {})), [true, true, true, true, false, false]);
  assert.deepEqual([s.counters.droppedQueue, s.counters.dropped, s.counters.inFlight], [2, 2, 4]);
  clock.advance(300); // the four leave the line
  assert.equal(s.counters.inFlight, 0);
  assert.equal(s.offer(20, () => {}), true);
  s.set({ queue: 0 });
  assert.ok(Array.from({ length: 50 }, () => s.offer(20, () => {})).every(Boolean));
  // Lost before the queue (like netem): a loss drop does not count as a queue drop.
  const { s: s2 } = mk(() => 0);
  s2.set({ enabled: true, delayMs: 300, queue: 1, loss: 0.5 });
  s2.offer(20, () => {});
  assert.deepEqual([s2.counters.droppedLoss, s2.counters.droppedQueue], [1, 0]);
  assert.deepEqual(sanitizeShaper({ queue: '8' }).ok, { queue: 8 });
  assert.equal(sanitizeShaper({ queue: '-1' }).errors.length, 1);
});

test('sanitizeShaper validates ranges and booleans', () => {
  assert.deepEqual(sanitizeShaper({ bps: '2000', loss: '0.3', enabled: '1', delayMs: '0', burstSec: '0.5' }).ok, { bps: 2000, loss: 0.3, enabled: true, delayMs: 0, burstSec: 0.5 });
  const bad = sanitizeShaper({ loss: '30', bps: '-1', enabled: 'maybe', burstSec: '0' });
  assert.equal(bad.errors.length, 4);
  assert.deepEqual(bad.ok, {});
});
