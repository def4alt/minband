import { test } from 'node:test';
import assert from 'node:assert/strict';
import { replayRows, unpack } from '../src/replay.ts';
import { generate } from '../src/synth.ts';
import { baselineBbps } from '../src/baselines.ts';
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

test('baseline B formula matches the server: entities * 31 B * 30 Hz * 8 + 30 Hz * 40 B * 8', () => {
  assert.equal(baselineBbps(1), 17040);
  assert.equal(baselineBbps(3), 31920);
  assert.equal(baselineBbps(0), 9600);
});
