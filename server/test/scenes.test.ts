import { test } from 'node:test';
import assert from 'node:assert/strict';
import { SPREAD_M, lookAt, sharedCamera, sharedScene, spreadCamera, spreadCentre, spreadScene, type CameraPose } from '../src/scenes.js';
import { World } from '../src/world.js';
import { Shaper } from '../src/shaper.js';
import type { PacketEvent } from '../src/types.js';
import { FakeClock, ScriptedEdge, wireAcks } from './fake.js';

/** Camera forward (-Z) rotated by the pose quaternion. */
function forward(q: number[]): number[] {
  const [x, y, z, w] = q, v = [0, 0, -1];
  const t = [2 * (y * v[2] - z * v[1]), 2 * (z * v[0] - x * v[2]), 2 * (x * v[1] - y * v[0])]; // 2 q x v
  return [v[0] + w * t[0] + y * t[2] - z * t[1], v[1] + w * t[1] + z * t[0] - x * t[2], v[2] + w * t[2] + x * t[1] - y * t[0]];
}
/** Distance from `p` to the camera's forward ray. */
function offRay(c: CameraPose, p: number[]): number {
  const f = forward(c.quat), d = [p[0] - c.pos[0], p[1] - c.pos[1], p[2] - c.pos[2]];
  const along = d[0] * f[0] + d[1] * f[1] + d[2] * f[2];
  return Math.hypot(d[0] - along * f[0], d[1] - along * f[1], d[2] - along * f[2]) + (along < 0 ? 1e9 : 0);
}

test('lookAt: the camera -Z axis points at the target, +Y stays up', () => {
  for (const [eye, target] of [[[6, 1.6, 0], [0, 0.5, 0]], [[-3, 6, 2], [1, 0.9, -4]], [[0, 2, -5], [0, 0, 0]], [[1, 1, 1], [1, 0, 3]]]) {
    const q = lookAt(eye, target);
    assert.ok(Math.abs(Math.hypot(...q) - 1) < 1e-9);
    assert.ok(offRay({ pos: eye as [number, number, number], quat: q }, target) < 1e-9, `${eye} -> ${target}`);
  }
});

test('cameras: one viewpoint per device, moving, looking at its walkers', () => {
  const n = 4;
  for (let d = 0; d < n; d++) {
    const sh = sharedCamera(d, n), sp = spreadCamera(d, n), [cx, cz] = spreadCentre(d, n);
    for (const tick of [0, 600, 6000]) {
      assert.ok(offRay(sh(tick), [0, 0.5, 0]) < 1e-6);
      assert.ok(offRay(sp(tick), [cx, 0.9, cz]) < 1e-6);
      assert.ok(Math.hypot(sp(tick).pos[0] - cx, sp(tick).pos[2] - cz) < SPREAD_M / 2, 'over its own area');
    }
    assert.ok(Math.hypot(sh(0).pos[0] - sh(1200).pos[0], sh(0).pos[2] - sh(1200).pos[2]) > 0.5, 'moves');
    if (d > 0) assert.ok(Math.hypot(sh(0).pos[0] - sharedCamera(0, n)(0).pos[0], sh(0).pos[2] - sharedCamera(0, n)(0).pos[2]) > 2, 'own viewpoint');
  }
});

test('sim pose: offered at 2 Hz, sent at the budget pose interval, shown as the device pose', () => {
  const clock = new FakeClock();
  const world = new World({ now: clock.now, shaper: new Shaper({ now: clock.now, schedule: clock.schedule, cancel: clock.cancel }) });
  const scene = spreadScene(0, 1), camera = spreadCamera(0, 1);
  const e = new ScriptedEdge(world, clock, '10.0.0.1:5000', 100, 1, scene);
  wireAcks(world, [e]);
  const poses: number[] = [];
  const run = (secs: number) => {
    for (let i = 0; i < secs * 120; i++) {
      const tick = e.tick;
      e.run(1);
      if (tick % 60 === 0) {
        const c = camera(tick);
        const dg = e.edge.pose(...c.pos, ...c.quat, true, tick);
        if (dg.length) { world.ingest(e.addr, dg); poses.push(clock.t); }
      }
      if (i % 4 === 0) world.snapshot();
    }
  };
  run(10);
  const p = world.snapshot().devices[0].pose!;
  assert.equal(p.originLocked, true);
  assert.ok(offRay({ pos: p.pos, quat: p.quat }, [0, 0.9, 0]) < 1e-3, 'frustum looks at the area');
  assert.ok(poses.length >= 18, `${poses.length} poses in 10 s at budget 0`);
  world.link.apply('external', 'lora'); // 1500 bit/s: one pose per 10 s
  run(5); poses.length = 0; run(30);
  assert.ok(poses.length >= 2 && poses.length <= 4, `${poses.length} poses in 30 s at 1500 bit/s`);
  e.free();
});

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

test('log scene: replays a track log, holds the latest row per tick, loops with fresh ids', async () => {
  const { logScene } = await import('../src/scenes.js');
  const { mkdtempSync, writeFileSync } = await import('node:fs');
  const { join } = await import('node:path'); const { tmpdir } = await import('node:os');
  const p = join(mkdtempSync(join(tmpdir(), 'minband-log-')), 'tracks.csv');
  writeFileSync(p, 'tick,id,class,x,y,z,vx,vy,vz,conf\n100,7,0,1,0,2,0.5,0,0,200\n100,8,2,10,0,-5,8,0,0,180\n104,7,0,1.02,0,2,0.5,0,0,200\n');
  const { scene, durationTicks } = logScene(p);
  assert.equal(durationTicks, 5);
  assert.deepEqual(scene(0).map(t => t.id), [7, 8]);
  assert.deepEqual(scene(3).map(t => t.id), [7, 8], 'held until the next logged tick');
  assert.deepEqual(scene(4).map(t => [t.id, t.pos[0]]), [[7, 1.02]]);
  assert.deepEqual(scene(5).map(t => t.id), [100_007, 100_008], 'second loop: fresh ids');
});
