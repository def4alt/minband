// Synthetic edge: runs the real WASM Edge over a scripted scene and sends real UDP datagrams to
// the server, so the full pipeline (shaper, receiver, fusion, viewer) works with no phone.
//   npm run sim                           # one device
//   DEVICES=2 npm run sim                 # two devices observing the same walkers (fusion demo)
//   SCENE=spread DEVICES=8 npm run sim    # eight independent one-walker feeds, each in its own area (drones per link)
//   SCENE=spread WALKERS=3 npm run sim    # three walkers per area
//   GT_POST_MS=0 npm run sim              # do not upload ground truth (default: last 10 s every 5 s)
//   TRACKS=runs/footage/<clip>/tracks.csv npm run sim   # replay a track log (phone, real drone footage), looping
// Ground truth: the scene the edge saw is POSTed to /api/ground-truth so the server's twin-error
// metric (and the viewer's readout) works without a phone. Device ids are 100 + d.
// Pose: each device has a moving camera (src/scenes.ts: its own orbit around the shared scene, or a
// drone orbit over its area), offered to the edge at 2 Hz like the phone; core sends it at the
// budget's pose interval (0.5 s unlimited, 10 s below 4 kbit/s) with originLocked true.
import dgram from 'node:dgram';
import { WasmEdge, describe } from 'minband-core';
import { TICK_HZ } from './types.js';
import { SCENES, fixedCamera, logScene, sharedCamera, sharedScene, spreadCamera, spreadScene, type SceneName } from './scenes.js';

const HOST = process.env.MINBAND_HOST ?? '127.0.0.1';
const PORT = Number(process.env.MINBAND_UDP_PORT ?? 7777);
const DEVICES = Number(process.env.DEVICES ?? 1);
// TRACKS=<csv>: replay a track log (phone or tools/footage) instead of a synthetic scene; implies SCENE=log.
const TRACKS = process.env.TRACKS;
const SCENE = (TRACKS ? 'log' : process.env.SCENE ?? 'shared') as SceneName;
// The camera that filmed the log, "x,y,z" in the log's frame (tools/footage summary.json: camera_m).
const TRACKS_CAMERA = (process.env.TRACKS_CAMERA ?? '0,40,40').split(',').map(Number) as [number, number, number];
const WALKERS = Number(process.env.WALKERS ?? 1);
const VERBOSE = !!process.env.VERBOSE;
const API = process.env.MINBAND_API ?? `http://${HOST}:${process.env.MINBAND_WS_PORT ?? 8080}`;
const GT_POST_MS = Number(process.env.GT_POST_MS ?? 5000);
const GT_WINDOW_TICKS = Number(process.env.GT_WINDOW_S ?? 10) * TICK_HZ;
const STATS_MS = 5000;
const UDP_IP_OVERHEAD = 28;
const POSE_EVERY_TICKS = TICK_HZ / 2;

if (!SCENES.includes(SCENE)) { console.error(`SCENE must be one of ${SCENES.join(', ')}`); process.exit(2); }
if (SCENE === 'log' && !TRACKS) { console.error('SCENE=log needs TRACKS=<csv>'); process.exit(2); }

function unpack(buf: Uint8Array): Uint8Array[] {
  const out: Uint8Array[] = []; let i = 0;
  while (i + 2 <= buf.length) { const n = buf[i] | (buf[i + 1] << 8); i += 2; out.push(buf.subarray(i, i + n)); i += n; }
  return out;
}

/** Bytes sent since the last stats line, incl. 28 B UDP/IP per datagram. */
const sims: { deviceId: number; edge: WasmEdge; bytes: number }[] = [];
// Each device starts at its own phase (golden-ratio spread over up to 10 s): real edges are not
// phase-locked, and eight edges keyframing and posing in the same instant overrun a narrow link's
// queue in a way real drones would not.
const phaseMs = (d: number) => Math.round(((d * 0.6180339887) % 1) * Math.min(10_000, 1250 * DEVICES));

