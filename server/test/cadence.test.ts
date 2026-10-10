// Device liveness follows the heartbeat cadence of the budget each edge was told (S19, S15).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { describe } from 'minband-core';
import { World, DEVICE_TIMEOUT_MARGIN_MS } from '../src/world.js';
import { Shaper } from '../src/shaper.js';
import type { Snapshot } from '../src/types.js';
import { FakeClock, ScriptedEdge, wireAcks, type Track } from './fake.js';

const A = '10.0.0.5:50000', C = '10.0.0.9:6000';
/** A chair: nothing moves, so only keyframes (the heartbeat) and Hello refreshes are sent. */
const STATIC = (): Track[] => [{ id: 2, class: 56, pos: [-1, 0, 1], vel: [0, 0, 0], conf: 180 }];
const TELEMETRY = { keyframeMs: 15_000, helloRefreshMs: 30_000, poseMs: 10_000, coastMs: 18_750, staleMs: 45_000, dropMs: 75_000 };

function setup() {
  const clock = new FakeClock();
  const shaper = new Shaper({ now: clock.now, schedule: clock.schedule, cancel: clock.cancel, rng: () => 0.5 });
  return { clock, world: new World({ now: clock.now, shaper }) };
}

/** Run `secs` of edge ticks with a snapshot every 4 ticks (~30 Hz); calls `each` on every snapshot. */
function drive(e: ScriptedEdge, secs: number, each: (s: Snapshot) => void, deliver = true) {
  for (let i = 0; i < secs * 30; i++) { e.run(4, { deliver }); each(e.world.snapshot()); }
}

test('telemetry cadence: a static device holds between keyframes; a dead link coasts, goes stale, then is removed', () => {
  const { clock, world } = setup();
  const e = new ScriptedEdge(world, clock, A, 42, 1, STATIC);
  wireAcks(world, [e]);
  world.link.apply('external', 'telemetry'); // budget 450, shaper off
  drive(e, 20, () => {});
  assert.deepEqual(world.snapshot().devices[0].cadence, TELEMETRY);

  const kf: number[] = []; const bad: string[] = []; let minAir = Infinity;
  drive(e, 90, s => {
    const d = s.devices[0];
    if (d.silent || d.coasting || d.entities.some(x => x.stale || x.coasting)) bad.push(`${s.t}`);
    if (d.entities.length !== 1) bad.push(`entities ${d.entities.length}`);
    kf.push(...s.packets.filter(p => p.kind === 'keyframe').map(p => p.t));
    if (s.t - kf[0] > 30_000) minAir = Math.min(minAir, s.link.airtimeShare); // once the window has filled
  });
  assert.deepEqual(bad, [], 'never stale or coasting while the heartbeat arrives');
  const gaps = kf.slice(1).map((t, i) => t - kf[i]);
  assert.ok(gaps.length >= 5 && gaps.every(g => Math.abs(g - 15_000) <= 20), `keyframe gaps ${gaps}`);
  assert.ok(minAir > 0 && minAir < Infinity, 'airtime averages over two heartbeats, so it never reads 0 between keyframes');

  // Link dead: coasting after one missed keyframe (18.75 s), stale at 45 s, removed at 75 + 10 s.
  const last = world.device(42)!.lastSeenMs;
  let coastAt = 0, silentAt = 0, goneAt = 0;
  drive(e, 100, s => {
    const d = s.devices[0];
    if (!d) { goneAt ||= s.t - last; return; }
    if (d.coasting && !coastAt) coastAt = s.t - last;
    if (d.silent && !silentAt) { silentAt = s.t - last; assert.ok(d.entities.every(x => x.stale && x.coasting)); }
  }, false);
  assert.ok(Math.abs(coastAt - TELEMETRY.coastMs) <= 50, `coasting after ${coastAt} ms`);
  assert.ok(Math.abs(silentAt - TELEMETRY.staleMs) <= 50, `silent after ${silentAt} ms`);
  assert.ok(Math.abs(goneAt - TELEMETRY.dropMs - DEVICE_TIMEOUT_MARGIN_MS) <= 50, `removed after ${goneAt} ms`);
  e.free();
});

test('budget back to 0 restores 2 s keyframes; the old cadence holds until the edge has heard', () => {
  const { clock, world } = setup();
  const e = new ScriptedEdge(world, clock, A, 42, 1, STATIC);
  let k0 = Infinity; // edge keyframe count when the budget went back to 0
  world.onAck = (addr, ack) => {
    // The ack after the first keyframe at the new budget is lost: the edge keeps its 15 s period once more.
    if (JSON.parse(e.edge.stats_json()).keyframes < k0 + 2) return;
    e.onAck(addr, ack);
  };
  k0 = -Infinity;
  world.link.apply('external', 'telemetry');
  drive(e, 20, () => {});
  world.link.apply('clean');
  k0 = JSON.parse(e.edge.stats_json()).keyframes;
  const kf: number[] = []; const bad: number[] = [];
  drive(e, 40, s => {
    const d = s.devices[0];
    if (d.silent || d.entities.some(x => x.stale)) bad.push(s.t);
    kf.push(...s.packets.filter(p => p.kind === 'keyframe').map(p => p.t));
  });
  assert.deepEqual(bad, [], 'not stale while the edge still runs the old cadence');
  const gaps = kf.slice(1).map((t, i) => t - kf[i]);
  assert.ok(gaps.slice(0, 1).every(g => g > 10_000), `first gap still slow: ${gaps}`);
  assert.ok(gaps.slice(-5).every(g => Math.abs(g - 2_000) <= 20), `then 2 s again: ${gaps}`);
  assert.equal(world.snapshot().devices[0].cadence.keyframeMs, 2_000);
  e.free();
});

test('the budget split counts quiet devices by their own cadence', () => {
  const { clock, world } = setup();
  const a = new ScriptedEdge(world, clock, A, 42, 1, STATIC), c = new ScriptedEdge(world, clock, C, 43, 2, STATIC);
  const told: Record<string, number> = {};
  world.onAck = (addr, ack) => {
    told[addr] = Number(/budget_bps: (\d+)/.exec(describe(ack))![1]);
    a.onAck(addr, ack); c.onAck(addr, ack);
  };
  world.link.apply('external', 'telemetry');
  for (let i = 0; i < 120 * 60; i++) { a.run(1); c.run(1); clock.advance(-1000 / 120); }
  // Both send one keyframe per 15 s, far longer than the old fixed 5 s liveness window.
  assert.deepEqual([told[A], told[C], world.edgeBudget()], [225, 225, 225]);
  a.free(); c.free();
});
