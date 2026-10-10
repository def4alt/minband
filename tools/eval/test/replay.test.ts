import { test } from 'node:test';
import assert from 'node:assert/strict';
import { maxOf, replayRows, unpack } from '../src/replay.ts';
import { generate } from '../src/synth.ts';
import { baselineBbps, baselineC, baselineRows, minbandBps, thumbnailIntervalS, LINK_PROFILES, THUMBNAIL_CHIP_BYTES } from '../src/baselines.ts';
import { toFrames } from '../src/gt.ts';
import type { GtRow } from '../src/gt.ts';

function staticScene(seconds: number, hz = 30): GtRow[] {
  const rows: GtRow[] = [];
  for (let tick = 0; tick < seconds * 120; tick += 120 / hz) {
    rows.push({ tick, id: 1, class: 56, x: -1.5, y: 0.45, z: 1.2, vx: 0, vy: 0, vz: 0, conf: 170 });
    rows.push({ tick, id: 2, class: 63, x: 1.2, y: 0.75, z: 0.4, vx: 0, vy: 0, vz: 0, conf: 150 });
  }
  return rows;
}

test('2-entity static scene: ~0 error, full availability, < 200 bytes/s', () => {
  const r = replayRows(staticScene(60), { thetaPos: 0.15 });
  assert.ok(r.errMean < 1e-4, `err ${r.errMean}`);
  assert.ok(r.errMax < 1e-4, `max ${r.errMax}`);
  assert.equal(r.availability, 1);
  assert.equal(r.phantomRows, 0);
  assert.ok(r.bytesPerSec < 200, `${r.bytesPerSec} B/s`);
  assert.ok(r.bytesPerSec > 0);
  assert.equal(r.wireBytes, r.payloadBytes + 28 * r.datagrams);
});

test('lossless, zero-delay link: twin error at logged frames never exceeds theta_pos', () => {
  const rows = generate('three_walkers', { durationS: 30 });
  for (const theta of [0.05, 0.15, 0.5]) {
    const r = replayRows(rows, { thetaPos: theta });
    assert.ok(r.errMax <= theta + 1e-4, `theta ${theta}: max ${r.errMax}`);
    assert.equal(r.availability, 1);
    assert.equal(r.deltas + r.keyframes + r.otherDatagrams, r.datagrams);
  }
});

test('tighter threshold costs more bytes and buys less error', () => {
  const rows = generate('one_walker', { durationS: 60 });
  const tight = replayRows(rows, { thetaPos: 0.05 }), loose = replayRows(rows, { thetaPos: 0.5 });
  assert.ok(tight.bytesPerSec > loose.bytesPerSec);
  assert.ok(tight.errMean < loose.errMean);
});

test('lossy link is deterministic per seed and actually drops datagrams', () => {
  const rows = generate('three_walkers', { durationS: 30 });
  const a = replayRows(rows, { thetaPos: 0.15, loss: 0.2, delayTicks: 6, seed: 3 });
  const b = replayRows(rows, { thetaPos: 0.15, loss: 0.2, delayTicks: 6, seed: 3 });
  assert.deepEqual(a, b);
  assert.ok(a.lostDatagrams > 0);
  assert.ok(a.receiver.gapsDetected > 0);
  assert.ok(a.acksSent > 0, 'gaps produce nacks');
  const c = replayRows(rows, { thetaPos: 0.15, loss: 0.2, delayTicks: 6, seed: 4 });
  assert.notDeepEqual(a, c);
});

test('unpack splits u16-LE length-prefixed datagrams', () => {
  const buf = new Uint8Array([2, 0, 0xaa, 0xbb, 0, 0, 1, 0, 0xcc]);
  assert.deepEqual(unpack(buf).map(d => [...d]), [[0xaa, 0xbb], [], [0xcc]]);
});

test('maxOf handles a busy 4K clip: half a million per-row errors (Math.max(...xs) overflowed the stack)', () => {
  const xs = new Float64Array(500_000).map((_, i) => (i * 7919) % 1000 / 100);
  xs[123_456] = 42;
  assert.equal(maxOf(xs), 42);
  assert.equal(maxOf([]), 0);
  assert.equal(maxOf([-2, -1]), -1);
});

test('baseline B formula matches the server: entities * 31 B * 30 Hz * 8 + 30 Hz * 40 B * 8', () => {
  assert.equal(baselineBbps(1), 17040);
  assert.equal(baselineBbps(3), 31920);
  assert.equal(baselineBbps(0), 9600);
});

test('baseline C formula: one 150 B chip + 28 B UDP/IP every (150 + 28) * 8 / R seconds', () => {
  assert.equal(THUMBNAIL_CHIP_BYTES, 150);
  assert.equal(thumbnailIntervalS(1424), 1); // 178 B * 8 = 1424 bit
  assert.equal(thumbnailIntervalS(712), 2);
  assert.ok(Math.abs(thumbnailIntervalS(9600) - 0.148333) < 1e-6, 'hf');
  assert.equal(thumbnailIntervalS(2000), 0.712, 'lora');
  assert.ok(Math.abs(thumbnailIntervalS(600) - 2.373333) < 1e-6, 'telemetry');
  assert.equal(thumbnailIntervalS(1000, 100), 1.024, 'other chip size');
  assert.equal(thumbnailIntervalS(0), Infinity);
  assert.deepEqual(LINK_PROFILES.map(l => [l.id, l.bps]), [['hf', 9600], ['lora', 2000], ['telemetry', 600]]);
});

test('baseline C at equal bytes uses MinBand\'s own wire rate at the default theta, plus one row per link profile', () => {
  const rows = generate('one_walker', { durationS: 30 });
  const { frames, step } = toFrames(rows);
  const input = { name: 'one_walker', frames, step };
  const r = replayRows(rows, { thetaPos: 0.15 });
  assert.equal(minbandBps(input), r.bytesPerSec * 8, 'same replay as the sweep\'s default-theta row');
  const c = baselineC([input]);
  assert.deepEqual(c.map(x => [x.id, x.scenario]), [['thumb_equal_bytes', 'one_walker'], ['thumb_hf', ''], ['thumb_lora', ''], ['thumb_telemetry', '']]);
  assert.equal(c[0].intervalS, (150 + 28) * 8 / (r.bytesPerSec * 8));
  // Equal bytes: the thumbnail feed sends exactly MinBand's wire bytes over the log.
  assert.ok(Math.abs((r.durationS / c[0].intervalS) * (150 + 28) - r.wireBytes) < 1e-6);
  const csv = baselineRows([], [], c);
  assert.deepEqual(csv.map(x => [x.kind, x.chip_bytes]), [['C', 150], ['C', 150], ['C', 150], ['C', 150]]);
  assert.equal(csv[2].interval_s, 0.712);
  assert.equal(csv[2].bytes_per_s, 250);
});
