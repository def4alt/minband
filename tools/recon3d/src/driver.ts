// The 3D operator page's driver: the edge's 3D tracks (tools/recon3d/lift.py: detections placed in the
// MASt3R-SLAM reconstruction, in metres, with a height) go through the Rust/WASM edge, real wire frames,
// a shaped link and the Rust/WASM receiver. The page renders what the receiver decoded inside the
// reconstructed scene: each object as its own reconstructed surface points, moving.
//
//   npm start                      http://localhost:8092
//   PORT=8093 SCENE=vd076 PROFILE=lora npm start      (scenes: every runs/recon3d/<run>/scene; RUN_DIR=a,b to pick)
//
// What is not on the wire yet, said plainly:
// - The static map. It is 3d-map-stream's job (its own 10-20 kbit/s channel); the page loads it whole.
// - The objects' appearance ("chips"). The wire has ChipHead/ChipSym (PROTOCOL.md 3.5) but the core does
//   not emit them yet; here a chip goes through a simulated channel with its real compressed size, at
//   most half the link (the spec's chip cap), and the page shows a class stand-in until it arrives.
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { WebSocketServer, WebSocket } from 'ws';

const require = createRequire(import.meta.url);
const core = require('../../../core/pkg-node/minband_core.js');
const { WasmEdge, WasmReceiver, describe_frame } = core;

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, '../../..');
const PORT = Number(process.env.PORT || 8092);
// Scenes: every run dir under runs/recon3d with a scene/ (lift.py output), or RUN_DIR=a,b to pick.
const RUNS_ROOT = path.join(repo, 'runs/recon3d');
const RUN_DIRS = (process.env.RUN_DIR ? process.env.RUN_DIR.split(',').map((d) => path.resolve(repo, d))
  : fs.existsSync(RUNS_ROOT) ? fs.readdirSync(RUNS_ROOT).map((d) => path.join(RUNS_ROOT, d)) : [])
  .filter((d) => fs.existsSync(path.join(d, 'scene', 'tracks3d.csv'))).sort();
if (!RUN_DIRS.length) { console.error(`no scenes: run tools/recon3d/run.sh first (looked in ${process.env.RUN_DIR || RUNS_ROOT})`); process.exit(1); }
const WEB = path.join(here, '../web');
const THREE_DIR = path.join(here, '../node_modules/three');
const TICK_HZ = 120, STEP_S = 0.1, UDP_IP_OVERHEAD = 28;
// No GNSS in the footage: an arbitrary origin (VisDrone clips are Chinese cities; the sites are not known).
const ORIGIN_LAT = 34.75, ORIGIN_LON = 113.62;
// Without a budget (the video regime, megabits) chips get this much of the fat link.
const CHIP_BPS_FAT = 400_000;

// ---- data ----------------------------------------------------------------------------------------
type Row = { id: number; cls: number; e: number; n: number; u: number; ve: number; vn: number; conf: number; bbox: number[]; ce: number };
type Cam = { tick: number; e: number; n: number; u: number; yaw: number; pitch: number; roll: number };
type Scene = { name: string; dir: string; summary: any; byTick: Map<number, Row[]>; ticks: number[]; cams: Cam[];
  chips: Record<string, { cls: number; ready_tick: number; wire_bytes: number }>; lastTick: number; durationS: number; hfov: number };

