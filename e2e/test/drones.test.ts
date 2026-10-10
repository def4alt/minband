// Drones per link (plan P1): eight sim edges, each with its own walker and a moving camera,
// share one emulated HF link (9.6 kbit/s, 500 ms, 1 % loss). The server splits the link budget
// over the devices it hears; per-device and total rates and the channel airtime are reported.
import { test, after, before } from 'node:test';
import assert from 'node:assert/strict';
import { startStack, getJson, sleep, type Stack } from '../lib/stack.ts';

const N = 8;
let s: Stack;
before(async () => { s = await startStack({ sim: { DEVICES: String(N), SCENE: 'spread', WALKERS: '1' } }); });
after(async () => { await s?.stop(); });

test(`${N} drones share one 9.6 kbit/s HF link`, async () => {
  await s.feed.until(`${N} identified devices`, x => x.devices.filter((d: any) => !d.provisional && d.entities.length >= 1).length === N, 15_000);
  await getJson(`${s.server.api}/api/link?profile=hf`);
  await sleep(30_000);
  const m = await getJson(`${s.server.api}/api/metrics`);
  const snap = s.feed.latest;
  const devs = m.devices.filter((d: any) => !d.provisional);
  const total = devs.reduce((a: number, d: any) => a + d.bps, 0);
  const offered = devs.reduce((a: number, d: any) => a + d.offeredBps, 0);
  const c = m.shaper.counters;
  const errs = devs.map((d: any) => d.twinError?.meanM).filter((x: any) => typeof x === 'number');
  const meanErr = errs.reduce((a: number, b: number) => a + b, 0) / errs.length;
  console.log(`hf x${N}: delivered ${Math.round(total)} bit/s (offered ${Math.round(offered)}), per device ${devs.map((d: any) => Math.round(d.bps)).join('/')} bit/s, ` +
    `budget each ${devs[0].edgeBudgetBps} bit/s, uplink airtime ${(snap.link.airtimeShare * 100).toFixed(0)} %, ` +
    `dropped ${c.dropped}/${c.offered} (loss ${c.droppedLoss}, cap ${c.droppedCap}, queue ${c.droppedQueue ?? 0}), mean twin error ${(meanErr * 100).toFixed(1)} cm`);
  assert.equal(devs.length, N);
  assert.ok(total <= 9600 * 1.05, `delivered ${total} bit/s fits 9.6 kbit/s`);
  assert.ok(devs.every((d: any) => d.edgeBudgetBps > 0 && d.edgeBudgetBps <= 8000 / N + 1), 'budget split over the devices');
  assert.ok(snap.link.airtimeShare > 0 && snap.link.airtimeShare < 1, `airtime ${snap.link.airtimeShare}`);
  assert.ok(devs.every((d: any) => d.staleEntities === 0), 'no stale entities');
  assert.ok(meanErr < 0.25, `mean twin error ${meanErr}`);
  assert.ok(snap.devices.every((d: any) => d.pose), 'every drone sends its camera pose');
});
