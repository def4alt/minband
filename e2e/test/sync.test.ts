// The sync protocol's new honesty features end to end (core v1 via the server): every entity
// carries the threshold its edge declared (S14), entities coast with a growing error radius when
// the heartbeat is missed (S15), the keyframe cadence follows the budget (S19), keyframe parts are
// paced (S16), and the packet stream names the entities each datagram carried.
import { test, after, before } from 'node:test';
import assert from 'node:assert/strict';
import { startStack, getJson, sleep, waitFor, type Stack } from '../lib/stack.ts';

let s: Stack;
before(async () => { s = await startStack({ historyMs: 60_000 }); });
after(async () => { await s?.stop(); });

const dev100 = (snap: any) => snap.devices.find((d: any) => d.deviceId === 100);

test('entities carry the declared threshold; ce equals theta while the heartbeat holds', async () => {
  const d = await s.feed.until('device up', x => dev100(x)?.entities.length >= 3 && dev100(x), 10_000);
  for (const e of d.entities) {
    assert.ok(e.theta >= 0.15 && e.theta < 0.5, `theta ${e.theta}`);
    assert.equal(e.coasting, false);
    assert.equal(e.ce, e.theta);
  }
});

test('packet events list the entity ids each delta/keyframe carried', async () => {
  const from = s.feed.packets.length;
  await sleep(3000);
  const kf = s.feed.packets.slice(from).filter(p => p.dir === 'up' && p.kind === 'keyframe');
  assert.ok(kf.length > 0);
  assert.ok(kf.every(p => Array.isArray(p.ids) && p.ids.length >= 3), JSON.stringify(kf[0]));
});

test('blackout: coasting after the missed heartbeat, ce grows, then snaps back to theta', async () => {
  await getJson(`${s.server.api}/api/shaper?enabled=1&loss=1&revertAfterMs=9000`);
  const t0 = Date.now();
  const c = await s.feed.until('coasting', x => dev100(x)?.entities.every((e: any) => e.coasting) && dev100(x), 6000);
  const tc = Date.now() - t0;
  assert.ok(tc >= 1500 && tc <= 4500, `coasting after ${tc} ms (keyframe 2 s + margin)`);
  const ce0 = Math.max(...c.entities.map((e: any) => e.ce));
  await sleep(2000);
  const ce1 = Math.max(...dev100(s.feed.latest).entities.map((e: any) => e.ce));
  assert.ok(ce1 > ce0 + 2, `ce grows with silence x class max speed: ${ce0.toFixed(2)} -> ${ce1.toFixed(2)} m`);
  await waitFor('link back', () => s.feed.latest.shaperRevertMs === null, 10_000);
  const back = await s.feed.until('re-synced', x => dev100(x)?.entities.length >= 3 && dev100(x).entities.every((e: any) => !e.coasting && !e.stale && e.ce === e.theta) && dev100(x), 6000);
  assert.ok(back.entities.length >= 3);
});
