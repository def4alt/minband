import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ClockOffset } from '../src/clock.js';
import { seededRng } from '../src/shaper.js';

// The local clock is a plain number driven by the test (fake clock). Edge time is
// local - trueOffset(t); every sample arrives `delay` ms after it was sent.
function feed(c: ClockOffset, from: number, to: number, everyMs: number, off: (t: number) => number, delay: (t: number) => number) {
  for (let t = from; t < to; t += everyMs) { const d = delay(t); c.sample(t + d, t - off(t)); }
}

test('converges to the minimum transit delay under jitter', () => {
  const c = new ClockOffset(); const r = seededRng(1);
  feed(c, 0, 20_000, 33, () => 5_000, () => 2 + r() * 20);
  assert.ok(c.offsetMs !== null);
  assert.ok(c.offsetMs! >= 5_002 && c.offsetMs! < 5_002.5, `got ${c.offsetMs}`);
});

test('a sample below the estimate lowers it immediately', () => {
  const c = new ClockOffset();
  feed(c, 0, 5_000, 33, () => 1_000, () => 30);
  c.sample(5_000, 5_000 - 1_005);
  assert.equal(c.offsetMs, 1_005);
});

test('tracks an edge clock that runs slow (drift)', () => {
  // 500 ppm: the offset grows 0.5 ms per second, 150 ms over 5 minutes.
  const c = new ClockOffset(); const r = seededRng(2);
  const off = (t: number) => 3_000 + t * 0.0005;
  let worst = 0;
  for (let t = 0; t < 300_000; t += 50) {
    const d = 1 + r() * 10; c.sample(t + d, t - off(t));
    if (t > 20_000) worst = Math.max(worst, Math.abs(c.offsetMs! - (off(t) + 1)));
  }
  assert.ok(worst < 8, `worst tracking error ${worst.toFixed(2)} ms`);
});

test('tracks an edge clock that runs fast (offset shrinking) at once', () => {
  const c = new ClockOffset();
  const off = (t: number) => 3_000 - t * 0.001;
  feed(c, 0, 60_000, 50, off, () => 1);
  assert.ok(Math.abs(c.offsetMs! - (off(60_000) + 1)) < 1, `got ${c.offsetMs}`);
});

test('a sustained latency increase below the step threshold is absorbed at <= 1 ms/s', () => {
  const c = new ClockOffset();
  feed(c, 0, 20_000, 33, () => 2_000, () => 5);
  const before = c.offsetMs!;
  let prevT = 20_000, prev = before;
  for (let t = 20_000; t < 80_000; t += 33) {
    c.sample(t + 305, t - 2_000);
    const inc = c.offsetMs! - prev;
    assert.ok(inc <= (t - prevT) / 1000 + 1e-9, `rose ${inc} ms in ${t - prevT} ms`);
    assert.ok(c.offsetMs! <= c.windowMinMs! + 1e-9, 'estimate above window min');
    prev = c.offsetMs!; prevT = t;
  }
  const rise = c.offsetMs! - before;
  assert.ok(rise > 40 && rise <= 60.5, `rose ${rise} ms in 60 s`);
  assert.equal(c.steps, 0);
});

test('re-syncs after the phone pauses (edge clock step)', () => {
  const c = new ClockOffset();
  feed(c, 0, 20_000, 33, () => 1_000, () => 3);
  // Phone sleeps 8 s: edge ticks stall, so afterwards local - edge is 8 s larger.
  const resume = 28_000;
  let syncedAt: number | null = null;
  for (let t = resume; t < resume + 5_000; t += 33) {
    c.sample(t + 3, t - 9_000);
    if (syncedAt === null && Math.abs(c.offsetMs! - 9_003) < 1) syncedAt = t;
  }
  assert.ok(syncedAt !== null, `never re-synced, at ${c.offsetMs}`);
  assert.ok(syncedAt! - resume <= c.opts.stepHoldMs + 50, `re-synced after ${syncedAt! - resume} ms`);
  assert.equal(c.steps, 1);
  // Old, lower samples must not drag it back.
  feed(c, resume + 5_000, resume + 8_000, 33, () => 9_000, () => 3);
  assert.ok(Math.abs(c.offsetMs! - 9_003) < 1);
});

test('a short burst of late samples is not mistaken for a step', () => {
  const c = new ClockOffset();
  feed(c, 0, 10_000, 33, () => 500, () => 2);
  feed(c, 10_000, 11_500, 33, () => 500, () => 1_800); // 1.5 s of 1.8 s latency (< stepHoldMs)
  feed(c, 11_500, 15_000, 33, () => 500, () => 2);
  assert.ok(Math.abs(c.offsetMs! - 502) < 1, `got ${c.offsetMs}`);
  assert.equal(c.steps, 0);
});

test('never exceeds the window min, and reset() forgets everything', () => {
  const c = new ClockOffset(); const r = seededRng(3);
  let base = 100;
  for (let t = 0; t < 120_000; t += 20 + r() * 200) {
    if (r() < 0.002) base += (r() - 0.3) * 3_000; // random clock steps
    c.sample(t + r() * 50, t - base);
    assert.ok(c.offsetMs! <= c.windowMinMs! + 1e-9);
  }
  c.reset();
  assert.equal(c.offsetMs, null);
  assert.equal(c.edgeMsAt(5), null);
  c.sample(10, 0);
  assert.equal(c.edgeMsAt(20), 10);
});
