import { test } from 'node:test';
import assert from 'node:assert/strict';
import { WasmEdge } from 'minband-core';
import { World, DEVICE_SILENT_MS, DEVICE_TIMEOUT_MS } from '../src/world.js';
import { Shaper } from '../src/shaper.js';
import { FakeClock, ScriptedEdge, chatty, unpack, wireAcks } from './fake.js';

const A = '10.0.0.5:50000', B = '10.0.0.5:50001', C = '10.0.0.9:6000';

function setup() {
  const clock = new FakeClock();
  const shaper = new Shaper({ now: clock.now, schedule: clock.schedule, cancel: clock.cancel, rng: () => 0.5 });
  const world = new World({ now: clock.now, shaper });
  return { clock, world, shaper };
}

/** The first datagram a fresh edge emits is its Hello. */
function helloFrom(deviceId: number, nonce: number, tick = 0): Uint8Array {
  const e = new WasmEdge(deviceId, nonce);
  const [h] = unpack(e.tick('[]', tick)); e.free();
  return h;
}

test('keyed by device_id after Hello; a Hello from a new address migrates the same Device', () => {
  const { clock, world } = setup();
  const e = new ScriptedEdge(world, clock, A, 42, 1);
  const acks = wireAcks(world, [e]);
  e.run(240);
  assert.deepEqual([...world.devices.keys()], ['id:42']);
  const dev = world.device(42)!;
  assert.equal(dev.addr, A);
  assert.equal(world.snapshot().devices[0].entities.length, 2);
  const rx = dev.rx;

  // Wi-Fi hiccup: the phone comes back on a new port and says Hello (same session).
  e.addr = B;
  world.ingest(B, helloFrom(42, 1, e.tick));
  assert.equal(world.devices.size, 1);
  assert.equal(world.device(42), dev, 'same Device object');
  assert.equal(dev.rx, rx, 'receiver state kept for the same session');
  assert.equal(dev.addr, B);
  assert.deepEqual([...dev.aliases], [A]);
  assert.equal(dev.addrChanges, 1);
  assert.equal(world.snapshot().devices[0].entities.length, 2, 'entities survive the move');

  acks.length = 0;
  e.run(360);
  assert.ok(acks.length >= 2, `acks ${acks.length}`);
  assert.ok(acks.every(a => a.addr === B), 'acks go to the latest address');

  // A late datagram from the old port is still this device's, and the ack still goes to B.
  acks.length = 0;
  const late = unpack(e.edge.tick(JSON.stringify([]), e.tick)).filter(d => d[1] !== 0); // despawns: a real Delta (drop a Hello refresh)
  clock.advance(200);
  for (const dg of late) world.ingest(A, dg);
  assert.equal(world.devices.size, 1);
  assert.ok(acks.length >= 1 && acks.every(a => a.addr === B));
  e.free();
});

test('provisional (keyed by address) until its Hello is seen; the 5 s Hello refresh identifies it', () => {
  const { clock, world } = setup();
  // An edge acked by a previous server run: its first Hello went to the old server (not delivered),
  // and it refreshes Hello every 5 s so a restarted server can re-identify it.
  const e = new ScriptedEdge(world, clock, C, 9, 5);
  wireAcks(world, [e]);
  e.run(1, { deliver: false }); // Hello to the old server
  e.edge.on_datagram(new Uint8Array([0, 4, 0, 0, 0])); // Ack { last_seq: 0 } from the old server
  e.run(300);
  assert.deepEqual([...world.devices.keys()], [`addr:${C}`]);
  const v = world.snapshot().devices[0];
  assert.equal(v.provisional, true);
  assert.equal(v.deviceId, 0);
  assert.equal(v.key, `addr:${C}`);
  assert.ok(v.entities.length > 0);
  const dev = world.deviceAt(C)!;
  const rx = dev.rx;

  // The Hello refresh at tick 600 identifies the device without losing its state.
  e.run(360);
  assert.deepEqual([...world.devices.keys()], ['id:9']);
  assert.equal(world.device(9), dev, 'same Device object');
  assert.equal(dev.rx, rx, 'same session: receiver kept');
  assert.equal(world.snapshot().devices[0].provisional, false);
  assert.ok(world.snapshot().devices[0].entities.length > 0, 'entities survive identification');

  // The app restarts its edge (new session, ticks from 0) and says Hello from the same address.
  e.free();
  const e2 = new ScriptedEdge(world, clock, C, 9, 6);
  wireAcks(world, [e2]);
  e2.run(240);
  assert.deepEqual([...world.devices.keys()], ['id:9']);
  assert.equal(world.device(9), dev);
  assert.equal(dev.sessions, 2, 'ticks restarted: fresh receiver');
  assert.ok(dev.rx.last_edge_tick() < 240);
  assert.equal(world.snapshot().devices[0].provisional, false);
  e2.free();
});

test('a silent device is adopted by a new port that continues its tick stream (no Hello)', () => {
  const { clock, world } = setup();
  const e = new ScriptedEdge(world, clock, A, 42, 1);
  const acks = wireAcks(world, [e]);
  e.run(360);
  const dev = world.device(42)!;
  e.run(120, { deliver: false }); // 1 s of dead link
  e.addr = B; acks.length = 0;
  e.run(360, { snapshotEvery: 4 });
  assert.equal(world.devices.size, 1, [...world.devices.keys()].join(','));
  assert.equal(world.device(42), dev);
  assert.equal(dev.addr, B);
  assert.equal(dev.addrChanges, 1);
  assert.ok(acks.length > 0 && acks.every(a => a.addr === B));
  const v = world.snapshot().devices[0];
  assert.equal(v.entities.length, 2);
  assert.ok(v.entities.every(x => !x.stale));
  e.free();
});