function loadScene(dir: string): Scene {
  const sc = path.join(dir, 'scene');
  const summary = JSON.parse(fs.readFileSync(path.join(sc, 'summary.json'), 'utf8'));
  const byTick = new Map<number, Row[]>();
  for (const line of fs.readFileSync(path.join(sc, 'tracks3d.csv'), 'utf8').split('\n').slice(1)) {
    if (!line) continue;
    const c = line.split(',').map(Number);
    const r: Row = { id: c[1], cls: c[2], e: c[3], n: c[4], u: c[5], ve: c[6], vn: c[7], conf: c[9], bbox: c.slice(10, 14), ce: c[14] };
    let a = byTick.get(c[0]);
    if (!a) byTick.set(c[0], (a = []));
    a.push(r);
  }
  const ticks = [...byTick.keys()].sort((a, b) => a - b);
  const cams: Cam[] = fs.readFileSync(path.join(sc, 'cameras.csv'), 'utf8').split('\n').slice(1).filter(Boolean).map((l) => {
    const c = l.split(',').map(Number);
    return { tick: c[0], e: c[1], n: c[2], u: c[3], yaw: c[4], pitch: c[5], roll: c[6] };
  });
  const chips = JSON.parse(fs.readFileSync(path.join(sc, 'chips.json'), 'utf8'));
  const lastTick = Math.max(ticks[ticks.length - 1] ?? 0, cams[cams.length - 1].tick);
  const durationS = Number(process.env.DURATION_S) || Math.ceil(lastTick / TICK_HZ + 30);
  console.log(`scene ${path.basename(dir)}: ${ticks.length} ticks of tracks, ${cams.length} camera poses, ${Object.keys(chips).length} chips, ${(lastTick / TICK_HZ).toFixed(1)} s of footage`);
  return { name: path.basename(dir), dir, summary, byTick, ticks, cams, chips, lastTick, durationS, hfov: summary.hfov_deg };
}
const scenes = new Map(RUN_DIRS.map((d) => [path.basename(d), d]));
let S = loadScene(RUN_DIRS.find((d) => path.basename(d) === process.env.SCENE) ?? RUN_DIRS[0]);

function lastAtOrBefore<T>(arr: T[], key: (x: T) => number, tick: number): number {
  let lo = 0, hi = arr.length - 1, best = -1;
  while (lo <= hi) { const m = (lo + hi) >> 1; if (key(arr[m]) <= tick) { best = m; lo = m + 1; } else hi = m - 1; }
  return best;
}
function tracksAt(tick: number): Row[] {
  const k = lastAtOrBefore(S.ticks, (x) => x, tick);
  if (k < 0 || tick - S.ticks[k] > 6 || tick > S.lastTick) return [];
  return S.byTick.get(S.ticks[k])!;
}
function camAt(tick: number): Cam { return S.cams[Math.max(0, lastAtOrBefore(S.cams, (c) => c.tick, tick))]; }

// ---- the drone: the reconstructed camera trajectory is its telemetry --------------------------------
function egoAt(tick: number, video: boolean) {
  const c = camAt(tick), p = camAt(tick - 30);
  const dt = Math.max(1e-3, (c.tick - p.tick) / TICK_HZ);
  const speed = Math.hypot(c.e - p.e, c.n - p.n) / dt;
  const dep = Math.max(5, -c.pitch) * Math.PI / 180;
  const reach = Math.min(400, c.u / Math.tan(dep));               // optical axis to the ground
  const yaw = c.yaw * Math.PI / 180;
  return {
    e: c.e, n: c.n, alt_agl: c.u, heading_deg: c.yaw, speed, climb: (c.u - p.u) / dt, nav_mode: 1, gnss: 0, battery: 80,
    pos_ce: 5.0, fp_e: c.e + reach * Math.sin(yaw), fp_n: c.n + reach * Math.cos(yaw), fp_radius: Math.min(500, reach * Math.tan((S.hfov / 2) * Math.PI / 180)),
    video, looking: tick <= S.lastTick,
  };
}

// ---- link profiles (the side-by-side page's: HACKATHON_PLAN.md 3.3) --------------------------------
type Profile = 'clean' | 'hf' | 'lora' | 'telemetry' | 'blackout';
const PROFILES: Record<Profile, { budgetBps: number; loss: number; delayS: number; up: boolean; video: boolean }> = {
  clean: { budgetBps: 0, loss: 0, delayS: 0.02, up: true, video: true },
  hf: { budgetBps: 9600, loss: 0.01, delayS: 0.5, up: true, video: false },
  lora: { budgetBps: 2000, loss: 0.1, delayS: 0.3, up: true, video: false },
  telemetry: { budgetBps: 600, loss: 0.05, delayS: 0.05, up: true, video: false },
  blackout: { budgetBps: 2000, loss: 1, delayS: 0.3, up: false, video: false },
};

