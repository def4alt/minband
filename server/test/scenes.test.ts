import { test } from 'node:test';
import assert from 'node:assert/strict';
import { SPREAD_M, sharedScene, spreadCentre, spreadScene } from '../src/scenes.js';
import { World } from '../src/world.js';
import { Shaper } from '../src/shaper.js';
import type { PacketEvent } from '../src/types.js';
import { FakeClock, ScriptedEdge, wireAcks } from './fake.js';

test('spread: each device walks its own area; areas never overlap', () => {
  const n = 8;
  const c = Array.from({ length: n }, (_, d) => spreadCentre(d, n));
  for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) assert.ok(Math.hypot(c[i][0] - c[j][0], c[i][1] - c[j][1]) >= SPREAD_M);
  for (let d = 0; d < n; d++) {
    const s = spreadScene(d, n, 2);
    for (let tick = 0; tick < 120 * 60; tick++) {
      const tr = s(tick);
      assert.deepEqual(tr.map(t => t.id), [1, 2]);
      for (const t of tr) assert.ok(Math.abs(t.pos[0] - c[d][0]) < SPREAD_M / 2 && Math.abs(t.pos[2] - c[d][1]) < SPREAD_M / 2, `device ${d} at ${t.pos}`);
    }
  }
  assert.deepEqual(sharedScene(100)(0).map(t => t.id), [1, 2, 3, 4]); // fusion demo unchanged
});

test('spread: N devices are N independent one-walker feeds at about the eval one_walker rate', () => {
  const clock = new FakeClock();
  const world = new World({ now: clock.now, shaper: new Shaper({ now: clock.now, schedule: clock.schedule, cancel: clock.cancel }) });
  const n = 4, secs = 60;
  const edges = Array.from({ length: n }, (_, d) => new ScriptedEdge(world, clock, `10.0.0.${d + 1}:5000`, 100 + d, d + 1, spreadScene(d, n)));
  wireAcks(world, edges);
  const pk: PacketEvent[] = [];
  for (let i = 0; i < secs * 120; i++) {
    for (const e of edges) { e.run(1); clock.advance(-1000 / 120); }
    clock.advance(1000 / 120);
    if (i % 4 === 0) pk.push(...world.snapshot().packets);
  }
  const s = world.snapshot();
  assert.deepEqual(s.devices.map(d => d.key).sort(), ['id:100', 'id:101', 'id:102', 'id:103']);
  assert.equal(s.global.length, n, 'no cross-device merges');
  for (let d = 0; d < n; d++) {
    const bps = pk.filter(p => p.dir === 'up' && p.key === `id:${100 + d}`).reduce((a, p) => a + p.bytes, 0) / secs;
    assert.ok(bps > 90 && bps < 170, `device ${100 + d}: ${bps.toFixed(1)} B/s (eval one_walker: 118.5)`);
  }
  edges.forEach(e => e.free());
});
