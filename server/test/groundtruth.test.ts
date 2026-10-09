import { test } from 'node:test';
import assert from 'node:assert/strict';
import { SnapshotRing, evaluateTwin, parseGroundTruthCsv, summarize, MISSING_M } from '../src/groundtruth.js';
import { World } from '../src/world.js';
import { Shaper } from '../src/shaper.js';
import { TICK_HZ } from '../src/types.js';
import { FakeClock, ScriptedEdge, scene, wireAcks } from './fake.js';

const csvOf = (rows: { tick: number; id: number; cls: number; pos: number[] }[]) =>
  'tick,id,class,x,y,z,vx,vy,vz,conf\n' + rows.map(r => `${r.tick},${r.id},${r.cls},${r.pos.join(',')},0,0,0,200`).join('\n') + '\n';

test('ring keeps 60 s, increasing ticks, nearest lookup', () => {
  const r = new SnapshotRing();
  for (let t = 0; t <= 80 * TICK_HZ; t += 4) r.push(t, [{ id: 1, pos: [t, 0, 0] }]);
  assert.ok(r.ticks[0] >= 20 * TICK_HZ - 4 && r.ticks[0] <= 20 * TICK_HZ);
  assert.equal(r.size, 60 * 30 + 1);
  assert.equal(r.push(100, []), false, 'non-increasing tick ignored');
  assert.equal(r.ticks[r.nearest(5001)], 5000);
  assert.equal(r.ticks[r.nearest(5003)], 5004);
  assert.equal(r.ticks[r.nearest(0)], r.ticks[0]);
  assert.equal(r.ticks[r.nearest(1e9)], 80 * TICK_HZ);
});

test('csv parsing skips header, comments and malformed rows', () => {
  const { rows, malformed } = parseGroundTruthCsv('tick,id,class,x,y,z,vx,vy,vz,conf\r\n10,1,0,1,2,3,0,0,0,200\r\n# note\n\nbad,row\n11,1,0,1,x,3,0,0,0,1\n12,2,56,0.5,0,-1,0,0,0,9\n');
  assert.equal(rows.length, 2);
  assert.equal(malformed, 2);
  assert.deepEqual(rows[1], { tick: 12, id: 2, class: 56, pos: [0.5, 0, -1] });
});

test('evaluate: distance to the closest snapshot, missing entity = 2 m, outside window skipped', () => {
  const r = new SnapshotRing();
  for (let t = 100; t <= 200; t += 4) r.push(t, [{ id: 1, pos: [0, 0, 0] }]);
  const rows = parseGroundTruthCsv(csvOf([
    { tick: 101, id: 1, cls: 0, pos: [3, 4, 0] }, // 5 m
    { tick: 150, id: 1, cls: 0, pos: [0, 0, 1] }, // 1 m
    { tick: 150, id: 9, cls: 0, pos: [0, 0, 0] }, // missing
    { tick: 50, id: 1, cls: 0, pos: [0, 0, 0] },  // before the recording
    { tick: 300, id: 1, cls: 0, pos: [0, 0, 0] }, // after
  ])).rows;
  const ev = evaluateTwin(rows, r);
  assert.equal(ev.samples, 3);
  assert.equal(ev.skipped, 2);
  assert.equal(ev.missing, 1);
  assert.ok(Math.abs(ev.meanM! - (5 + 1 + MISSING_M) / 3) < 1e-9);
  assert.equal(ev.p95M, 5);
  assert.deepEqual(summarize([]), { meanM: null, p95M: null, samples: 0 });
  const many = summarize(Array.from({ length: 100 }, (_, i) => i + 1));
  assert.equal(many.p95M, 95);
});

test('world: GT from the scene the edge saw gives a small twin error; unknown device is null', () => {
  const clock = new FakeClock();
  const world = new World({ now: clock.now, shaper: new Shaper({ now: clock.now, schedule: clock.schedule, cancel: clock.cancel }) });
  const e = new ScriptedEdge(world, clock, '127.0.0.1:9000', 42, 1);
  wireAcks(world, [e]);
  e.run(10 * TICK_HZ, { snapshotEvery: 4 });
  const rows: { tick: number; id: number; cls: number; pos: number[] }[] = [];
  for (let t = 2 * TICK_HZ; t < 10 * TICK_HZ; t++) for (const tr of scene(t)) rows.push({ tick: t, id: tr.id, cls: tr.class, pos: tr.pos });
  const r = world.postGroundTruth(42, csvOf(rows))!;
  assert.ok(r.samples > 1500, `samples ${r.samples}`);
  assert.ok(r.meanM! < 0.15, `mean ${r.meanM}`);
  assert.ok(r.p95M! < 0.3, `p95 ${r.p95M}`);
  const m = world.metrics();
  assert.equal(m.twinError.samples, r.samples);
  assert.equal(m.devices[0].twinError!.meanM, r.meanM);

  // GT for an entity the twin never had: every row is "missing".
  const r2 = world.postGroundTruth(42, csvOf([{ tick: 5 * TICK_HZ, id: 99, cls: 0, pos: [0, 0, 0] }]))!;
  assert.equal(r2.meanM, MISSING_M);
  assert.equal(world.postGroundTruth(7, 'tick,id\n'), null);
  e.free();
});