function unpack(buf: Uint8Array): Uint8Array[] {
  const out: Uint8Array[] = []; let i = 0;
  while (i + 2 <= buf.length) { const n = buf[i] | (buf[i + 1] << 8); i += 2; out.push(buf.subarray(i, i + n)); i += n; }
  return out;
}
const r1 = (x: number) => Math.round(x * 10) / 10;
const r2 = (x: number) => Math.round(x * 100) / 100;

class Replay {
  t = 0; playing = true; profile: Profile = (process.env.PROFILE as Profile) || 'clean';
  edge: any; rx: any;
  pending: { at: number; bytes: Uint8Array; up: boolean }[] = [];
  delivered: { t: number; bytes: number }[] = [];
  lastDigest = -1e9; bytesTotal = 0; frames = 0; dropped = 0;
  // The simulated chip channel: contact id -> chip (track id), bytes left, delivered.
  chipQueue: { contact: number; chip: string; left: number }[] = [];
  chipsFor = new Map<number, string>();
  chipBytes = 0;

  constructor() { this.reset(0); }

  reset(t: number) {
    this.t = t;
    const P = PROFILES[this.profile];
    this.edge = new WasmEdge(JSON.stringify({
      device_id: 1, nonce: (Math.random() * 2 ** 31) >>> 0, origin_lat_e7: Math.round(ORIGIN_LAT * 1e7), origin_lon_e7: Math.round(ORIGIN_LON * 1e7), origin_alt: 32767,
      pos_res: 2, caps: 2 | 4 | 8 | 0x200 | 0x400 | 0x800, hfov_x10: Math.round(S.hfov * 10), img_w: S.summary.width, img_h: S.summary.height,
      video_frame0: 0, fps_x100: Math.round(S.summary.fps * 100), budget_bps: P.budgetBps, max_frame: 1200,
      sigma_own: 2.0, sigma_att_deg: 0.5, sigma_h: 0.5, sigma_px: 2.0, f_px: S.summary.f_px,
      // Individuals on a fat link: a parked row of cars 5 m apart stays five objects. The edge still
      // coarsens on its own when the link cannot carry them (PROTOCOL.md 6.4).
      contacts: { link_m: 3.0, link_dismount_m: 1.5 },
    }));
    this.rx = new WasmReceiver(P.budgetBps);
    this.pending = []; this.delivered = []; this.lastDigest = -1e9; this.bytesTotal = 0; this.frames = 0; this.dropped = 0;
    this.chipQueue = []; this.chipsFor = new Map(); this.chipBytes = 0;
  }

