import { test } from 'node:test';
import assert from 'node:assert/strict';
import { Fusion, MERGE_MS, SPLIT_MS } from '../src/fusion.js';
import type { DeviceView, EntityView } from '../src/types.js';

const ent = (id: number, x: number, cls = 0, vx = 0, o: Partial<EntityView> = {}): EntityView => ({ id, class: cls, pos: [x, 0, 0], vel: [vx, 0, 0], conf: 200, tick: 0, age: 0, stale: false, theta: 0.15, coasting: false, ce: 0.15, ...o });
const CADENCE_0 = { keyframeMs: 2000, helloRefreshMs: 5000, poseMs: 500, coastMs: 2500, staleMs: 6000, dropMs: 10000 };
const dev = (deviceId: number, entities: EntityView[], key = `id:${deviceId}`): DeviceView => ({
  deviceId, addr: '', entities, pose: null, bps: 0, msgsPerSec: 0, stats: {}, lastSeenMs: 0,
  key, provisional: false, offeredBps: 0, edgeTick: 0, silent: false, addrChanges: 0, clockOffsetMs: null,
  airtimeShare: 0, cadence: CADENCE_0, coasting: false,
});
/** Run `f.update` every 33 ms from `from` to `to` (inclusive) with a fixed scene; return the last output. */
function run(f: Fusion, from: number, to: number, devices: () => DeviceView[]) {
  let out = f.update(devices(), from);
  for (let t = from + 33; t <= to; t += 33) out = f.update(devices(), t);
  return out;
}

test('merge after 1 s close, not before', () => {
  const f = new Fusion();
  const scene = () => [dev(1, [ent(1, 0)]), dev(2, [ent(7, 0.3)])];
  assert.equal(run(f, 0, MERGE_MS - 34, scene).length, 2);
  const out = f.update(scene(), MERGE_MS);
  assert.equal(out.length, 1);
  assert.deepEqual(out[0].sources.map(s => s.deviceId).sort(), [1, 2]);
  assert.ok(Math.abs(out[0].pos[0] - 0.15) < 1e-9); // equal confidence: midpoint
});

test('no merge for different class or velocity', () => {
  const f = new Fusion();
  assert.equal(run(f, 0, 3000, () => [dev(1, [ent(1, 0, 0)]), dev(2, [ent(1, 0.1, 56)])]).length, 2);
  assert.equal(run(f, 3000, 6000, () => [dev(1, [ent(1, 0, 0, 0)]), dev(2, [ent(1, 0.1, 0, 1)])]).length, 2);
});

test('split after 1 s far, not before', () => {
  const f = new Fusion();
  run(f, 0, 1100, () => [dev(1, [ent(1, 0)]), dev(2, [ent(7, 0.3)])]);
  const far = () => [dev(1, [ent(1, 0)]), dev(2, [ent(7, 1.5)])];
  assert.equal(run(f, 1200, 1200 + SPLIT_MS - 1, far).length, 1);
  assert.equal(f.update(far(), 1200 + SPLIT_MS).length, 2);
  // Between SPLIT_DIST and MERGE_DIST nothing changes (hysteresis band).
  const f2 = new Fusion();
  run(f2, 0, 1100, () => [dev(1, [ent(1, 0)]), dev(2, [ent(7, 0.3)])]);
  assert.equal(run(f2, 1200, 6000, () => [dev(1, [ent(1, 0)]), dev(2, [ent(7, 0.8)])]).length, 1);
});

test('never merges two tracks of the same device, also not transitively', () => {
  const f = new Fusion();
  assert.equal(run(f, 0, 3000, () => [dev(1, [ent(1, 0), ent(2, 0.1)])]).length, 2);
  // a1 and a2 from device 1 are both close to b from device 2: b may join one of them only.
  const f2 = new Fusion();
  const out = run(f2, 0, 5000, () => [dev(1, [ent(1, 0), ent(2, 0.2)]), dev(2, [ent(9, 0.1)])]);
  assert.equal(out.length, 2);
  for (const g of out) {
    const devs = g.sources.map(s => s.deviceId);
    assert.equal(new Set(devs).size, devs.length, `group ${g.gid} has two tracks of one device`);
  }
  // Same for a third device joining: still one track per device per group.
  const f3 = new Fusion();
  const out3 = run(f3, 0, 5000, () => [dev(1, [ent(1, 0), ent(2, 0.2)]), dev(2, [ent(9, 0.1)]), dev(3, [ent(4, 0.15)])]);
  for (const g of out3) { const d = g.sources.map(s => s.deviceId); assert.equal(new Set(d).size, d.length); }
});

