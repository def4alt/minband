// Loss and blackout through the in-process shaper with real UDP from the sim edge.
import { test, after, before } from 'node:test';
import assert from 'node:assert/strict';
import { startStack, getJson, sleep, waitFor, type Stack } from '../lib/stack.ts';

let s: Stack;
before(async () => { s = await startStack(); });
after(async () => { await s?.stop(); });

const dev100 = (snap: any) => snap.devices.find((d: any) => d.deviceId === 100);

test('20 % loss: the twin stays coherent (state repair, no retransmission queue)', async () => {
  await s.feed.until('device up', x => dev100(x)?.entities.length >= 3, 10_000);
  await getJson(`${s.server.api}/api/shaper?enabled=1&loss=0.2`);
  await sleep(12_000); // one full ground-truth window under loss
  const m = await getJson(`${s.server.api}/api/metrics`);
  const d = m.devices.find((x: any) => x.deviceId === 100);
  assert.ok(m.shaper.counters.droppedLoss > 0, 'shaper dropped datagrams');
  assert.ok(d.stats.gapsDetected > 0, 'receiver saw gaps');
  assert.ok(d.staleEntities === 0, `stale ${d.staleEntities}`);
  assert.ok(d.twinError.meanM < 0.2, `mean ${d.twinError.meanM} m at 20 % loss`);
  await getJson(`${s.server.api}/api/shaper?enabled=0&loss=0`);
});

test('10 s blackout: entities keep predicting, go stale, and re-sync after the link returns', async () => {
  await s.feed.until('fresh', x => dev100(x)?.entities.length >= 3 && dev100(x).entities.every((e: any) => !e.stale), 10_000);
  await getJson(`${s.server.api}/api/shaper?enabled=1&loss=1&revertAfterMs=10000`);
  const t0 = Date.now();
  const stale = await s.feed.until('stale during blackout', x => dev100(x)?.entities.length && dev100(x).entities.every((e: any) => e.stale) && x, 12_000);
  assert.ok(Date.now() - t0 >= 4000, 'not stale before the stale threshold');
  assert.ok(dev100(stale).entities.length >= 3, 'entities kept (predicted), not dropped');
  await waitFor('revert', () => s.feed.latest.shaperRevertMs === null, 12_000);
  const tBack = Date.now();
  await s.feed.until('fresh after blackout', x => dev100(x)?.entities.length >= 3 && dev100(x).entities.every((e: any) => !e.stale), 6000);
  assert.ok(Date.now() - tBack < 4000, `re-sync took ${Date.now() - tBack} ms (one keyframe is 2 s)`);
});
