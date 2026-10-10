// Smoke test against a running server (mock or real): endpoints, then the WebSocket contract.
// usage: node scripts/smoke.mjs [http://localhost:8090]
import WebSocket from 'ws';
const base = process.argv[2] || 'http://localhost:8090';
const fail = (m) => { console.error('FAIL', m); process.exit(1); };
const ok = (m) => console.log('ok ', m);

const meta = await (await fetch(`${base}/meta`)).json();
for (const k of ['fps', 'width', 'height', 'start_frame', 'every', 'ground', 'origin', 'homographies']) if (!(k in meta)) fail(`meta.${k}`);
ok(`/meta: ${Object.keys(meta.homographies).length} homographies, origin ${meta.origin.map((x) => x.toFixed(2))}`);
const v = await fetch(`${base}/video.mp4`, { headers: { Range: 'bytes=100-199' } });
if (v.status !== 206 || v.headers.get('content-range')?.indexOf('bytes 100-199/') !== 0) fail(`video range: ${v.status} ${v.headers.get('content-range')}`);
ok(`/video.mp4 range -> 206 ${v.headers.get('content-range')}`);
for (const p of ['/', '/app.js', '/camera.js']) { const r = await fetch(base + p); if (!r.ok) fail(p); }
ok('static page files');

const ws = new WebSocket(base.replace(/^http/, 'ws'));
const msgs = [];
const next = () => new Promise((res, rej) => { ws.once('message', (d) => res(JSON.parse(String(d)))); setTimeout(() => rej(new Error('timeout')), 3000); });
await new Promise((res, rej) => { ws.once('open', res); ws.once('error', rej); });
for (let i = 0; i < 5; i++) msgs.push(await next());
const m = msgs[msgs.length - 1];
for (const k of ['t', 'clipT', 'edge', 'wire', 'rx']) if (!(k in m)) fail(`msg.${k}`);
for (const k of ['tracks', 'contacts', 'ego']) if (!(k in m.edge)) fail(`edge.${k}`);
for (const k of ['frames', 'budgetBps', 'profile', 'up', 'bytesPerS', 'dropped']) if (!(k in m.wire)) fail(`wire.${k}`);
for (const k of ['contacts', 'ego', 'events', 'known', 'of', 'bytesTotal']) if (!(k in m.rx)) fail(`rx.${k}`);
const dts = msgs.slice(1).map((x, i) => x.t - msgs[i].t);
ok(`5 messages, t ${msgs[0].t.toFixed(2)} -> ${m.t.toFixed(2)} (dt ${dts.map((d) => d.toFixed(2)).join(',')}), ${m.edge.tracks.length} tracks, ${m.edge.contacts.length} edge contacts, ${m.rx.contacts.length} rx contacts`);
if (m.edge.tracks.length) { const tr = m.edge.tracks[0]; for (const k of ['id', 'cls', 'e', 'n', 've', 'vn', 'conf']) if (!(k in tr)) fail(`track.${k}`); }
if (m.edge.contacts.length) {
  const c = m.edge.contacts[0];
  for (const k of ['id', 'rev', 'e', 'n', 'ce', 'radius', 'count', 'mix', 'motion', 'confirmed', 'lost', 'departed', 'focused', 'course', 'speed', 'members', 'firstSeen', 'since']) if (!(k in c)) fail(`contact.${k}`);
}
if (m.rx.contacts.length) { const c = m.rx.contacts[0]; for (const k of ['ceShown', 'liveness', 'ageS']) if (!(k in c)) fail(`rx.contact.${k}`); }
ok('field names match the contract');

ws.send(JSON.stringify({ cmd: 'seek', t: 30 }));
let s; do s = await next(); while (Math.abs(s.t - 30) > 1.5);
ok(`seek 30 -> t=${s.t.toFixed(2)}`);
ws.send(JSON.stringify({ cmd: 'pause' }));
await next(); await next(); const p1 = await next(), p2 = await next();
if (p1.t !== p2.t) fail(`pause: t still advancing ${p1.t} -> ${p2.t}`);
ok(`pause holds t=${p1.t.toFixed(2)}`);
ws.send(JSON.stringify({ cmd: 'rate', x: 4 })); ws.send(JSON.stringify({ cmd: 'play' }));
await next(); const r1 = await next(), r2 = await next();
if (r2.t - r1.t < 0.3) fail(`rate 4: dt ${r2.t - r1.t}`);
ok(`rate 4x: dt ${(r2.t - r1.t).toFixed(2)} s per message`);
ws.send(JSON.stringify({ cmd: 'rate', x: 1 }));
ws.send(JSON.stringify({ cmd: 'link', profile: 'blackout' }));
await next(); const b = await next();
if (b.wire.profile !== 'blackout' || b.wire.up !== false) fail('blackout');
ok(`link blackout: up=${b.wire.up} budget=${b.wire.budgetBps}`);
ws.send(JSON.stringify({ cmd: 'link', profile: 'clean' }));
await next(); const c = await next();
if (c.wire.profile !== 'clean') fail('clean');
ok(`link clean: budget=${c.wire.budgetBps}`);
if (c.edge.contacts.length) {
  const id = c.edge.contacts[0].id;
  ws.send(JSON.stringify({ cmd: 'focus', id, mode: 'track' }));
  await next(); const f = await next();
  if (!f.edge.contacts.find((x) => x.id === id)?.focused) fail('focus');
  ok(`focus track contact ${id} -> focused`);
}
// wait for some wire frames and events
let frames = 0, events = 0, lastT = 0;
for (let i = 0; i < 40; i++) { const x = await next(); frames += x.wire.frames.length; events += x.rx.events.length; lastT = x.t; }
ok(`4 s of clean link: ${frames} frames, ${events} events, t=${lastT.toFixed(1)}`);
if (frames === 0) fail('no frames');
ws.close();
console.log('SMOKE PASS');
