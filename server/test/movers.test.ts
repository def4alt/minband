import { test } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import type { AddressInfo } from 'node:net';
import { Motion, headingOf, moversFeed, MOVE_WINDOW_MS, STILL_MS, MIN_AGE_MS, GHOST_MS, MAX_EXTRAPOLATE_MS } from '../src/movers.js';
import type { FusedEntity } from '../src/fusion.js';
import { World } from '../src/world.js';
import { Shaper } from '../src/shaper.js';
import { createApi } from '../src/http.js';
import { FakeClock, ScriptedEdge, wireAcks, type Track } from './fake.js';

const ent = (gid: string, pos: [number, number, number], vel: [number, number, number] = [0, 0, 0], o: Partial<FusedEntity> = {}): FusedEntity =>
  ({ gid, class: 0, pos, vel, sources: [{ deviceId: 1, id: 1 }], stale: false, ce: 0.15, coasting: false, ...o });

/** Drive `m` at 30 Hz from `from` to `to` with a scene function of time (ms); return the last output. */
function run(m: Motion, from: number, to: number, scene: (t: number) => FusedEntity[], eyeOf?: (e: FusedEntity) => [number, number, number] | null) {
  let out = m.update(scene(from), from, eyeOf);
  for (let t = from + 33; t <= to; t += 33) out = m.update(scene(t), t, eyeOf);
  return out;
}

test('a new entity starts static; a walker at 1 m/s is moving within a second and a half', () => {
  const m = new Motion();
  const walker = (t: number) => [ent('w', [t / 1000, 0, 0], [1, 0, 0])];
  assert.equal(m.update(walker(0), 0)[0].moving, false);
  assert.equal(run(m, 33, MIN_AGE_MS - 33, walker)[0].moving, false, 'not yet: not judged before MIN_AGE_MS');
  assert.equal(run(m, MIN_AGE_MS, MIN_AGE_MS + 600, walker)[0].moving, true);
});

test('speed alone is not motion: a seated person born with 0.7 m/s and a jittering position never moves', () => {
  const seated = (t: number) => [ent('s', [0.2 * (Math.floor(t / 140) % 3 - 1), 0, -2], [0.7, 0, 0])];
  assert.equal(run(new Motion(), 0, 6000, seated)[0].moving, false);
});

test('a two-hit flicker that stops is never moving', () => {
  // Born from two hits, carries 1.2 m/s for the edge tracker's 1 s coast, then stands with zero
  // velocity until it dies: never a mover.
  const flicker = (t: number) => t < 1000
    ? [ent('f', [1.2 * t / 1000, 0, 0], [1.2, 0, 0])]
    : [ent('f', [1.2, 0, 0], [0, 0, 0])];
  assert.equal(run(new Motion(), 0, 2000, flicker)[0].moving, false);
});

test('a slow drift of 0.5 m within 2 s is moving, below the speed rule', () => {
  const m = new Motion();
  // 0.3 m/s, reported a little low (the tracker lags) and under SPEED_MIN: displacement must
  // catch it (the robust positions trail the raw ones by up to half a second).
  const drift = (t: number) => [ent('d', [0.3 * t / 1000, 0, 0], [0.25, 0, 0])];
  assert.equal(run(m, 0, 1500, drift)[0].moving, false);
  assert.equal(run(m, 1533, MOVE_WINDOW_MS + 700, drift)[0].moving, true);
});

test('a step with no speed behind it (a lift flip) is not motion; with speed it is; a chair needs a metre', () => {
  const flip = (t: number) => [ent('f', [t < 1000 ? 0 : 0.7, 0, 0], [0.1, 0, 0])];
  assert.equal(run(new Motion(), 0, 3000, flip)[0].moving, false);
  const step = (t: number) => [ent('s', [t < 1000 ? 0 : 0.7, 0, 0], [0.3, 0, 0])];
  assert.equal(run(new Motion(), 0, 2500, step)[0].moving, true);
  // 6 m from the device the lift's range flips by 0.8 m along the ray: not motion there; a
  // walker crossing the view covers the distance across the ray; one walking straight at the
  // device with the same poor range is not seen (the along rule needs 0.96 m).
  const eye = (): [number, number, number] => [0, 1.3, 0];
  const far = (t: number) => [ent('r', [0, 0, Math.floor(t / 400) % 2 === 0 ? -6 : -6.8], [0, 0, 0.3])];
  assert.equal(run(new Motion(), 0, 4000, far, eye)[0].moving, false);
  const walker = (t: number) => [ent('k', [0.8 * t / 1000, 0, -6], [0.3, 0, 0])];
  assert.equal(run(new Motion(), 0, 3000, walker, eye)[0].moving, true);
  const closer = (t: number) => [ent('c', [0, 0, -6 + 0.3 * t / 1000], [0, 0, 0.3])];
  assert.equal(run(new Motion(), 0, 3000, closer, eye)[0].moving, false);
  const chair = (t: number) => [ent('c', [t < 1000 ? 0 : 0.8, 0, 0], [0.3, 0, 0], { class: 56 })];
  assert.equal(run(new Motion(), 0, 3000, chair)[0].moving, false);
});

