// Two sim edges observing the same walkers (shared marker origin): fusion merges them into one
// picture, and the toggle splits them again.
import { test, after, before } from 'node:test';
import assert from 'node:assert/strict';
import { startStack, getJson, type Stack } from '../lib/stack.ts';

let s: Stack;
before(async () => { s = await startStack({ sim: { DEVICES: '2' } }); });
after(async () => { await s?.stop(); });

test('two devices fuse into one set of global entities', async () => {
  const snap = await s.feed.until('two identified devices with entities', x =>
    x.devices.filter((d: any) => !d.provisional && d.entities.length >= 3).length === 2 && x, 10_000);
  const perDevice = snap.devices.reduce((a: number, d: any) => a + d.entities.length, 0);
  const merged = await s.feed.until('merged entities', x => x.global.some((g: any) => g.sources.length === 2) && x, 10_000);
  assert.ok(merged.global.length < perDevice, `global ${merged.global.length} vs per-device ${perDevice}`);
});

test('fusion toggle splits the picture back into per-device entities', async () => {
  await getJson(`${s.server.api}/api/fusion?enabled=0`);
  const snap = await s.feed.until('unfused', x => !x.fusion && x.global.every((g: any) => g.sources.length === 1) && x, 5000);
  assert.ok(snap.global.length >= 6);
  await getJson(`${s.server.api}/api/fusion?enabled=1`);
  await s.feed.until('fused again', x => x.fusion && x.global.some((g: any) => g.sources.length === 2), 10_000);
});
