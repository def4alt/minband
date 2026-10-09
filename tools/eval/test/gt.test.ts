import { test } from 'node:test';
import assert from 'node:assert/strict';
import { formatGt, parseGt, toFrames, GT_HEADER, type GtRow } from '../src/gt.ts';
import { generate } from '../src/synth.ts';

test('CSV round trip: parse(format(rows)) == rows to 10 um, and format is a fixed point', () => {
  const rows = generate('three_walkers', { durationS: 5, noise: 0.03, seed: 7 });
  assert.ok(rows.length > 400);
  const text = formatGt(rows);
  assert.ok(text.startsWith(GT_HEADER + '\n'));
  const back = parseGt(text);
  assert.equal(back.length, rows.length);
  back.forEach((r, i) => {
    const o = rows[i];
    for (const k of ['tick', 'id', 'class', 'conf'] as const) assert.equal(r[k], o[k], `row ${i} ${k}`);
    for (const k of ['x', 'y', 'z', 'vx', 'vy', 'vz'] as const) assert.ok(Math.abs(r[k] - o[k]) <= 5e-6, `row ${i} ${k}: ${r[k]} vs ${o[k]}`);
  });
  assert.equal(formatGt(back), text);
});

test('parser accepts the phone format (header, CRLF, blank lines, %.5f floats) and rejects short rows', () => {
  const phone = 'tick,id,class,x,y,z,vx,vy,vz,conf\r\n8,3,0,1.25000,0.90000,-2.00000,0.50000,0.00000,0.00000,210\r\n\r\n4,1,56,0,0,0,0,0,0,180\r\n';
  const rows = parseGt(phone);
  assert.deepEqual(rows.map(r => [r.tick, r.id]), [[4, 1], [8, 3]], 'sorted by tick');
  assert.deepEqual([rows[1].x, rows[1].y, rows[1].z, rows[1].vx, rows[1].conf], [1.25, 0.9, -2, 0.5, 210]);
  assert.throws(() => parseGt('1,2,3\n'), /expected 10 columns/);
});

test('frames: one per tick, gaps in the log become empty frames at the frame period', () => {
  const row = (tick: number, id: number): GtRow => ({ tick, id, class: 0, x: 0, y: 0, z: 0, vx: 0, vy: 0, vz: 0, conf: 200 });
  const rows = [row(0, 1), row(0, 2), row(4, 1), row(8, 1), row(20, 1)];
  const { frames, step } = toFrames(rows);
  assert.equal(step, 4);
  assert.deepEqual(frames.map(f => [f.tick, f.tracks.length]), [[0, 2], [4, 1], [8, 1], [12, 0], [16, 0], [20, 1]]);
});