test('a chair with 10 cm of tracker jitter never moves; a stopped walker goes static after STILL_MS', () => {
  const m = new Motion();
  const jitter = (t: number) => 0.1 * Math.sin(t / 97);
  const chair = (t: number) => [ent('c', [1 + jitter(t), 0, 1 - jitter(t * 1.3)], [0.05, 0, -0.05], { class: 56 })];
  assert.equal(run(m, 0, 10_000, chair)[0].moving, false);

  const m2 = new Motion();
  const walkThenStop = (t: number) => t < 3000
    ? [ent('w', [t / 1000, 0, 0], [1, 0, 0])]
    : [ent('w', [3 + jitter(t), 0, 0], [0, 0, 0])];
  assert.equal(run(m2, 0, 3000, walkThenStop)[0].moving, true);
  // The STILL_MS clock starts when the robust position enters the 0.3 m ball around the stop,
  // which trails the halt by up to a second.
  assert.equal(run(m2, 3033, 3000 + STILL_MS - 400, walkThenStop)[0].moving, true, 'a pause keeps the mover');
  assert.equal(run(m2, 3000 + STILL_MS - 367, 3000 + STILL_MS + 1200, walkThenStop)[0].moving, false);
  // Walks off again: moving within the window.
  const again = (t: number) => [ent('w', [3 + (t - 9300) / 1000, 0, 0], [1, 0, 0])];
  assert.equal(run(m2, 9300, 9300 + 1600, again)[0].moving, true);
});

test('a new gid beside a mover inherits moving; not after GHOST_MS, not 3 m away', () => {
  const m = new Motion();
  const walker = (t: number) => [ent('w', [t / 1000, 0, -3], [1, 0, 0])];
  assert.equal(run(m, 0, 3000, walker)[0].moving, true);
  run(m, 3033, 4000, () => []);
  const reborn = (t: number) => [ent('r', [3.2, 0, -3], [0, 0, 0]), ent('x', [6.5, 0, -3], [0, 0, 0])];
  const out = m.update(reborn(4033), 4033);
  assert.equal(out[0].moving, true, 'reborn 0.7 m from the lost mover');
  assert.equal(out[1].moving, false, '3 m away');
  const m2 = new Motion();
  assert.equal(run(m2, 0, 3000, walker)[0].moving, true);
  run(m2, 3033, 3000 + GHOST_MS + 500, () => []);
  assert.equal(m2.update(reborn(6000), 6000)[0].moving, false, 'gone too long');
});

test('a scene dragged along with the device is not moving; the device really walking past still people is not either', () => {
  const m = new Motion();
  let out: ReturnType<Motion['update']> = [];
  for (let t = 0; t <= 4000; t += 33) {
    const d = t / 1000;
    out = m.update([ent('a', [0.5 + d, 0, -3], [1, 0, 0]), ent('b', [-0.5 + d, 0, -2.5], [1, 0, 0]), ent('c', [1.5 + d, 0, -4], [1, 0, 0]), ent('d', [-1.5 + d, 0, -3.5], [1, 0, 0]), ent('e', [2.5 + d, 0, -3], [1, 0, 0])], t, () => [d, 1.3, 0]);
  }
  assert.ok(out.every(r => !r.moving), 'dragged scene');
  const m2 = new Motion();
  for (let t = 0; t <= 4000; t += 33) {
    const d = t / 1000;
    out = m2.update([ent('a', [0.5, 0, -3], [0, 0, 0]), ent('b', [-0.5, 0, -2.5], [0, 0, 0]), ent('c', [1.5, 0, -4], [0, 0, 0]), ent('d', [-1.5, 0, -3.5], [0, 0, 0]), ent('e', [2.5, 0, -3], [0, 0, 0])], t, () => [d, 1.3, 0]);
  }
  assert.ok(out.every(r => !r.moving), 'device walking past');
});