test('a port change with no silence is adopted once the old port has been quiet 500 ms', () => {
  const { clock, world } = setup();
  const e = new ScriptedEdge(world, clock, A, 42, 1, chatty);
  wireAcks(world, [e]);
  e.run(360);
  e.addr = B; // NAT rebinding: the very next datagram comes from a new port
  e.run(30);
  assert.equal(world.devices.size, 2, 'briefly provisional');
  assert.ok(world.deviceAt(B)!.provisional);
  e.run(120);
  assert.deepEqual([...world.devices.keys()], ['id:42']);
  assert.equal(world.device(42)!.addr, B);
  e.free();
});

test('no adoption when ambiguous or when the ticks do not continue', () => {
  const { clock, world } = setup();
  const a = new ScriptedEdge(world, clock, A, 42, 1);
  const b = new ScriptedEdge(world, clock, C, 43, 2);
  wireAcks(world, [a, b]);
  for (let i = 0; i < 360; i++) { a.run(1); b.run(1); clock.advance(-1000 / 120); }
  // Both go quiet at once; a stream continuing "their" ticks could be either: keep it apart.
  for (let i = 0; i < 120; i++) { a.run(1, { deliver: false }); b.run(1, { deliver: false }); clock.advance(-1000 / 120); }
  a.addr = B; a.run(100); // ticks 480..579: no Hello yet
  assert.equal(world.devices.size, 3);
  assert.ok(world.deviceAt(B)!.provisional);
  a.run(140); // the Hello refresh at tick 600 resolves the ambiguity
  assert.deepEqual([...world.devices.keys()].sort(), ['id:42', 'id:43']);
  assert.equal(world.device(42)!.addr, B);

  // Unrelated edge (ticks from 0, pre-acked) on a new port while 42 is silent: not adopted.
  const { clock: c2, world: w2 } = setup();
  const x = new ScriptedEdge(w2, c2, A, 42, 1); wireAcks(w2, [x]); x.run(600);
  c2.advance(1000);
  const y = new ScriptedEdge(w2, c2, B, 77, 3);
  y.edge.on_datagram(new Uint8Array([0, 4, 0, 0, 0]));
  y.run(120);
  assert.equal(w2.devices.size, 2);
  assert.equal(w2.device(42)!.addr, A);
  [a, b, x, y].forEach(e => e.free());
});

test('acks only for datagrams that got through the shaper (blackout silences the downlink)', () => {
  const { clock, world, shaper } = setup();
  const e = new ScriptedEdge(world, clock, A, 42, 1);
  const acks = wireAcks(world, [e]);
  shaper.set({ enabled: true, loss: 1 });
  e.run(240);
  assert.equal(acks.length, 0);
  assert.equal(world.devices.size, 0, 'dropped Hellos create no device');
  shaper.set({ loss: 0 });
  e.run(240);
  assert.ok(acks.length > 0);
  assert.deepEqual([...world.devices.keys()], ['id:42'], 'the edge kept saying Hello until one got through');
  e.free();
});

test('a new session nonce from the same device starts a fresh receiver', () => {
  const { clock, world } = setup();
  const e = new ScriptedEdge(world, clock, A, 42, 1);
  wireAcks(world, [e]);
  e.run(600, { snapshotEvery: 4 });
  const dev = world.device(42)!;
  assert.ok(dev.rx.last_edge_tick() > 500);
  e.free();
  const e2 = new ScriptedEdge(world, clock, A, 42, 2, t => [{ id: 5, class: 41, pos: [0, 1, 0], vel: [0, 0, 0], conf: 100 }]);
  wireAcks(world, [e2]);
  e2.run(240, { snapshotEvery: 4 });
  assert.equal(world.device(42), dev);
  assert.equal(dev.sessions, 2);
  assert.ok(dev.rx.last_edge_tick() < 240, `last tick ${dev.rx.last_edge_tick()}`);
  const v = world.snapshot().devices[0];
  assert.deepEqual(v.entities.map(x => x.id), [5]);
  assert.ok(Math.abs(v.edgeTick - e2.tick) <= 2, `edge tick ${v.edgeTick} vs ${e2.tick}`);
  e2.free();
});

test('silent device: entities stale after 5 s, device removed after 30 s', () => {
  const { clock, world } = setup();
  const e = new ScriptedEdge(world, clock, A, 42, 1);
  wireAcks(world, [e]);
  e.run(240);
  clock.advance(world.device(42)!.lastSeenMs + DEVICE_SILENT_MS - 100 - clock.t);
  let v = world.snapshot().devices[0];
  assert.equal(v.silent, false);
  clock.advance(200);
  v = world.snapshot().devices[0];
  assert.equal(v.silent, true);
  assert.ok(v.entities.length > 0 && v.entities.every(x => x.stale));
  clock.advance(DEVICE_TIMEOUT_MS);
  assert.equal(world.snapshot().devices.length, 0);
  assert.equal(world.deviceAt(A), undefined);
  e.free();
});

test('extrapolates to edge now: edgeTick tracks the edge within a tick or two', () => {
  const { clock, world, shaper } = setup();
  const e = new ScriptedEdge(world, clock, A, 42, 1);
  wireAcks(world, [e]);
  e.run(1200);
  assert.ok(Math.abs(world.snapshot().devices[0].edgeTick - e.tick) <= 2);
  // 300 ms of emulated latency stays compensated (below the clock step threshold).
  shaper.set({ enabled: true, delayMs: 300 });
  e.run(1200);
  assert.ok(Math.abs(world.snapshot().devices[0].edgeTick - e.tick) <= 3, `${world.snapshot().devices[0].edgeTick} vs ${e.tick}`);
  e.free();
});