  step() {
    if (this.playing) { this.t += STEP_S; if (this.t >= S.durationS) this.reset(0); }
    const t = this.t, tick = Math.round(t * TICK_HZ), P = PROFILES[this.profile];
    const tracks = tracksAt(tick);
    const ego = egoAt(tick, P.video);
    const cam = camAt(tick);
    let sent = 0;
    if (this.playing) {
      if (tick <= S.lastTick) this.edge.pose(tick, cam.e, cam.n, cam.u, cam.yaw, cam.pitch, cam.roll);
      // ce: the tracker's own error radius in the reconstruction's frame (lift.py), not a GNSS model.
      const tj = JSON.stringify(tracks.map((x) => ({ id: x.id, class: x.cls, e: x.e, n: x.n, u: x.u, ve: x.ve, vn: x.vn, conf: x.conf, bbox: x.bbox, ce: x.ce })));
      for (const b of unpack(this.edge.tick(tj, JSON.stringify(ego), tick))) {
        const bytes = new Uint8Array(b);
        this.frames++; sent += bytes.length;
        if (P.up && Math.random() >= P.loss) this.pending.push({ at: t + P.delayS, bytes, up: false }); else this.dropped++;
      }
      if (P.up && t - this.lastDigest >= 5 && this.rx.needs_digest()) {
        this.lastDigest = t;
        const up = this.rx.make_digest(P.budgetBps, tick);
        if (Math.random() >= P.loss) this.pending.push({ at: t + P.delayS, bytes: up, up: true });
      }
    }
    this.pending.sort((a, b) => a.at - b.at);
    while (this.pending.length && this.pending[0].at <= t) {
      const p = this.pending.shift()!;
      try {
        if (p.up) this.edge.on_uplink(p.bytes, tick);
        else { this.rx.on_frame(p.bytes); this.bytesTotal += p.bytes.length; this.delivered.push({ t, bytes: p.bytes.length + UDP_IP_OVERHEAD }); }
      } catch (e) { console.warn('decode', e); }
    }
    this.delivered = this.delivered.filter((d) => t - d.t <= 5);
    if (tick % (10 * TICK_HZ) === 0) this.rx.gc(tick);

    const es = JSON.parse(this.edge.snapshot_json(tick));
    const members = new Map<number, number[]>(es.contacts.map((c: any) => [c.id, c.members]));
    const rxs = JSON.parse(this.rx.snapshot_json(tick));
    // Chips: a single-object contact the receiver holds, whose object's chip exists by now, is queued
    // once; the channel drains at half the link (or a fat share on the video link) while the link is up.
    for (const c of rxs) {
      if (c.departed || c.count !== 1 || this.chipsFor.has(c.id) || this.chipQueue.some((q) => q.contact === c.id)) continue;
      const m = members.get(c.id);
      if (!m || m.length !== 1) continue;
      const ch = S.chips[String(m[0])];
      if (!ch || ch.ready_tick > tick) continue;
      this.chipQueue.push({ contact: c.id, chip: String(m[0]), left: ch.wire_bytes });
    }
    if (this.playing && P.up && this.chipQueue.length) {
      let budget = ((P.budgetBps > 0 ? 0.5 * P.budgetBps : CHIP_BPS_FAT) / 8) * STEP_S * (1 - P.loss);
      while (budget > 0 && this.chipQueue.length) {
        const q = this.chipQueue[0];
        const take = Math.min(budget, q.left);
        q.left -= take; budget -= take; this.chipBytes += take;
        if (q.left <= 0) { this.chipsFor.set(q.contact, q.chip); this.chipQueue.shift(); }
      }
    }

    const ray = (c: any) => (c.ray ? [r1(c.ray[0]), r1(c.ray[1])] : null);
    const poses = JSON.parse(this.rx.poses_json());
    const lastPose = poses.length ? poses[poses.length - 1] : null;
    const rawEgo = JSON.parse(this.rx.ego_json());
    return {
      t: Math.round(t * 1000) / 1000, duration: S.durationS, footageS: r1(S.lastTick / TICK_HZ), playing: this.playing, scene: S.name, scenes: [...scenes.keys()],
      profile: this.profile, budgetBps: P.budgetBps, video: ego.video && tick <= S.lastTick,
      wire: { bytesPerS: Math.round(this.delivered.reduce((s, d) => s + d.bytes, 0) / 5), sentNow: sent, frames: this.frames, dropped: this.dropped, total: this.bytesTotal,
        chipBytes: Math.round(this.chipBytes), chipQueue: this.chipQueue.length, chipsDelivered: this.chipsFor.size },
      edge: { tracks: tracks.map((x) => ({ id: x.id, cls: x.cls, e: r2(x.e), n: r2(x.n), u: r2(x.u), bbox: x.bbox })),
        cam: { e: r2(cam.e), n: r2(cam.n), u: r2(cam.u), yaw: r1(cam.yaw), pitch: r1(cam.pitch), roll: r1(cam.roll) },
        contacts: es.contacts.filter((c: any) => !c.departed).length, level: es.detail.level, linkM: r1(es.detail.link_m) },
      rx: {
        contacts: rxs.filter((c: any) => !c.departed).map((c: any) => ({
          id: c.id, e: r2(c.e), n: r2(c.n), u: c.u, ceShown: r1(c.ce_shown), radius: r1(c.radius), count: c.count, mix: c.mix, motion: c.motion,
          course: Math.round(c.course), speed: r1(c.speed), liveness: c.liveness, lost: c.lost, group: c.group, located: c.located, silenceS: r1(c.silence_s), ray: ray(c),
          chip: this.chipsFor.get(c.id) ?? null })),
        pose: lastPose ? { tick: lastPose.tick, e: lastPose.x / 100, n: lastPose.y / 100, u: lastPose.z / 100, yaw: lastPose.yaw / 100, pitch: lastPose.pitch / 100, roll: lastPose.roll / 100 } : null,
        ego: rawEgo ? { e: rawEgo.rec.dx, n: rawEgo.rec.dy, altAgl: rawEgo.rec.alt_agl, heading: Math.round(rawEgo.rec.heading * 360 / 256), ageS: r1((tick - rawEgo.tick) / TICK_HZ) } : null,
        known: this.rx.known(), of: this.rx.of(), events: JSON.parse(this.rx.events_json()).slice(-12).map((e: any) => ({ t: r1(e.tick / TICK_HZ), text: e.text })),
      },
    };
  }

