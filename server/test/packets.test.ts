import { test } from 'node:test';
import assert from 'node:assert/strict';
import { World, MAX_PACKETS } from '../src/world.js';
import { Shaper, UDP_IP_OVERHEAD } from '../src/shaper.js';
import type { PacketEvent } from '../src/types.js';
import { FakeClock, ScriptedEdge, wireAcks } from './fake.js';

const A = '10.0.0.5:50000', C = '10.0.0.9:6000';

function setup(rng: () => number = () => 0.5) {
  const clock = new FakeClock();
  const shaper = new Shaper({ now: clock.now, schedule: clock.schedule, cancel: clock.cancel, rng });
  const world = new World({ now: clock.now, shaper });
  return { clock, world, shaper };
}

test('packet events: every datagram up (shaper drops flagged) and every ack down, drained per snapshot', () => {
  let n = 0;
  const { clock, world, shaper } = setup(() => (n++ % 4 === 3 ? 0 : 0.99)); // every 4th datagram lost
  const e = new ScriptedEdge(world, clock, A, 42, 1);
  const acks = wireAcks(world, [e]);
  shaper.set({ enabled: true, loss: 0.2 });
  const t0 = clock.t;
  const pk: PacketEvent[] = [];
  for (let i = 0; i < 20; i++) { e.run(120); pk.push(...world.snapshot().packets); }
  assert.equal(world.snapshot().packets.length, 0, 'drained');
  const up = pk.filter(p => p.dir === 'up'), down = pk.filter(p => p.dir === 'down');
  assert.equal(up.length, e.sent);
  assert.equal(up.filter(p => p.dropped).length, shaper.counters.droppedLoss);
  assert.ok(shaper.counters.droppedLoss > 5);
  assert.equal(up.reduce((a, p) => a + p.bytes, 0), shaper.counters.offeredBytes, 'bytes include 28 B UDP/IP');
  assert.ok(up.every(p => p.key === 'id:42'), 'Hello names its device; later datagrams by address');
  assert.equal(up[0].kind, 'hello');
  assert.ok(up.some(p => p.kind === 'delta') && up.some(p => p.kind === 'keyframe'));
  assert.ok(up.filter(p => p.kind === 'delta' || p.kind === 'keyframe').every(p => Number.isInteger(p.seq)));
  assert.equal(down.length, acks.length);
  assert.ok(down.every(p => p.kind === 'ack' && !p.dropped && p.key === 'id:42' && p.bytes > UDP_IP_OVERHEAD && p.seq === undefined));
  for (let i = 1; i < pk.length; i++) assert.ok(pk[i].t >= pk[i - 1].t && pk[i].t >= t0);
  e.free();
});

test('packet events: malformed and unattributed datagrams; queue capped, oldest dropped', () => {
  const { clock, world } = setup();
  const t0 = clock.t;
  for (let i = 0; i < MAX_PACKETS + 500; i++) { world.ingest(C, new Uint8Array([9, 9, i & 255])); clock.advance(1); }
  const p = world.snapshot().packets;
  assert.equal(p.length, MAX_PACKETS);
  assert.equal(p[0].t, t0 + 500);
  assert.deepEqual([p[0].kind, p[0].key, p[0].dir, p[0].dropped, p[0].bytes], ['malformed', '', 'up', false, 3 + UDP_IP_OVERHEAD]);
});
