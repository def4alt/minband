import { test } from 'node:test';
import assert from 'node:assert/strict';
import dgram from 'node:dgram';
import http from 'node:http';
import net from 'node:net';
import { once } from 'node:events';
import type { AddressInfo } from 'node:net';
import {
  CotSender, TcpOut, UdpOut, cotConfigFromEnv, cotEvents, eventXml, eventsXml, parseCotEndpoints, xmlEscape,
  type CotSource,
} from '../src/cot.js';
import { geodeticToEnu, type GeoAnchor } from '../src/geo.js';
import { createApi } from '../src/http.js';
import { Shaper } from '../src/shaper.js';
import type { GlobalEntity } from '../src/types.js';
import { World } from '../src/world.js';
import { FakeClock, ScriptedEdge, wireAcks } from './fake.js';

// ---- a minimal strict XML parser: enough to prove well-formedness of what cot.ts emits ----------
interface XNode { name: string; attrs: Record<string, string>; children: XNode[]; text: string }
const ENT: Record<string, string> = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'" };
function decode(s: string): string {
  if (s.includes('<')) throw new Error(`raw < in "${s}"`);
  if (/&(?!(amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)/.test(s)) throw new Error(`bare & in "${s}"`);
  return s.replace(/&(#x[0-9a-fA-F]+|#\d+|\w+);/g, (_, e: string) =>
    e[0] === '#' ? String.fromCodePoint(e[1] === 'x' ? parseInt(e.slice(2), 16) : Number(e.slice(1))) : ENT[e]);
}
function parseXml(src: string): XNode {
  let i = 0;
  const m = /^\s*<\?xml\s[^?]*\?>/.exec(src);
  if (m) i = m[0].length;
  const ws = () => { while (/\s/.test(src[i] ?? '')) i++; };
  const name = () => { const r = /^[A-Za-z_][\w.:-]*/.exec(src.slice(i)); if (!r) throw new Error(`name expected at ${i}`); i += r[0].length; return r[0]; };
  const el = (): XNode => {
    if (src[i] !== '<') throw new Error(`< expected at ${i}`);
    i++;
    const n: XNode = { name: name(), attrs: {}, children: [], text: '' };
    for (;;) {
      ws();
      if (src.startsWith('/>', i)) { i += 2; return n; }
      if (src[i] === '>') { i++; break; }
      const k = name(); ws();
      if (src[i] !== '=') throw new Error(`= expected at ${i}`);
      i++; ws();
      const q = src[i]; if (q !== '"' && q !== "'") throw new Error(`quote expected at ${i}`);
      const end = src.indexOf(q, i + 1); if (end < 0) throw new Error('unterminated attribute');
      if (k in n.attrs) throw new Error(`duplicate attribute ${k}`);
      n.attrs[k] = decode(src.slice(i + 1, end)); i = end + 1;
    }
    for (;;) {
      const lt = src.indexOf('<', i); if (lt < 0) throw new Error(`unclosed <${n.name}>`);
      n.text += decode(src.slice(i, lt)); i = lt;
      if (src.startsWith('</', i)) {
        i += 2; const c = name(); ws();
        if (c !== n.name || src[i] !== '>') throw new Error(`</${c}> closes <${n.name}>`);
        i++; return n;
      }
      n.children.push(el());
    }
  };
  ws(); const root = el(); ws();
  if (i !== src.length) throw new Error(`trailing content at ${i}`);
  return root;
}
const child = (n: XNode, name: string) => { const c = n.children.find(x => x.name === name); assert.ok(c, `<${name}> in <${n.name}>`); return c; };

// ---- fixtures -----------------------------------------------------------------------------------
const SEOUL: GeoAnchor = { lat: 37.5665, lon: 126.978, headingDeg: 90, altM: null };
const T0 = Date.UTC(2026, 9, 10, 9, 0, 0);
function ent(o: Partial<GlobalEntity> = {}): GlobalEntity {
  return { gid: 'g1', class: 0, pos: [0, 0, -10], vel: [0, 0, -1.2], sources: [{ deviceId: 100, id: 3 }], stale: false, ce: 0.35, coasting: false, geo: null, ...o };
}
function source(global: GlobalEntity[], geo: GeoAnchor | null = SEOUL, t = T0) {
  const s = { geo, now: () => t, lastSnapshot: { t, global }, snapshot: () => s.lastSnapshot, set: (g: GlobalEntity[]) => { s.lastSnapshot = { t, global: g }; } };
  return s satisfies CotSource;
}

test('CoT event fields: type, how, time/stale, point, ce/le, track, callsign', () => {
  const [single, fused, coasting, stale, noCe] = cotEvents([
    ent(),
    ent({ gid: 'g2', sources: [{ deviceId: 100, id: 3 }, { deviceId: 101, id: 7 }] }),
    ent({ gid: 'g3', sources: [{ deviceId: 100, id: 4 }, { deviceId: 101, id: 8 }], coasting: true, ce: 2.4 }),
    ent({ gid: 'g4', class: 56, stale: true, vel: [0, 0, 0] }),
    { ...ent({ gid: 'g5' }), ce: undefined as unknown as number }, // before fusion fills ce
  ], SEOUL, T0);
  assert.equal(single.uid, 'minband-g1');
  assert.equal(single.type, 'a-u-G');
  assert.equal(single.how, 'm-p', 'one source: dead-reckoned prediction');
  assert.equal(fused.how, 'm-f', 'two sources: fused');
  assert.equal(coasting.how, 'm-p', 'coasting: back to prediction');
  assert.equal(single.time, T0); assert.equal(single.start, T0);
  assert.equal(single.stale, T0 + 5000, 'default validity: max(5 s, 3 periods at 1 Hz)');
  assert.equal(stale.stale, T0, 'already stale: stale = now');
  assert.equal(cotEvents([ent()], SEOUL, T0, { hz: 0.2 })[0].stale, T0 + 15_000);
  assert.equal(cotEvents([ent()], SEOUL, T0, { staleS: 2 })[0].stale, T0 + 2000);
  // 10 m along forward (-Z) at heading 90: 10 m east of the marker; moving east at 1.2 m/s.
  const enu = geodeticToEnu(single.lat, single.lon, 0, SEOUL.lat, SEOUL.lon, 0);
  assert.ok(Math.abs(enu[0] - 10) < 1e-6 && Math.abs(enu[1]) < 1e-6, enu.join());
  assert.ok(Math.abs(single.course - 90) < 1e-9 && Math.abs(single.speed - 1.2) < 1e-9);
  assert.equal(single.ce, 0.35);
  assert.equal(single.hae, 9999999, 'no anchor altitude: hae unknown');
  assert.equal(single.le, 9999999);
  assert.equal(noCe.ce, 9999999, 'missing ce: unknown, not 0');
  assert.equal(single.callsign, 'DISMOUNT g1');
  assert.equal(stale.callsign, 'CHAIR g4');
  assert.match(single.remarks, /dismount \(COCO person\), 1 source\./);
  assert.match(coasting.remarks, /2 sources, coasting/);
  assert.match(coasting.remarks, /ce 2\.40 m/);

  // Footage classes: still a-u-G; the class goes in the callsign and remarks only.
  const [mover, armour, truck] = cotEvents([ent({ gid: 'g8', class: 100 }), ent({ gid: 'g9', class: 101 }), ent({ gid: 'g10', class: 7 })], SEOUL, T0);
  assert.deepEqual([mover.type, armour.type, truck.type], ['a-u-G', 'a-u-G', 'a-u-G']);
  assert.equal(mover.callsign, 'MOVER g8');
  assert.match(mover.remarks, /mover \(unclassified ground mover: motion only, no appearance class\)/);
  assert.equal(armour.callsign, 'ARMOURED g9');
  assert.match(armour.remarks, /tank\/IFV\/APC by appearance, unverified/);
  assert.equal(truck.callsign, 'TRUCK g10');

  const withAlt = cotEvents([ent({ pos: [0, 1.5, -10] })], { ...SEOUL, altM: 38 }, T0)[0];
  assert.ok(Math.abs(withAlt.hae - 39.5) < 0.01, `hae ${withAlt.hae}`);
  assert.equal(withAlt.le, 0.35, 'le = ce: the threshold bounds the 3D error');
  assert.equal(cotEvents([ent()], SEOUL, T0, { uidPrefix: 'lab2' })[0].uid, 'lab2-g1');
});

test('CoT XML is well-formed, escaped and parses back to the same values', () => {
  const [e] = cotEvents([ent({ gid: 'g7', ce: 0.4 })], { ...SEOUL, altM: 38 }, T0, { uidPrefix: `a&b"<'>` });
  const xml = eventXml(e);
  assert.match(xml, /^<\?xml version="1\.0" encoding="UTF-8"\?>\n<event /);
  const ev = parseXml(xml);
  assert.equal(ev.name, 'event');
  assert.deepEqual(Object.keys(ev.attrs), ['version', 'uid', 'type', 'how', 'time', 'start', 'stale']);
  assert.equal(ev.attrs.version, '2.0');
  assert.equal(ev.attrs.uid, `a&b"<'>-g7`);
  assert.equal(ev.attrs.type, 'a-u-G');
  assert.equal(ev.attrs.how, 'm-p');
  assert.equal(ev.attrs.time, '2026-10-10T09:00:00.000Z');
  assert.equal(ev.attrs.start, ev.attrs.time);
  assert.equal(ev.attrs.stale, '2026-10-10T09:00:05.000Z');
  const pt = child(ev, 'point');
  assert.ok(Math.abs(Number(pt.attrs.lat) - e.lat) < 1e-7 && Math.abs(Number(pt.attrs.lon) - e.lon) < 1e-7);
  assert.equal(pt.attrs.hae, '38.00');
  assert.equal(pt.attrs.ce, '0.40');
  assert.equal(pt.attrs.le, '0.40');
  const det = child(ev, 'detail');
  assert.equal(child(det, 'contact').attrs.callsign, 'DISMOUNT g7');
  assert.deepEqual(child(det, 'track').attrs, { course: '90.0', speed: '1.20' });
  assert.equal(child(det, 'remarks').text, e.remarks);

  const noAlt = parseXml(eventXml(cotEvents([ent()], SEOUL, T0)[0]));
  assert.deepEqual([child(noAlt, 'point').attrs.hae, child(noAlt, 'point').attrs.le], ['9999999.0', '9999999.0']);

  assert.equal(xmlEscape(`<a href="x">&'\u0001\u0007ok\n`), '&lt;a href=&quot;x&quot;&gt;&amp;&apos;ok\n');
  const doc = parseXml(eventsXml(cotEvents([ent(), ent({ gid: 'g2' })], SEOUL, T0)));
  assert.equal(doc.name, 'events');
  assert.equal(doc.attrs.count, '2');
  assert.deepEqual(doc.children.map(c => c.attrs.uid), ['minband-g1', 'minband-g2']);
  assert.equal(parseXml(eventsXml([])).attrs.count, '0');
});

test('endpoint and env parsing', () => {
  assert.deepEqual(parseCotEndpoints(''), []);
  assert.deepEqual(parseCotEndpoints(' udp://239.2.3.1:6969?ttl=2&iface=192.168.1.10 , tcp://tak.local:8087 '), [
    { proto: 'udp', host: '239.2.3.1', port: 6969, url: 'udp://239.2.3.1:6969', ttl: 2, iface: '192.168.1.10' },
    { proto: 'tcp', host: 'tak.local', port: 8087, url: 'tcp://tak.local:8087' },
  ]);
  for (const bad of ['239.2.3.1:6969', 'ssl://tak:8089', 'udp://host', 'tcp://host:0', 'udp://h:1?ttl=0']) assert.throws(() => parseCotEndpoints(bad), Error, bad);
  assert.deepEqual(cotConfigFromEnv({}), { endpoints: [], opts: { hz: 1 } });
  assert.deepEqual(cotConfigFromEnv({ MINBAND_COT: 'udp://127.0.0.1:4242', MINBAND_COT_HZ: '2', MINBAND_COT_STALE_S: '10' }).opts, { hz: 2, staleS: 10 });
  assert.throws(() => cotConfigFromEnv({ MINBAND_COT_HZ: '0' }), /MINBAND_COT_HZ/);
  assert.throws(() => cotConfigFromEnv({ MINBAND_COT_STALE_S: '-1' }), /MINBAND_COT_STALE_S/);
});

test('sender: a final stale event when an entity disappears or the anchor is cleared; one log line without anchor', () => {
  const src = source([ent(), ent({ gid: 'g2' })]);
  const sent: string[] = [], logs: string[] = [];
  const out = { url: 'mem', connected: true, send: (x: string) => { sent.push(x); return true; }, close() {} };
  const s = new CotSender(src, [out], {}, l => logs.push(l));
  assert.deepEqual(s.tick().map(e => e.uid), ['minband-g1', 'minband-g2']);
  src.set([ent()]);
  const r2 = s.tick();
  assert.deepEqual(r2.map(e => [e.uid, e.stale - e.time]), [['minband-g1', 5000], ['minband-g2', 0]]);
  assert.match(r2[1].remarks, /Track ended\.$/);
  assert.equal(s.tick().length, 1, 'the final is sent once');
  src.geo = null;
  const r4 = s.tick();
  assert.deepEqual(r4.map(e => [e.uid, e.stale - e.time]), [['minband-g1', 0]]);
  assert.deepEqual(s.tick(), []);
  assert.equal(logs.filter(l => l.includes('no geodetic anchor')).length, 1);
  src.geo = SEOUL;
  assert.equal(s.tick().length, 1);
  assert.ok(logs.some(l => l.includes('anchor set')));
  assert.equal(sent.length, 2 + 2 + 1 + 1 + 1);
  for (const x of sent) parseXml(x);
  assert.deepEqual(s.counters, { rounds: 6, events: 7, staleFinals: 2, skipped: 0 });
});

test('UDP: one event per datagram to a loopback listener', async () => {
  const rx = dgram.createSocket('udp4');
  await new Promise<void>(r => rx.bind(0, '127.0.0.1', r));
  const port = (rx.address() as AddressInfo).port;
  const [spec] = parseCotEndpoints(`udp://127.0.0.1:${port}`);
  const out = new UdpOut(spec, () => {});
  const s = new CotSender(source([ent(), ent({ gid: 'g2' })]), [out]);
  const got: string[] = [];
  const done = new Promise<void>(r => rx.on('message', m => { got.push(m.toString('utf8')); if (got.length === 2) r(); }));
  s.tick();
  await done;
  assert.deepEqual(got.map(x => parseXml(x).attrs.uid).sort(), ['minband-g1', 'minband-g2']);
  s.stop(); rx.close();
});

test('TCP: streams events to a TAK-style input and reconnects after the server drops it', async () => {
  const conns: net.Socket[] = []; let buf = '';
  const server = net.createServer(c => { conns.push(c); c.on('data', d => { buf += d.toString('utf8'); }); });
  await new Promise<void>(r => server.listen(0, '127.0.0.1', r));
  const port = (server.address() as AddressInfo).port;
  const logs: string[] = [];
  const out = new TcpOut(parseCotEndpoints(`tcp://127.0.0.1:${port}`)[0], l => logs.push(l), { min: 20, max: 100 });
  const s = new CotSender(source([ent()]), [out]);
  const until = async (f: () => boolean) => { for (let i = 0; i < 200 && !f(); i++) await new Promise(r => setTimeout(r, 10)); assert.ok(f()); };

  assert.equal(s.tick().length, 1);
  assert.equal(s.counters.skipped, 1, 'not connected yet: skipped, not queued');
  await until(() => out.connected);
  s.tick();
  await until(() => buf.includes('</event>'));
  const events = buf.split('<?xml').filter(Boolean).map(x => parseXml('<?xml' + x));
  assert.equal(events[0].attrs.uid, 'minband-g1');

  conns[0].destroy(); // TAK Server restarts
  await until(() => !out.connected);
  await until(() => out.connected && conns.length === 2);
  buf = '';
  s.tick();
  await until(() => buf.includes('</event>'));
  assert.ok(logs.some(l => l.includes('disconnected')) && logs.filter(l => l.includes('connected')).length >= 2, logs.join(' | '));
  s.stop();
  for (const c of conns) c.destroy();
  await new Promise(r => server.close(r));
});

test('World snapshot carries Snapshot.geo and GlobalEntity.geo; /api/geo and /api/cot', async () => {
  const clock = new FakeClock(T0);
  const world = new World({ now: clock.now, shaper: new Shaper({ now: clock.now, schedule: clock.schedule, cancel: clock.cancel }) });
  const e = new ScriptedEdge(world, clock, '127.0.0.1:9000', 42, 1);
  wireAcks(world, [e]);
  e.run(600, { snapshotEvery: 4 });
  let snap = world.snapshot();
  assert.equal(snap.geo, null);
  assert.ok(snap.global.length >= 2 && snap.global.every(g => g.geo === null));

  const server = http.createServer(createApi(world, { cot: { hz: 1 } }));
  await new Promise<void>(r => server.listen(0, '127.0.0.1', r));
  const base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  try {
    assert.deepEqual(await (await fetch(`${base}/api/geo`)).json(), { anchor: null });
    let r = await fetch(`${base}/api/cot`);
    assert.equal(r.status, 409);
    assert.match((await r.json()).error, /no geodetic anchor/);

    r = await fetch(`${base}/api/geo?lat=37.5665&lon=126.978&heading=90&alt=38`);
    assert.equal(r.status, 200);
    assert.deepEqual(await r.json(), { anchor: { lat: 37.5665, lon: 126.978, headingDeg: 90, altM: 38, mgrs: '52S CG 21424 59640' } });
    assert.equal((await fetch(`${base}/api/geo?heading=500`)).status, 200);
    assert.equal(world.geo?.headingDeg, 140, 'partial update, heading normalised');
    r = await fetch(`${base}/api/geo?lat=99`);
    assert.equal(r.status, 400);
    assert.equal(world.geo?.lat, 37.5665, 'a bad request changes nothing');
    await fetch(`${base}/api/geo?heading=90`);

    snap = world.snapshot();
    assert.deepEqual(snap.geo, { lat: 37.5665, lon: 126.978, headingDeg: 90, mgrs: '52S CG 21424 59640' });
    for (const g of snap.global) {
      assert.ok(g.geo, g.gid);
      const enu = geodeticToEnu(g.geo.lat, g.geo.lon, 38, 37.5665, 126.978, 38);
      // heading 90: forward (-Z) is east and +X is south.
      assert.ok(Math.abs(enu[0] - -g.pos[2]) < 0.01 && Math.abs(enu[1] - -g.pos[0]) < 0.01, `${g.gid}: ${enu} vs ${g.pos}`);
      assert.match(g.geo.mgrs, /^52S CG \d{5} \d{5}$/);
    }

    r = await fetch(`${base}/api/cot`);
    assert.equal(r.status, 200);
    assert.match(r.headers.get('content-type') ?? '', /application\/xml/);
    const doc = parseXml(await r.text());
    assert.equal(Number(doc.attrs.count), snap.global.length);
    assert.deepEqual(doc.children.map(c => c.attrs.uid).sort(), snap.global.map(g => `minband-${g.gid}`).sort());
    for (const c of doc.children) {
      assert.equal(c.attrs.type, 'a-u-G');
      assert.ok(Number(child(c, 'point').attrs.lat) > 37.56);
    }

    assert.deepEqual(await (await fetch(`${base}/api/geo?mgrs=52SCG2142459640&heading=0`)).json(),
      { anchor: { lat: world.geo!.lat, lon: world.geo!.lon, headingDeg: 0, altM: 38, mgrs: '52S CG 21424 59640' } });
    assert.deepEqual(await (await fetch(`${base}/api/geo?clear=1`)).json(), { anchor: null });
    assert.equal(world.snapshot().geo, null);
    assert.equal((await fetch(`${base}/api/geo?bogus=1`)).status, 400);
  } finally {
    await new Promise(r => server.close(r));
    e.free();
  }
});