  command(m: any) {
    switch (m.cmd) {
      case 'pause': this.playing = false; break;
      case 'play': this.playing = true; break;
      case 'seek': this.reset(Math.max(0, Math.min(S.durationS - 0.1, Number(m.t) || 0))); break;
      case 'scene': { const d = scenes.get(String(m.name)); if (d && path.basename(d) !== S.name) { S = loadScene(d); this.reset(0); } break; }
      case 'link':
        if (!(m.profile in PROFILES)) break;
        this.profile = m.profile;
        this.edge.set_budget(PROFILES[this.profile].budgetBps); this.rx.set_budget(PROFILES[this.profile].budgetBps);
        break;
    }
  }
}

// ---- http + ws ------------------------------------------------------------------------------------
const MIME: Record<string, string> = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.json': 'application/json', '.bin': 'application/octet-stream', '.mp4': 'video/mp4' };
const replay = new Replay();

function sendFile(res: http.ServerResponse, file: string, req: http.IncomingMessage) {
  if (!fs.existsSync(file)) { res.writeHead(404); res.end(`missing ${file}`); return; }
  const size = fs.statSync(file).size;
  const type = MIME[path.extname(file)] || 'application/octet-stream';
  const range = req.headers.range && /bytes=(\d*)-(\d*)/.exec(req.headers.range);
  if (range) {
    const start = range[1] ? Number(range[1]) : 0, end = Math.min(range[2] ? Number(range[2]) : size - 1, size - 1);
    res.writeHead(206, { 'content-type': type, 'accept-ranges': 'bytes', 'content-range': `bytes ${start}-${end}/${size}`, 'content-length': end - start + 1 });
    fs.createReadStream(file, { start, end }).pipe(res);
  } else {
    res.writeHead(200, { 'content-type': type, 'accept-ranges': 'bytes', 'content-length': size, 'cache-control': 'no-cache' });
    fs.createReadStream(file).pipe(res);
  }
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url || '/', 'http://x');
  const p = url.pathname;
  // Scene files of the scene playing now; the page adds ?s=<name> so a switch is never served from cache.
  if (p === '/summary.json' || p === '/map.bin' || p === '/map.json' || p === '/chips.json') return sendFile(res, path.join(S.dir, 'scene', p.slice(1)), req);
  if (p === '/video.mp4') return sendFile(res, path.join(S.dir, 'clip-720p.mp4'), req);
  if (p.startsWith('/three/')) {
    const f = path.normalize(path.join(THREE_DIR, p.slice('/three/'.length)));
    if (f.startsWith(THREE_DIR)) return sendFile(res, f, req);
  }
  const f = path.normalize(path.join(WEB, p === '/' ? 'index.html' : p));
  if (!f.startsWith(WEB) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) { res.writeHead(404); res.end('not found'); return; }
  sendFile(res, f, req);
});
const wss = new WebSocketServer({ server });
wss.on('connection', (ws) => ws.on('message', (d) => { try { replay.command(JSON.parse(String(d))); } catch { /* ignore */ } }));
setInterval(() => {
  let msg: string;
  try { msg = JSON.stringify(replay.step()); } catch (e) { console.error('step', e); return; }
  for (const c of wss.clients) if (c.readyState === WebSocket.OPEN) c.send(msg);
}, STEP_S * 1000);
server.listen(PORT, () => console.log(`recon3d driver: http://localhost:${PORT}  scenes ${[...scenes.keys()].join(', ')}  playing ${S.name}  profile ${replay.profile}`));
