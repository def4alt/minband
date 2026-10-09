import { test } from 'node:test';
import assert from 'node:assert/strict';
import { describe } from 'minband-core';
import { World } from '../src/world.js';
import { Shaper, UDP_IP_OVERHEAD } from '../src/shaper.js';
import { LORA_LONGFAST, airtimeMs, serialModel } from '../src/link.js';
import type { AirtimeModel, PacketEvent } from '../src/types.js';
import { FakeClock, ScriptedEdge, wireAcks } from './fake.js';

const A = '10.0.0.5:50000', C = '10.0.0.9:6000';

function setup(rng: () => number = () => 0.5) {
  const clock = new FakeClock();
  const shaper = new Shaper({ now: clock.now, schedule: clock.schedule, cancel: clock.cancel, rng });
  const world = new World({ now: clock.now, shaper, link: { schedule: clock.schedule, cancel: clock.cancel, rng: () => 0.5 } });
  return { clock, world, shaper };
}

test('airtime share per device and in total, from delivered datagrams under the current model', () => {
  const { clock, world } = setup();
  const a = new ScriptedEdge(world, clock, A, 42, 1), c = new ScriptedEdge(world, clock, C, 43, 2);
  wireAcks(world, [a, c]);
  const pk: PacketEvent[] = [];
  world.link.apply('external', 'lora'); // shaper off: everything is delivered
  for (let i = 0; i < 600; i++) { a.run(1); c.run(1); clock.advance(-1000 / 120); if (i % 4 === 0) pk.push(...world.snapshot().packets); }
  pk.push(...world.snapshot().packets);
  const now = clock.t;
  // Averaged over two heartbeats: lora's 1500 bit/s split in two is 750 each, keyframes every 11.67 s.
  const w42 = world.device(42)!.airWindowMs, w43 = world.device(43)!.airWindowMs, wd = Math.max(w42, w43);
  const inWin = (f: (p: PacketEvent) => boolean, w: number) => pk.filter(p => f(p) && now - p.t < w);
  const share = (m: AirtimeModel, f: (p: PacketEvent) => boolean, w: number) =>
    inWin(f, w).reduce((s, p) => s + airtimeMs(m, p.bytes - UDP_IP_OVERHEAD), 0) / w;
  const up = (id: number) => (p: PacketEvent) => p.dir === 'up' && p.key === `id:${id}`;
  const snap = world.snapshot();
  const d42 = snap.devices.find(d => d.deviceId === 42)!, d43 = snap.devices.find(d => d.deviceId === 43)!;
  assert.equal(d42.cadence.keyframeMs, 1400 / 120 * 1000);
  assert.equal(w42, 2 * d42.cadence.keyframeMs);
  const e42 = share(LORA_LONGFAST, up(42), w42);
  assert.ok(e42 > 0.1, `share ${e42}`);
  assert.ok(Math.abs(d42.airtimeShare - e42) < 1e-9, `${d42.airtimeShare} vs ${e42}`);
  assert.ok(Math.abs(d43.airtimeShare - share(LORA_LONGFAST, up(43), w43)) < 1e-9);
  assert.ok(Math.abs(snap.link.airtimeShare - d42.airtimeShare - d43.airtimeShare) < 1e-12);
  const msgs = inWin(up(42), w42).length * 1000 / w42 + inWin(up(43), w43).length * 1000 / w43;
  assert.ok(Math.abs(snap.link.msgsPerSec - msgs) < 1e-9);
  assert.ok(Math.abs(snap.link.downAirtimeShare! - share(LORA_LONGFAST, p => p.dir === 'down', wd)) < 1e-9);
  assert.ok(snap.link.downMsgsPerSec! > 0);
  assert.deepEqual([snap.link.profile, snap.link.as, snap.link.model], ['external', 'lora', LORA_LONGFAST]);
  // A new model applies to the same window at once; no model, no airtime.
  world.link.apply('external', 'hf');
  assert.ok(Math.abs(world.linkView().airtimeShare - share(serialModel(9_600), up(42), w42) - share(serialModel(9_600), up(43), w43)) < 1e-9);
  world.link.apply('clean');
  const v = world.snapshot();
  assert.deepEqual([v.link.airtimeShare, v.devices[0].airtimeShare, v.link.downAirtimeShare], [0, 0, 0]);
  assert.ok(v.link.msgsPerSec > 0);
  a.free(); c.free();
});

test('the link budget is split over the live devices in each ack', () => {
  const { clock, world } = setup();
  const a = new ScriptedEdge(world, clock, A, 42, 1), c = new ScriptedEdge(world, clock, C, 43, 2);
  const told: Record<string, number[]> = { [A]: [], [C]: [] };
  world.onAck = (addr, ack) => {
    told[addr].push(Number(/budget_bps: (\d+)/.exec(describe(ack))![1]));
    a.onAck(addr, ack); c.onAck(addr, ack);
  };
  world.link.apply('external', 'hf'); // 8000 bit/s for the whole link
  a.run(240);
  assert.equal(told[A].at(-1), 8_000, 'one device gets it all');
  for (let i = 0; i < 240; i++) { a.run(1); c.run(1); clock.advance(-1000 / 120); }
  assert.deepEqual([told[A].at(-1), told[C].at(-1), world.edgeBudget()], [4_000, 4_000, 4_000]);
  c.run(120 * 10, { deliver: false }); // c quiet for 10 s, past its 9 s stale period at 4000 bit/s (a too: the clock moved)
  a.run(120);
  assert.equal(told[A].at(-1), 8_000, 'a silent device gives its share back');
  world.link.apply('clean');
  a.run(120);
  assert.equal(told[A].at(-1), 0);
  a.free(); c.free();
});

test('airtime counts delivered datagrams only; the profile sets what the edge is told', () => {
  const { clock, world, shaper } = setup(() => 0); // every loss draw drops
  const e = new ScriptedEdge(world, clock, A, 42, 1);
  wireAcks(world, [e]);
  e.run(600);
  world.link.apply('blackout');
  assert.equal(world.budgetBps, 0);
  e.run(600);
  assert.equal(world.snapshot().link.airtimeShare, 0);
  assert.ok(shaper.counters.droppedLoss > 0);
  world.link.apply('hf');
  assert.equal(world.budgetBps, 8_000);
  e.free();
});
