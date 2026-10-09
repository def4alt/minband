import { test } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import type { AddressInfo } from 'node:net';
import { World } from '../src/world.js';
import { createApi } from '../src/http.js';
import { FakeClock, ScriptedEdge, wireAcks } from './fake.js';
import { Shaper } from '../src/shaper.js';

async function withServer(world: World, fn: (base: string) => Promise<void>) {
  const server = http.createServer(createApi(world));
  await new Promise<void>(r => server.listen(0, '127.0.0.1', r));
  const base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  try { await fn(base); } finally { await new Promise(r => server.close(r)); }
}

test('GET /api/shaper sets and reports the shaper; bad input is a 400 that changes nothing', async () => {
  const world = new World();
  await withServer(world, async base => {
    let r = await fetch(`${base}/api/shaper?bps=2000&loss=0.3&enabled=1&delayMs=100`);
    assert.equal(r.status, 200);
    assert.equal(r.headers.get('access-control-allow-origin'), '*');
    let j = await r.json();
    assert.deepEqual(j.config, { bps: 2000, delayMs: 100, loss: 0.3, enabled: true, burstSec: 0.5 });
    assert.equal(typeof j.counters.dropped, 'number');
    r = await fetch(`${base}/api/shaper?loss=30`);
    assert.equal(r.status, 400);
    r = await fetch(`${base}/api/shaper?lossy=1`);
    assert.equal(r.status, 400);
    assert.equal(world.shaper.config.loss, 0.3);
    j = await (await fetch(`${base}/api/shaper?loss=1&revertAfterMs=10000`)).json();
    assert.equal(j.config.loss, 1);
    assert.ok(j.revertInMs > 9000);
    j = await (await fetch(`${base}/api/shaper`)).json(); // read-only
    assert.equal(j.config.bps, 2000);
    world.shaper.set({}); // end the timed override (clears its timer)
  });
});

test('GET /api/metrics, /api/budget, /api/fusion; POST /api/ground-truth', async () => {
  const clock = new FakeClock();
  const world = new World({ now: clock.now, shaper: new Shaper({ now: clock.now, schedule: clock.schedule, cancel: clock.cancel }) });
  const e = new ScriptedEdge(world, clock, '127.0.0.1:9000', 42, 1);
  wireAcks(world, [e]);
  e.run(600, { snapshotEvery: 4 });
  await withServer(world, async base => {
    let m = await (await fetch(`${base}/api/metrics`)).json();
    assert.equal(m.devices.length, 1);
    assert.equal(m.devices[0].deviceId, 42);
    for (const k of ['bps', 'msgsPerSec', 'offeredBps', 'entities']) assert.equal(typeof m.devices[0][k], 'number', k);
    assert.equal(typeof m.devices[0].stats.gapsDetected, 'number');
    assert.equal(m.entityCount, 2);
    assert.deepEqual(m.twinError, { meanM: null, p95M: null, samples: 0 });
    assert.equal(typeof m.shaper.counters.passed, 'number');

    assert.deepEqual(await (await fetch(`${base}/api/budget?bps=4000`)).json(), { budgetBps: 4000 });
    assert.equal(world.budgetBps, 4000);
    assert.deepEqual(await (await fetch(`${base}/api/fusion?enabled=0`)).json(), { fusion: false });
    assert.equal(world.fusion.enabled, false);

    const csv = 'tick,id,class,x,y,z,vx,vy,vz,conf\n300,2,56,-1,0,1,0,0,0,180\n304,2,56,-1,0,1.1,0,0,0,180\n';
    let r = await fetch(`${base}/api/ground-truth?deviceId=42`, { method: 'POST', body: csv, headers: { 'content-type': 'text/csv' } });
    assert.equal(r.status, 200);
    const g = await r.json();
    assert.equal(g.samples, 2);
    assert.ok(g.meanM >= 0 && g.meanM < 0.1, `mean ${g.meanM}`);
    m = await (await fetch(`${base}/api/metrics`)).json();
    assert.equal(m.twinError.samples, 2);
    assert.equal(typeof m.twinError.meanM, 'number');
    assert.equal((await fetch(`${base}/api/ground-truth?deviceId=5`, { method: 'POST', body: csv })).status, 404);
    assert.equal((await fetch(`${base}/api/ground-truth`, { method: 'POST', body: csv })).status, 400);
    assert.equal((await fetch(`${base}/api/ground-truth?deviceId=42`)).status, 405);
    assert.equal((await fetch(`${base}/nope`)).status, 404);
    assert.equal((await fetch(`${base}/api/metrics`, { method: 'OPTIONS' })).status, 204);
  });
  e.free();
});