for (let d = 0; d < DEVICES; d++) setTimeout(() => startDevice(d), DEVICES > 1 ? phaseMs(d) : 0);

function startDevice(d: number) {
  const deviceId = 100 + d;
  const scene = SCENE === 'log' ? logScene(TRACKS!).scene : SCENE === 'spread' ? spreadScene(d, DEVICES, WALKERS) : sharedScene(deviceId);
  const camera = SCENE === 'log' ? fixedCamera(TRACKS_CAMERA) : SCENE === 'spread' ? spreadCamera(d, DEVICES) : sharedCamera(d, DEVICES);
  const edge = new WasmEdge(deviceId, Math.floor(Math.random() * 2 ** 31));
  const sock = dgram.createSocket('udp4');
  sock.on('message', m => edge.on_datagram(new Uint8Array(m)));
  const t0 = Date.now();
  let tick = 0;
  const sim = { deviceId, edge, bytes: 0 };
  sims.push(sim);
  const gt: { tick: number; line: string }[] = [];
  setInterval(() => {
    const now = Date.now();
    const target = Math.floor((now - t0) / 1000 * TICK_HZ);
    while (tick <= target) {
      const tracks = scene(tick);
      if (GT_POST_MS > 0) for (const tr of tracks) gt.push({ tick, line: [tick, tr.id, tr.class, ...tr.pos.map(v => v.toFixed(4)), ...tr.vel.map(v => v.toFixed(4)), tr.conf].join(',') });
      const out = unpack(edge.tick(JSON.stringify(tracks), tick));
      if (tick % POSE_EVERY_TICKS === 0) { // offered at 2 Hz like the phone; core sends it at the budget's pose interval
        const c = camera(tick);
        const pose = edge.pose(...c.pos, ...c.quat, true, tick);
        if (pose.length) out.push(pose);
      }
      for (const dg of out) {
        sock.send(dg, PORT, HOST);
        sim.bytes += dg.length + UDP_IP_OVERHEAD;
        if (VERBOSE) console.log(`[${deviceId}] ${describe(dg)} (${dg.length} B)`);
      }
      tick++;
    }
  }, 1000 / 30);
  if (GT_POST_MS > 0) setInterval(async () => {
    while (gt.length && gt[0].tick < tick - GT_WINDOW_TICKS) gt.shift();
    const body = 'tick,id,class,x,y,z,vx,vy,vz,conf\n' + gt.map(r => r.line).join('\n') + '\n';
    try {
      const r = await fetch(`${API}/api/ground-truth?deviceId=${deviceId}`, { method: 'POST', body, headers: { 'content-type': 'text/csv' } });
      const j = await r.json() as { meanM?: number | null; p95M?: number | null; samples?: number; error?: string };
      if (VERBOSE || !r.ok) console.log(`[${deviceId}] twin error: ${r.ok ? `mean ${j.meanM?.toFixed(3)} m p95 ${j.p95M?.toFixed(3)} m n=${j.samples}` : j.error}`);
    } catch { /* server not up yet */ }
  }, GT_POST_MS);
}

setInterval(() => {
  let total = 0;
  for (const s of sims) {
    console.log(`[${s.deviceId}] ${(s.bytes * 1000 / STATS_MS).toFixed(1)} B/s ${s.edge.stats_json()}`);
    total += s.bytes; s.bytes = 0;
  }
  if (sims.length > 1) console.log(`[all] ${(total * 1000 / STATS_MS).toFixed(1)} B/s = ${(total * 8 / STATS_MS).toFixed(2)} kbit/s offered by ${sims.length} devices`);
}, STATS_MS);
console.log(`sim: ${DEVICES} device(s), scene ${SCENE}${SCENE === 'spread' ? ` (${WALKERS} walker(s) each)` : ''} -> ${HOST}:${PORT}`);
