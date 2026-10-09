// CoT export end to end: the server, given a geodetic anchor, sends one CoT event per fused entity
// over UDP to a TAK-style listener; positions land next to the anchor and the HTTP views agree.
import { test, after, before } from 'node:test';
import assert from 'node:assert/strict';
import dgram from 'node:dgram';
import { startStack, freeUdpPort, getJson, waitFor, type Stack } from '../lib/stack.ts';

const ANCHOR = { lat: 37.5665, lon: 126.978, heading: 90, alt: 38 }; // Seoul City Hall
let s: Stack, sock: dgram.Socket;
const events: string[] = [];

before(async () => {
  const port = await freeUdpPort();
  sock = dgram.createSocket('udp4');
  sock.on('message', m => events.push(m.toString()));
  await new Promise<void>(r => sock.bind(port, '127.0.0.1', () => r()));
  s = await startStack({
    sim: { DEVICES: '2' },
    server: { MINBAND_GEO: `${ANCHOR.lat},${ANCHOR.lon},${ANCHOR.heading},${ANCHOR.alt}`, MINBAND_COT: `udp://127.0.0.1:${port}`, MINBAND_COT_HZ: '2' },
  });
});
after(async () => { sock?.close(); await s?.stop(); });

const attr = (xml: string, tag: string, name: string) => new RegExp(`<${tag}\\b[^>]*\\b${name}="([^"]*)"`).exec(xml)?.[1];

test('CoT events arrive over UDP for the fused entities, next to the anchor', async () => {
  await waitFor('CoT events for 3+ entities', () => new Set(events.map(e => attr(e, 'event', 'uid'))).size >= 3, 15_000);
  const last = new Map<string, string>();
  for (const e of events) last.set(attr(e, 'event', 'uid')!, e);
  for (const [uid, xml] of last) {
    assert.match(uid, /^minband-/);
    assert.equal(attr(xml, 'event', 'type'), 'a-u-G');
    assert.match(attr(xml, 'event', 'how')!, /^m-[fp]$/);
    const lat = Number(attr(xml, 'point', 'lat')), lon = Number(attr(xml, 'point', 'lon'));
    // The sim scene is within ~5 m of the origin: 5 m is ~4.5e-5 deg of latitude.
    assert.ok(Math.abs(lat - ANCHOR.lat) < 1e-4 && Math.abs(lon - ANCHOR.lon) < 1.5e-4, `${uid} at ${lat},${lon}`);
    assert.ok(Date.parse(attr(xml, 'event', 'stale')!) >= Date.parse(attr(xml, 'event', 'time')!));
  }
});

test('snapshot and HTTP views carry the anchor and MGRS grid references', async () => {
  const geo = await getJson(`${s.server.api}/api/geo`);
  assert.match(geo.anchor.mgrs, /^52S [A-Z]{2} \d{5} \d{5}$/);
  const snap = await s.feed.until('global entities with geo', x => x.global.length && x.global.every((g: any) => g.geo?.mgrs) && x, 10_000);
  assert.equal(snap.geo.mgrs, geo.anchor.mgrs);
  const r = await fetch(`${s.server.api}/api/cot`);
  assert.equal(r.status, 200);
  assert.match(await r.text(), /<events count="\d+">/);
});

test('clearing the anchor stops geo in the snapshot', async () => {
  await getJson(`${s.server.api}/api/geo?clear=1`);
  await s.feed.until('geo cleared', x => x.geo === null && x.global.every((g: any) => g.geo === null), 5000);
  assert.equal((await fetch(`${s.server.api}/api/cot`)).status, 409);
});
