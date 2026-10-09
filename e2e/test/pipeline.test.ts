// The full pipeline with no phone: sim edge (WASM Edge) -> UDP -> server (shaper, WASM Receiver,
// fusion) -> WebSocket snapshot, plus the HTTP API and the ground-truth twin-error loop.
import { test, after, before } from 'node:test';
import assert from 'node:assert/strict';
import { startStack, getJson, sleep, type Stack } from '../lib/stack.ts';

let s: Stack;
before(async () => { s = await startStack(); });
after(async () => { await s?.stop(); });

test('sim device is identified and its walkers show up in the twin', async () => {
  const dev = await s.feed.until('device 100 with entities', snap => snap.devices.find((d: any) => d.deviceId === 100 && !d.provisional && d.entities.length >= 3), 10_000);
  assert.equal(dev.key, 'id:100');
  for (const e of dev.entities) {
    assert.equal(e.pos.length, 3);
    assert.ok(e.pos.every(Number.isFinite), 'finite positions');
  }
});

test('uplink is a few hundred B/s, orders of magnitude under video', async () => {
  await sleep(3000); // rate window is 2 s
  const m = await getJson(`${s.server.api}/api/metrics`);
  const d = m.devices.find((x: any) => x.deviceId === 100);
  assert.ok(d.bps > 100 && d.bps < 10_000, `bps ${d.bps}`);
  assert.ok(s.feed.latest.baselines.h264_720p_bps / d.bps > 100, 'video is >100x');
});

test('twin error against the sim ground truth stays within the position threshold', async () => {
  const m = await (async () => { for (let i = 0; i < 40; i++) { const m = await getJson(`${s.server.api}/api/metrics`); if (m.twinError.samples > 100) return m; await sleep(250); } throw new Error('no twin error samples'); })();
  // theta_pos is 0.15 m; live numbers also carry clock-offset and scheduling jitter.
  assert.ok(m.twinError.meanM < 0.15, `mean ${m.twinError.meanM}`);
  assert.ok(m.twinError.p95M < 0.3, `p95 ${m.twinError.p95M}`);
});
