// Link profiles (the Pi box table, applied in-process), time on air, the per-datagram packet
// stream behind the waterfall, and the measured H.264 table, against a live sim edge.
import { test, after, before } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { startStack, getJson, sleep, waitFor, type Stack } from '../lib/stack.ts';

let s: Stack;
const baselineFile = join(mkdtempSync(join(tmpdir(), 'minband-e2e-')), 'baseline_a.json');
writeFileSync(baselineFile, JSON.stringify({ entries: [{ id: 'h264_720p', bps: 1_234_567, source: 'measured: e2e fixture' }] }));

before(async () => { s = await startStack({ server: { MINBAND_BASELINE_A: baselineFile } }); });
after(async () => { await s?.stop(); });

const dev100 = (snap: any) => snap.devices.find((d: any) => d.deviceId === 100);

test('measured Baseline A replaces the configured 720p row', async () => {
  const snap = await s.feed.until('snapshot', x => x);
  const row = snap.baselineA.find((b: any) => b.id === 'h264_720p');
  assert.equal(row.measured, true);
  assert.equal(row.bps, 1_234_567);
  assert.equal(snap.baselineA.find((b: any) => b.id === 'h264_480p').measured, false);
  assert.equal(snap.baselines.h264_720p_bps, 1_234_567, 'legacy field follows the table');
});

test('packets: uplink deltas/keyframes and downlink acks, with link bytes', async () => {
  await s.feed.until('device up', x => dev100(x)?.entities.length >= 3, 10_000);
  const from = s.feed.packets.length;
  await sleep(4000);
  const ps = s.feed.packets.slice(from);
  const up = ps.filter(p => p.dir === 'up'), down = ps.filter(p => p.dir === 'down');
  assert.ok(up.some(p => p.kind === 'keyframe'), 'keyframes');
  assert.ok(up.some(p => p.kind === 'delta'), 'deltas');
  assert.ok(down.length > 0 && down.every(p => p.kind === 'ack'), 'acks down');
  assert.ok(up.every(p => p.bytes > 28 && p.key === 'id:100'), 'bytes include the UDP/IP header, attributed');
});

test('lora profile: shaper, budget and LoRa time on air; dropped datagrams are reported', async () => {
  const v = await getJson(`${s.server.api}/api/link?profile=lora`);
  assert.equal(v.profile, 'lora');
  assert.equal(v.model.kind, 'lora');
  const from = s.feed.packets.length;
  await sleep(8000);
  const snap = s.feed.latest;
  assert.equal(snap.shaper.enabled, true);
  assert.equal(snap.shaper.bps, 2000);
  assert.equal(snap.budgetBps, 1500);
  assert.equal(snap.link.profile, 'lora');
  assert.ok(snap.link.airtimeShare > 0.05, `airtime ${snap.link.airtimeShare}`);
  assert.ok(dev100(snap).airtimeShare > 0, 'per-device airtime');
  // ~20 datagrams in 8 s at this budget: P(no loss) ~ 0.9^20, so wait for the first drop.
  await waitFor('a dropped datagram (10 % loss)', () => s.feed.packets.slice(from).some(p => p.dropped), 30_000);
  assert.equal(dev100(snap).entities.filter((e: any) => e.stale).length, 0, 'twin holds');
});

test('external profile: the Pi box shapes, the server only takes budget and airtime model', async () => {
  const v = await getJson(`${s.server.api}/api/link?profile=external&as=hf`);
  assert.equal(v.profile, 'external');
  await s.feed.until('external applied', x => x.link.profile === 'external' && !x.shaper.enabled && x.budgetBps === 8000 && x.link.model.kind === 'serial', 3000);
  const r = await fetch(`${s.server.api}/api/link?profile=nope`);
  assert.equal(r.status, 400);
  await getJson(`${s.server.api}/api/link?profile=clean`);
  await s.feed.until('clean', x => x.link.profile === 'clean' && x.budgetBps === 0 && !x.shaper.enabled, 3000);
});
