// Below ~4 kbit/s (plan 3.4): the keyframe heartbeat follows the budget (S19), the controller holds
// the budget including the UDP/IP header, a static-ish scene does not flash stale between
// keyframes, and lifting the budget restores the 2 s heartbeat. One walker, telemetry profile
// (600 bit/s, 5 % loss, budget 450).
import { test, after, before } from 'node:test';
import assert from 'node:assert/strict';
import { startStack, getJson, sleep, type Stack } from '../lib/stack.ts';

let s: Stack;
before(async () => { s = await startStack({ sim: { SCENE: 'spread', WALKERS: '1' }, historyMs: 120_000 }); });
after(async () => { await s?.stop(); });

const dev100 = (snap: any) => snap.devices.find((d: any) => d.deviceId === 100);
const keyframesSince = (t: number) => s.feed.packets.filter(p => p.t >= t && p.dir === 'up' && p.kind === 'keyframe' && !p.dropped);

test('telemetry: keyframes stretch toward 15 s, budget held, nothing flashes stale', async () => {
  await s.feed.until('device up', x => dev100(x)?.entities.length >= 1, 10_000);
  await getJson(`${s.server.api}/api/link?profile=telemetry`);
  const d0 = await s.feed.until('cadence follows the budget', x => dev100(x)?.cadence.keyframeMs >= 10_000 && dev100(x), 8000);
  assert.ok(d0.cadence.staleMs >= 30_000, `stale after ${d0.cadence.staleMs} ms`);
  await sleep(10_000); // let the edge settle on the new budget
  const t0 = Date.now();
  await sleep(45_000);
  const kf = keyframesSince(t0);
  assert.ok(kf.length <= 12, `${kf.length} keyframe datagrams in 45 s (2 s cadence would be ~23 keyframes)`);
  // ...and the heartbeat does arrive: 3 keyframes due in 45 s at 15 s, 5 % loss.
  assert.ok(kf.length >= 2, `only ${kf.length} keyframe datagrams delivered in 45 s`);
  const hist = s.feed.recent(45_000);
  const staleSnaps = hist.filter(x => dev100(x)?.entities.some((e: any) => e.stale)).length;
  assert.equal(staleSnaps, 0, `${staleSnaps} of ${hist.length} snapshots had stale entities`);
  const m = await getJson(`${s.server.api}/api/metrics`);
  const dm = m.devices.find((x: any) => x.deviceId === 100);
  console.log(`telemetry: offered ${Math.round(dm.offeredBps)} bit/s, delivered ${Math.round(dm.bps)} bit/s, ${kf.length} keyframe datagrams / 45 s, keyframe every ${d0.cadence.keyframeMs} ms`);
  assert.ok(dm.offeredBps < 800, `edge offers ${dm.offeredBps} bit/s against a 450 bit/s budget`);
});

test('back to clean: the 2 s heartbeat returns (an Ack budget of 0 reaches the edge)', async () => {
  await getJson(`${s.server.api}/api/link?profile=clean`);
  await s.feed.until('cadence back to 2 s', x => dev100(x)?.cadence.keyframeMs === 2000, 8000);
  await sleep(4000);
  const t0 = Date.now();
  await sleep(8000);
  const kf = keyframesSince(t0);
  assert.ok(kf.length >= 3, `${kf.length} keyframes in 8 s`);
});
