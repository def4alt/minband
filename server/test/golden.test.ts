// The Rust golden scene replayed through the WASM build must produce byte-identical datagrams.
// Guards the determinism contract across native and WASM.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { WasmEdge, WasmReceiver } from 'minband-core';

const TICK_HZ = 120;
const golden = JSON.parse(readFileSync(new URL('../../core/tests/golden/scene1.json', import.meta.url), 'utf8')) as {
  datagrams: [number, string][]; extrapolations: [number, number, number[]][]; bytes_total: number;
};

// Mirror of core/tests/golden.rs::scene. Keep in sync.
function scene(tick: number) {
  const t = Math.fround(tick / TICK_HZ);
  const v: object[] = [];
  let x: number, z: number, vx: number, vz: number;
  if (t < 2) { x = t; z = 0; vx = 1; vz = 0; } else if (t < 4) { x = 2; z = Math.fround(Math.fround(t - 2) * 1.5); vx = 0; vz = 1.5; } else { x = Math.fround(2 - Math.fround(t - 4)); z = 3; vx = -1; vz = 0; }
  v.push({ id: 1, class: 0, pos: [x, 0, z], vel: [vx, 0, vz], conf: 230 });
  v.push({ id: 2, class: 56, pos: [-1, 0, 1], vel: [0, 0, 0], conf: 180 });
  if (t >= 3 && t < 6) v.push({ id: 3, class: 0, pos: [0, 0, -t], vel: [0, 0, -1], conf: 120 + (Math.trunc(t * 10) % 256) % 100 });
  return v;
}
const hex = (b: Uint8Array) => [...b].map(x => x.toString(16).padStart(2, '0')).join('');
function unpack(buf: Uint8Array): Uint8Array[] {
  const out: Uint8Array[] = []; let i = 0;
  while (i + 2 <= buf.length) { const n = buf[i] | (buf[i + 1] << 8); i += 2; out.push(buf.subarray(i, i + n)); i += n; }
  return out;
}
const isSeq5Delta = (d: Uint8Array) => d[0] === 0 && d[1] === 1 && d[2] === 5; // version, Delta tag, seq varint

test('wasm edge reproduces the native golden datagrams', () => {
  const edge = new WasmEdge(7, 0xC0FFEE);
  const rx = new WasmReceiver();
  // Hello at tick 0, then pre-ack like the Rust test: Ack { last_seq: 0, missing: [], budget_bps: 0 }
  edge.tick('[]', 0);
  edge.on_datagram(new Uint8Array([0, 4, 0, 0, 0]));
  const got: [number, string][] = []; let bytes = 0;
  const ex: [number, number, number[]][] = [];
  for (let tick = 0; tick < 8 * TICK_HZ; tick++) {
    for (const d of unpack(edge.tick(JSON.stringify(scene(tick)), tick))) {
      got.push([tick, hex(d)]); bytes += d.length;
      if (!isSeq5Delta(d)) rx.on_datagram(d);
    }
    if (tick % 12 === 0 && rx.needs_ack()) edge.on_datagram(rx.make_ack(0));
    if (tick % 30 === 0) for (const e of JSON.parse(rx.extrapolate_json(tick))) ex.push([tick, e.id, e.pos]);
  }
  assert.equal(got.length, golden.datagrams.length);
  got.forEach((g, i) => assert.deepEqual(g, golden.datagrams[i], `datagram ${i}`));
  assert.equal(bytes, golden.bytes_total);
  assert.equal(ex.length, golden.extrapolations.length);
  ex.forEach((e, i) => { assert.equal(e[0], golden.extrapolations[i][0]); assert.equal(e[1], golden.extrapolations[i][1]); e[2].forEach((p, k) => assert.ok(Math.abs(p - golden.extrapolations[i][2][k]) < 1e-6, `extrap ${i}.${k}`)); });
});
