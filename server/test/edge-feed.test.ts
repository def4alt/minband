import { test } from 'node:test';
import assert from 'node:assert/strict';
import { EdgeFeed, unpack } from '../src/edge-feed.js';

test('EdgeFeed.ingest: parses edge.py\'s track datagram shape, clamps/rounds conf', () => {
  const feed = new EdgeFeed();
  feed.ingest(JSON.stringify({
    t: 1000, tick: 1,
    tracks: [{ id: 7, cls: 0, pos: [1, 0, 2], vel: [0.5, 0, 0], conf: 199.6, age: 3 }],
  }), 1000);
  const tracks = feed.currentTracks(1000);
  assert.equal(tracks.length, 1);
  assert.equal(tracks[0].id, 7);
  assert.equal(tracks[0].class, 0);
  assert.equal(tracks[0].conf, 200); // rounded
});

test('EdgeFeed.ingest: out-of-range conf is clamped to 0..255', () => {
  const feed = new EdgeFeed();
  feed.ingest(JSON.stringify({ t: 0, tick: 0, tracks: [{ id: 1, cls: 0, pos: [0, 0, 0], vel: [0, 0, 0], conf: 999 }] }), 0);
  assert.equal(feed.currentTracks(0)[0].conf, 255);
});

test('EdgeFeed.ingest: malformed JSON is dropped, not thrown', () => {
  const feed = new EdgeFeed();
  assert.doesNotThrow(() => feed.ingest('not json', 2000));
  assert.equal(feed.currentTracks(2000).length, 0);
});

test('EdgeFeed.currentTracks: extrapolates by velocity between edge.py refreshes', () => {
  const feed = new EdgeFeed();
  feed.ingest(JSON.stringify({ t: 0, tick: 0, tracks: [{ id: 1, cls: 0, pos: [0, 0, 0], vel: [2, 0, 0], conf: 200 }] }), 0);
  const tracks = feed.currentTracks(500); // 0.5 s later, no new datagram
  assert.equal(tracks.length, 1);
  assert.ok(Math.abs(tracks[0].pos[0] - 1.0) < 1e-9, `expected x~1.0, got ${tracks[0].pos[0]}`);
});

test('EdgeFeed.currentTracks: drops a track not refreshed within STALE_MS', () => {
  const feed = new EdgeFeed();
  feed.ingest(JSON.stringify({ t: 0, tick: 0, tracks: [{ id: 1, cls: 0, pos: [0, 0, 0], vel: [0, 0, 0], conf: 200 }] }), 0);
  assert.equal(feed.currentTracks(1000).length, 1); // still fresh
  assert.equal(feed.currentTracks(10_000).length, 0); // long past STALE_MS (4000 ms)
});

test('EdgeFeed.ingest: a newer datagram for the same id overwrites the older one', () => {
  const feed = new EdgeFeed();
  feed.ingest(JSON.stringify({ t: 0, tick: 0, tracks: [{ id: 1, cls: 0, pos: [0, 0, 0], vel: [0, 0, 0], conf: 100 }] }), 0);
  feed.ingest(JSON.stringify({ t: 100, tick: 1, tracks: [{ id: 1, cls: 0, pos: [5, 0, 5], vel: [0, 0, 0], conf: 250 }] }), 100);
  const tracks = feed.currentTracks(100);
  assert.equal(tracks.length, 1);
  assert.deepEqual(tracks[0].pos, [5, 0, 5]);
  assert.equal(tracks[0].conf, 250);
});

test('unpack: splits u16-length-prefixed datagrams, same framing as sim.ts', () => {
  const a = new Uint8Array([1, 2, 3]), b = new Uint8Array([4, 5]);
  const buf = new Uint8Array(2 + a.length + 2 + b.length);
  buf.set([a.length & 0xff, a.length >> 8], 0);
  buf.set(a, 2);
  buf.set([b.length & 0xff, b.length >> 8], 2 + a.length);
  buf.set(b, 2 + a.length + 2);
  const out = unpack(buf);
  assert.equal(out.length, 2);
  assert.deepEqual([...out[0]], [1, 2, 3]);
  assert.deepEqual([...out[1]], [4, 5]);
});