test('heading: 0 along -Z, 90 along +X, held while slow, null until known; forgotten entities are dropped', () => {
  assert.equal(headingOf([0, 0, -1]), 0);
  assert.equal(headingOf([1, 0, 0]), 90);
  assert.equal(headingOf([0, 0, 1]), 180);
  assert.equal(headingOf([-1, 0, 0]), 270);
  assert.equal(headingOf([0.1, 0, -0.1]), null);
  const m = new Motion();
  let out = m.update([ent('a', [0, 0, 0], [0, 0, 0]), ent('b', [5, 0, 0], [0, 0, -1])], 0);
  assert.equal(out[0].headingDeg, null);
  assert.equal(out[1].headingDeg, 0);
  out = m.update([ent('b', [5, 0, -1], [0.1, 0, -0.1])], 1000);
  assert.equal(out.length, 1);
  assert.equal(out[0].headingDeg, 0, 'held from the last fast sample');
  assert.equal(m.size, 2, 'entity a kept for KEEP_MS');
  m.update([ent('b', [5, 0, -1], [0.1, 0, -0.1])], 2100);
  assert.equal(m.size, 1, 'entity a forgotten');
});

test('moversFeed: only movers unless all, dead reckoning capped, names from the class', () => {
  const g = (gid: string, moving: boolean, pos: [number, number, number], vel: [number, number, number]) =>
    ({ ...ent(gid, pos, vel), moving, headingDeg: 90 });
  const snap = { t: 10_000, global: [g('g1', true, [1, 0, -2], [1, 0, 0]), g('g2', false, [0, 0, 0], [0, 0, 0])] };
  const label = (c: number) => (c === 0 ? 'dismount' : `class ${c}`);
  let f = moversFeed(snap, 10_500, false, label);
  assert.deepEqual(f.movers.map(r => r.gid), ['g1']);
  assert.deepEqual(f.movers[0].pos, [1.5, 0, -2]);
  assert.equal(f.movers[0].name, 'dismount');
  assert.equal(f.movers[0].speed, 1);
  assert.equal(f.t, 10_500);
  assert.equal(f.ageMs, 500);
  f = moversFeed(snap, 10_000 + MAX_EXTRAPOLATE_MS + 5000, true, label);
  assert.deepEqual(f.movers.map(r => r.gid), ['g1', 'g2']);
  assert.deepEqual(f.movers[0].pos, [2, 0, -2], 'capped at MAX_EXTRAPOLATE_MS');
  assert.equal(f.ageMs, MAX_EXTRAPOLATE_MS);
  f = moversFeed(null, 7, false, label);
  assert.deepEqual(f.movers, []);
  assert.equal(f.frame.up, '+Y');
});

test('end to end: the walker of the scripted scene is a mover with a heading, the chair is not; GET /api/movers', async () => {
  const clock = new FakeClock();
  const world = new World({ now: clock.now, shaper: new Shaper({ now: clock.now, schedule: clock.schedule, cancel: clock.cancel }) });
  const e = new ScriptedEdge(world, clock, '127.0.0.1:9000', 42, 1);
  wireAcks(world, [e]);
  e.run(240, { snapshotEvery: 4 }); // 8 s: walker on a 2 m circle at 1 m/s, static chair
  const global = world.lastSnapshot!.global;
  const walker = global.find(x => x.class === 0)!, chair = global.find(x => x.class === 56)!;
  assert.equal(walker.moving, true);
  assert.equal(typeof walker.headingDeg, 'number');
  assert.equal(chair.moving, false);
  assert.equal(chair.headingDeg, null);

  const server = http.createServer(createApi(world));
  await new Promise<void>(r => server.listen(0, '127.0.0.1', r));
  const base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  try {
    let r = await fetch(`${base}/api/movers`);
    assert.equal(r.status, 200);
    let j = await r.json();
    assert.equal(j.movers.length, 1);
    assert.equal(j.movers[0].class, 0);
    assert.equal(j.movers[0].name, 'dismount');
    assert.equal(j.frame.forward, '-Z');
    j = await (await fetch(`${base}/api/movers?all=1`)).json();
    assert.equal(j.movers.length, 2);
    r = await fetch(`${base}/api/movers?all=2`);
    assert.equal(r.status, 400);
    r = await fetch(`${base}/api/movers?frame=x`);
    assert.equal(r.status, 400);
  } finally {
    await new Promise(r => server.close(r));
    e.free();
  }
});

test('a static edge scene: nothing moves, the feed is empty, all=1 lists it', () => {
  const clock = new FakeClock();
  const world = new World({ now: clock.now, shaper: new Shaper({ now: clock.now, schedule: clock.schedule, cancel: clock.cancel }) });
  const parked = (): Track[] => [{ id: 1, class: 2, pos: [3, 0, -4], vel: [0, 0, 0], conf: 200 }];
  const e = new ScriptedEdge(world, clock, '127.0.0.1:9001', 7, 1, parked);
  wireAcks(world, [e]);
  e.run(300, { snapshotEvery: 4 });
  const feed = moversFeed(world.lastSnapshot, clock.now(), false, () => 'x');
  assert.deepEqual(feed.movers, []);
  assert.equal(moversFeed(world.lastSnapshot, clock.now(), true, () => 'x').movers.length, 1);
  e.free();
});