test('devices are told apart by key, not deviceId (provisional devices share id 0)', () => {
  const f = new Fusion();
  const out = run(f, 0, 2000, () => [dev(0, [ent(1, 0)], 'addr:10.0.0.2:5000'), dev(0, [ent(1, 0.2)], 'addr:10.0.0.3:5000')]);
  assert.equal(out.length, 1);
  assert.equal(out[0].sources.length, 2);
});

test('disable toggle unmerges at once; re-enable needs a fresh 1 s', () => {
  const f = new Fusion();
  const scene = () => [dev(1, [ent(1, 0)]), dev(2, [ent(7, 0.3)])];
  assert.equal(run(f, 0, 1100, scene).length, 1);
  f.enabled = false;
  assert.equal(f.update(scene(), 1133).length, 2);
  assert.equal(run(f, 1166, 3000, scene).length, 2);
  assert.equal(f.pendingTimers, 0);
  f.enabled = true;
  assert.equal(run(f, 3033, 3033 + MERGE_MS - 1, scene).length, 2);
  assert.equal(f.update(scene(), 3033 + MERGE_MS).length, 1);
});

test('disable during the merge countdown does not leave a stale timer', () => {
  const f = new Fusion();
  const scene = () => [dev(1, [ent(1, 0)]), dev(2, [ent(7, 0.3)])];
  run(f, 0, 800, scene);
  f.enabled = false; run(f, 833, 5000, scene);
  f.enabled = true;
  assert.equal(f.update(scene(), 5033).length, 2, 'merged instantly from a stale timer');
});

test('hysteresis restarts when a track drops out briefly', () => {
  const f = new Fusion();
  run(f, 0, 800, () => [dev(1, [ent(1, 0)]), dev(2, [ent(7, 0.3)])]);
  f.update([dev(1, [ent(1, 0)]), dev(2, [])], 833);
  assert.equal(f.update([dev(1, [ent(1, 0)]), dev(2, [ent(7, 0.3)])], 1500).length, 2);
});

test('a member that changes class splits after 1 s', () => {
  const f = new Fusion();
  run(f, 0, 1100, () => [dev(1, [ent(1, 0)]), dev(2, [ent(7, 0.3)])]);
  const recls = () => [dev(1, [ent(1, 0)]), dev(2, [ent(7, 0.3, 56)])];
  assert.equal(run(f, 1133, 1133 + SPLIT_MS - 1, recls).length, 1);
  assert.equal(f.update(recls(), 1133 + SPLIT_MS).length, 2);
});

test('fused ce is the best fresh source; coasting only when every source coasts', () => {
  const f = new Fusion();
  const scene = (a: Partial<EntityView>, b: Partial<EntityView>) => () => [dev(1, [ent(1, 0, 0, 0, a)]), dev(2, [ent(7, 0.3, 0, 0, b)])];
  let g = run(f, 0, 1100, scene({ ce: 0.3 }, { ce: 0.2 }));
  assert.equal(g.length, 1);
  assert.deepEqual([g[0].ce, g[0].coasting], [0.2, false]);
  g = run(f, 1133, 1200, scene({ ce: 0.3 }, { ce: 1.5, coasting: true }));
  assert.deepEqual([g[0].ce, g[0].coasting], [0.3, false], 'one source on its heartbeat vouches for it');
  g = run(f, 1233, 1300, scene({ ce: 0.3, stale: true }, { ce: 1.5, coasting: true }));
  assert.deepEqual([g[0].ce, g[0].coasting], [1.5, false], 'a stale source does not lower ce');
  g = run(f, 1333, 1400, scene({ ce: 2.5, stale: true, coasting: true }, { ce: 4, stale: true, coasting: true }));
  assert.deepEqual([g[0].ce, g[0].coasting, g[0].stale], [2.5, true, true], 'all stale: best of all');
});

test('timers do not leak as tracks come and go', () => {
  const f = new Fusion();
  for (let i = 0; i < 200; i++) f.update([dev(1, [ent(i, 0)]), dev(2, [ent(1000 + i, 0.2)])], i * 33);
  assert.ok(f.pendingTimers <= 1, `pending ${f.pendingTimers}`);
});
