// peek(): structured routing info from core's peek_json, no string parsing in TS.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { WasmEdge, WasmReceiver } from 'minband-core';
import { peek } from '../src/peek.js';
import { unpack } from './fake.js';

test('peek reports kind, ids, keyframe parts and the declared threshold', () => {
  const edge = new WasmEdge(7, 123);
  const [hello] = unpack(edge.tick('[]', 5));
  assert.deepEqual({ ...peek(hello), text: undefined }, { kind: 'hello', deviceId: 7, nonce: 123, tick: 5, text: undefined });
  edge.on_datagram(new WasmReceiver().make_ack(0));
  const tracks = JSON.stringify([{ id: 4, class: 0, pos: [0, 0, 0], vel: [0, 0, 0], conf: 200 }, { id: 9, class: 56, pos: [1, 0, 0], vel: [0, 0, 0], conf: 200 }]);
  const [spawn] = unpack(edge.tick(tracks, 6));
  const d = peek(spawn);
  assert.equal(d.kind, 'delta'); assert.deepEqual(d.ids, [4, 9]); assert.equal(d.thetaM, 0.15); assert.equal(d.seq, 1);
  const [kf] = unpack(edge.tick(tracks, 6 + 240));
  const k = peek(kf);
  assert.deepEqual([k.kind, k.part, k.of, k.ids, k.tick], ['keyframe', 0, 1, [4, 9], 246]);
  assert.match(k.text, /^Keyframe seq=2 tick=246 part=0\/1 entities=2/);
  const old = peek(new Uint8Array([0, 4, 0, 0, 0]));
  assert.equal(old.kind, 'malformed'); assert.equal(old.text, 'error BadVersion(0)');
  assert.equal(peek(new Uint8Array([])).kind, 'malformed');
  edge.free();
});
