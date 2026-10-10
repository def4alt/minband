// The real replay driver for the side-by-side page: the footage tracks (detector + tracker output
// on the actual clip, runs/footage/<run>/tracks.csv) go through the Rust/WASM edge, real wire
// frames, a shaped link (rate, delay, loss, blackouts) and the Rust/WASM receiver. The page gets
// edge truth, the frames, and what the receiver decoded, on the contract in README.md.
//
//   npm start                                   http://localhost:8090
//   PORT=8091 RUN_DIR=... VIDEO=... PROFILE=lora npm start
//
// The only thing not from the footage is the drone's own telemetry (the clip ships none): the camera
// height and pitch are the tracker's fit on the same footage, the position is the camera nadir in
// the tracks frame, the origin lat/lon is the Muscatatuck site.
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { WebSocketServer, WebSocket } from 'ws';
// @ts-ignore plain ESM helper shared with the check scripts
import { loadMeta } from '../scripts/meta.mjs';
import { loadBoxes } from '../scripts/boxes.mjs';

const require = createRequire(import.meta.url);
const core = require('../../../core/pkg-node/minband_core.js');
const { WasmEdge, WasmReceiver, describe_frame } = core;

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, '../../..');
const PORT = Number(process.env.PORT || 8090);
// Default clip: the MEVA 1080p pass at 24-28 m (cars ~190 px) with the track-level consensus of two VisDrone-fine-tuned detectors
// (tools/footage/consensus.py); RUN_DIR/VIDEO switch to any other run.
const RUN_DIR = path.resolve(repo, process.env.RUN_DIR || 'runs/footage/meva-uav-0307-1720/best2'); // env paths: absolute or relative to the repo
const VIDEO = path.resolve(repo, process.env.VIDEO || 'runs/sidebyside/meva1080-720p.mp4');
const WEB = path.join(here, '../web');
const TICK_HZ = 120, STEP_S = 0.1;
const UDP_IP_OVERHEAD = 28;
// Muscatatuck Urban Training Center, Indiana (the MEVA site).
const ORIGIN_LAT = 39.0466, ORIGIN_LON = -85.5207;

// ---- data ----------------------------------------------------------------------------------------
type Track = { id: number; cls: number; e: number; n: number; ve: number; vn: number; conf: number };
const meta = loadMeta(RUN_DIR) as any;
const byTick = new Map<number, Track[]>();
for (const line of fs.readFileSync(path.join(RUN_DIR, 'tracks.csv'), 'utf8').split('\n').slice(1)) {
  if (!line) continue;
  const c = line.split(',');
  const tick = Number(c[0]);
  // ENU: east = x, north = -z, up = y (tracks frame: x right, z toward the camera).
  const tr: Track = { id: +c[1], cls: +c[2], e: +c[3], n: -c[5], ve: +c[6], vn: -c[8], conf: +c[9] };
  let arr = byTick.get(tick);
  if (!arr) byTick.set(tick, (arr = []));
  arr.push(tr);
}
const ticks = [...byTick.keys()].sort((a, b) => a - b);
const DURATION_S = Number(process.env.DURATION_S) || Math.ceil((ticks[ticks.length - 1] + 60) / TICK_HZ);
const LAST_S = ticks[ticks.length - 1] / TICK_HZ; // the footage ends here: the camera stops looking
const boxAt = loadBoxes(RUN_DIR) as (id: number, tick: number) => number[] | null;
console.log(`tracks: ${ticks.length} ticks, ${[...byTick.values()].reduce((s, v) => s + v.length, 0)} rows, ticks ${ticks[0]}..${ticks[ticks.length - 1]}`);
function tracksAt(t: number): Track[] {
  const tick = Math.round(t * TICK_HZ);
  let lo = 0, hi = ticks.length - 1, best = -1;
  while (lo <= hi) { const m = (lo + hi) >> 1; if (ticks[m] <= tick) { best = m; lo = m + 1; } else hi = m - 1; }
  if (best < 0 || tick - ticks[best] > 60) return [];
  return byTick.get(ticks[best])!;
}

// ---- the drone (what the footage cannot give) ----------------------------------------------------
const CAM_E = meta.camera_m[0], CAM_N = -meta.camera_m[2], CAM_ALT = meta.ground.height_m, CAM_PITCH = -meta.ground.pitch_deg;
// Ground point under the image centre: forward (north) by h / tan(pitch); footprint radius from the fov.
const FP_N = CAM_N + CAM_ALT / Math.tan((meta.ground.pitch_deg * Math.PI) / 180);
const FP_R = CAM_ALT * Math.tan((meta.ground.hfov_deg / 2 * Math.PI) / 180) / Math.sin((meta.ground.pitch_deg * Math.PI) / 180);

// ---- link profiles (HACKATHON_PLAN.md §3.3: rate, one-way delay, loss) -----------------------------
type Profile = 'clean' | 'hf' | 'lora' | 'telemetry' | 'contested' | 'blackout';
const PROFILES: Record<Profile, { budgetBps: number; loss: number; delayS: number; up: boolean; video: boolean; bursts?: boolean }> = {
  clean: { budgetBps: 0, loss: 0, delayS: 0.02, up: true, video: true },
  hf: { budgetBps: 9600, loss: 0.01, delayS: 0.5, up: true, video: false },
  lora: { budgetBps: 2000, loss: 0.1, delayS: 0.3, up: true, video: false },
  telemetry: { budgetBps: 600, loss: 0.05, delayS: 0.05, up: true, video: false },
  contested: { budgetBps: 2000, loss: 0.1, delayS: 0.3, up: true, video: false, bursts: true },
  blackout: { budgetBps: 2000, loss: 1, delayS: 0.3, up: false, video: false },
};
const FOCUS_BITS: Record<string, number> = { track: 1, split: 2, chip: 4, release: 8 };
const NAV = ['manual', 'auto', 'loiter', 'rth', 'landing', 'failsafe', 'lostlink', 'other'];
const GNSS = ['none', 'degraded', 'fix', 'rtk'];
const LINK = ['hears', 'silent', 'never', 'never'];

function unpack(buf: Uint8Array): Uint8Array[] {
  const out: Uint8Array[] = []; let i = 0;
  while (i + 2 <= buf.length) { const n = buf[i] | (buf[i + 1] << 8); i += 2; out.push(buf.subarray(i, i + n)); i += n; }
  return out;
}
const r1 = (x: number) => Math.round(x * 10) / 10;
const mixObj = (m: number[]) => ({ dismount: m[0], vehicle: m[1], armour: m[2], other: m[3] });

type Pending = { at: number; bytes: Uint8Array; up: boolean };
type WireFrame = { seq: number; bytes: number; delivered: boolean; lines: string[]; up?: boolean };

class Replay {
  t = 0; playing = true; rate = 1; profile: Profile = (process.env.PROFILE as Profile) || 'clean';
  edge: any; rx: any;
  pending: Pending[] = [];
  delivered: { t: number; bytes: number; wire: number }[] = [];
  dropped = 0; bytesTotal = 0;
  lastDigest = -1e9;
  // Operator focus, as scripts/focus.mjs simulates it: re-sent every second until a Contact comes
  // back flagged focused, then with every digest; a drill (pick one child, release its group) goes
  // up as one frame, three times.
  focus = new Map<number, { mode: string; since: number; acked: boolean; lastSend: number; drill?: { group: number; tries: number } }>();
  burstUntil = -1; nextBurst = -1;
  log: string[] = [];

  constructor() { this.reset(0); }

  reset(t: number) {
    this.t = t;
    const nonce = (Math.random() * 2 ** 31) >>> 0;
    const P = PROFILES[this.profile];
    this.edge = new WasmEdge(JSON.stringify({
      device_id: 1, nonce, origin_lat_e7: Math.round(ORIGIN_LAT * 1e7), origin_lon_e7: Math.round(ORIGIN_LON * 1e7), origin_alt: 32767, pos_res: 2,
      caps: 1 | 2 | 4 | 8 | 0x400 | 0x800, hfov_x10: Math.round(meta.ground.hfov_deg * 10), img_w: meta.width, img_h: meta.height,
      video_frame0: meta.start_frame, fps_x100: Math.round(meta.fps * 100), budget_bps: P.budgetBps, max_frame: 1200,
      sigma_own: 3.0, sigma_att_deg: 1.0, sigma_h: 2.0, sigma_px: 2.0, f_px: meta.ground.f_px,
    }));
    this.rx = new WasmReceiver(P.budgetBps);
    this.pending = []; this.delivered = []; this.dropped = 0; this.bytesTotal = 0; this.lastDigest = -1e9; this.focus.clear();
    this.burstUntil = -1; this.nextBurst = -1;
  }

  linkUp(t: number): boolean {
    const P = PROFILES[this.profile];
    if (!P.up) return false;
    if (P.bursts) {
      if (this.nextBurst < 0) this.nextBurst = t + 3 + Math.random() * 5;
      if (t >= this.nextBurst && t > this.burstUntil) { this.burstUntil = t + 1 + Math.random() * 4; this.nextBurst = this.burstUntil + 3 + Math.random() * 5; }
      if (t <= this.burstUntil) return false;
    }
    return true;
  }

  ego(t: number, P: (typeof PROFILES)[Profile]) {
    return { e: CAM_E, n: CAM_N, alt_agl: CAM_ALT, heading_deg: 0, speed: 0, climb: 0, nav_mode: 2, gnss: 2, battery: Math.max(0, Math.round(83 - t / 30)),
      pos_ce: 3.0, fp_e: CAM_E, fp_n: FP_N, fp_radius: FP_R, video: P.video && this.linkUp(t), looking: t <= LAST_S + 0.2 };
  }

  step(): any {
    if (this.playing) { this.t += STEP_S * this.rate; if (this.t >= DURATION_S) this.reset(0); }
    const t = this.t, tick = Math.round(t * TICK_HZ), P = PROFILES[this.profile];
    const up = this.linkUp(t);
    const tracks = tracksAt(t);
    const ego = this.ego(t, P);
    const frames: WireFrame[] = [];
    if (this.playing) {
      this.edge.pose(tick, CAM_E, CAM_N, CAM_ALT, 0, CAM_PITCH, 0);
      const tj = JSON.stringify(tracks.map((x) => ({ id: x.id, class: x.cls, e: x.e, n: x.n, ve: x.ve, vn: x.vn, conf: Math.max(0, Math.min(255, Math.round(x.conf))), bbox: boxAt(x.id, tick) ?? undefined })));
      const out: Uint8Array = this.edge.tick(tj, JSON.stringify(ego), tick);
      for (const b of unpack(out)) {
        const bytes = new Uint8Array(b);
        const delivered = up && Math.random() >= P.loss;
        const lines = String(describe_frame(bytes)).split('\n');
        const seq = Number(/seq=(\d+)/.exec(lines[0])?.[1] ?? -1);
        frames.push({ seq, bytes: bytes.length, delivered, lines });
        if (delivered) this.pending.push({ at: t + P.delayS, bytes, up: false }); else this.dropped++;
      }
      // Uplink: a digest every 5 s while frames arrive, focus renewals with it.
      if (up && t - this.lastDigest >= 5 && this.rx.needs_digest()) {
        this.lastDigest = t;
        this.sendUp(this.rx.make_digest(P.budgetBps, tick), t, P, frames);
        for (const [id, f] of this.focus) if (f.acked && !f.drill) { f.lastSend = t; this.sendUp(this.rx.make_focus(id, FOCUS_BITS[f.mode] ?? 1, 60, 1, tick), t, P, frames); }
      }
      for (const [id, f] of this.focus) {
        if (t - f.lastSend < 1) continue;
        if (f.drill) {
          f.lastSend = t; this.sendUp(this.rx.make_drill(id, f.drill.group, 60, tick), t, P, frames);
          if (++f.drill.tries >= 3) f.drill = undefined;
        } else if (!f.acked) { f.lastSend = t; this.sendUp(this.rx.make_focus(id, FOCUS_BITS[f.mode] ?? 1, 60, 1, tick), t, P, frames); }
      }
    }
    // Deliver.
    this.pending.sort((a, b) => a.at - b.at);
    while (this.pending.length && this.pending[0].at <= t) {
      const p = this.pending.shift()!;
      try {
        if (p.up) this.edge.on_uplink(p.bytes, tick);
        else { this.rx.on_frame(p.bytes); this.bytesTotal += p.bytes.length; this.delivered.push({ t, bytes: p.bytes.length, wire: p.bytes.length + UDP_IP_OVERHEAD }); }
      } catch (e) { console.warn('decode', e); }
    }
    this.delivered = this.delivered.filter((d) => t - d.t <= 10);
    if (tick % (10 * TICK_HZ) === 0) this.rx.gc(tick);

    // Views.
    const es = JSON.parse(this.edge.snapshot_json(tick));
    const edgeContacts = es.contacts.filter((c: any) => !c.departed).map((c: any) => ({
      id: c.id, rev: c.rev, e: c.e, n: c.n, ce: r1(c.ce), radius: r1(c.radius), count: c.count, mix: mixObj(c.mix), motion: c.motion, confirmed: c.confirmed,
      lost: c.lost, departed: c.departed, focused: c.focused, split: c.split, course: Math.round(c.course), speed: r1(c.speed), members: c.members,
      firstSeen: r1(c.first_seen), since: r1(c.since), parent: c.parent, rev_dirty: c.dirty, step: c.step, dueIn: r1(c.due_in), sends: c.sends }));
    const edgeTracks = es.tracks.filter((x: any) => !x.lost).map((x: any) => ({ id: x.id, cls: x.class, e: x.e, n: x.n, ve: x.ve, vn: x.vn, conf: x.conf, contact: x.contact }));
    const rxContacts = JSON.parse(this.rx.snapshot_json(tick)).map((c: any) => ({
      id: c.id, rev: c.rev, e: c.e, n: c.n, ce: r1(c.ce), ceShown: r1(c.ce_shown), radius: r1(c.radius), count: c.count, mix: mixObj(c.mix), motion: c.motion,
      confirmed: c.confirmed, lost: c.lost, departed: c.departed, focused: c.focused, group: c.group, course: Math.round(c.course), speed: r1(c.speed), members: [],
      firstSeen: c.first_seen, since: c.since, ageS: r1(c.silence_s), liveness: c.liveness, parent: c.parent, child: c.child, ray: c.ray, copies: c.copies,
      lat: c.lat, lon: c.lon, located: c.located, seenE: c.seen_e, seenN: c.seen_n, pMiss: r1(c.p_miss), unassuredS: r1(c.unassured_s) }));
    for (const [id, f] of this.focus) if (!f.acked && rxContacts.some((c: any) => c.focused && !c.departed && (c.id === id || c.parent === id))) f.acked = true;
    // A contact picked as one object that has since become a group (a neighbour joined before the
    // Focus arrived) is split, so the picked object shows up as an individual to click again.
    for (const [id, f] of this.focus) if (f.mode === 'track' && !f.drill && rxContacts.some((c: any) => c.id === id && !c.child && !c.departed && c.count > 1)) { f.mode = 'split'; f.acked = false; f.lastSend = -1e9; }
    const rawEgo = JSON.parse(this.rx.ego_json());
    const rxEgo = rawEgo ? {
      e: rawEgo.rec.dx, n: rawEgo.rec.dy, altAgl: rawEgo.rec.alt_agl, heading: Math.round(rawEgo.rec.heading * 360 / 256), speed: rawEgo.rec.speed / 4,
      nav: NAV[rawEgo.rec.nav & 7], gnss: GNSS[(rawEgo.rec.nav >> 3) & 3], link: LINK[(rawEgo.rec.nav >> 5) & 3], video: !!(rawEgo.rec.nav & 0x80), battery: rawEgo.rec.battery,
      fpE: rawEgo.rec.fp_dx, fpN: rawEgo.rec.fp_dy, fpRadius: m8(rawEgo.rec.fp_radius), nContacts: rawEgo.rec.n_contacts, nMoving: rawEgo.rec.n_moving,
      ageS: r1((tick - rawEgo.tick) / TICK_HZ) } : null;
    const events = JSON.parse(this.rx.events_json()).map((e: any) => ({
      t: r1(e.tick / TICK_HZ), kind: e.kind, id: e.id, text: e.text + (Math.abs(e.heard - e.tick) > 2 * TICK_HZ ? ` (heard at ${r1(e.heard / TICK_HZ)} s)` : '') }));
    const bytesPerS = this.delivered.reduce((s, d) => s + d.bytes, 0) / 10;
    const wirePerS = this.delivered.reduce((s, d) => s + d.wire, 0) / 10;
    const timing = JSON.parse(this.edge.timing_json());
    const of = this.rx.of();
    return {
      t: Math.round(t * 1000) / 1000, clipT: Math.round(t * 1000) / 1000, playing: this.playing, rate: this.rate, duration: DURATION_S,
      edge: { tracks: edgeTracks, contacts: edgeContacts,
        ego: { e: CAM_E, n: CAM_N, altAgl: Math.round(CAM_ALT), heading: 0, speed: 0, nav: 'loiter', gnss: 'fix', link: up ? 'hears' : 'silent', battery: ego.battery, fpE: CAM_E, fpN: FP_N, fpRadius: FP_R, video: ego.video } },
      wire: { frames, budgetBps: P.budgetBps, profile: this.profile, up, bytesPerS: r1(bytesPerS), wireBytesPerS: r1(wirePerS), dropped: this.dropped, regime: timing.regime, f: timing.f,
        floorS: r1(timing.floor / TICK_HZ), stats: JSON.parse(this.edge.stats_json()) },
      rx: { contacts: rxContacts, ego: rxEgo, events, known: this.rx.known(), of: of < 0 ? null : of, bytesTotal: this.bytesTotal, unheard: this.rx.device_unheard(tick), stats: JSON.parse(this.rx.stats_json()) },
    };
  }

  sendUp(bytes: Uint8Array, t: number, P: (typeof PROFILES)[Profile], frames: WireFrame[]) {
    const delivered = this.linkUp(t) && Math.random() >= P.loss;
    const lines = String(describe_frame(bytes)).split('\n');
    frames.push({ seq: Number(/seq=(\d+)/.exec(lines[0])?.[1] ?? -1), bytes: bytes.length, delivered, lines, up: true });
    if (delivered) this.pending.push({ at: t + P.delayS, bytes, up: true });
  }

  command(m: any) {
    switch (m.cmd) {
      case 'pause': this.playing = false; break;
      case 'play': this.playing = true; break;
      case 'rate': this.rate = Math.max(0.25, Math.min(8, Number(m.x) || 1)); break;
      case 'seek': { const t = Math.max(0, Math.min(DURATION_S - 0.1, Number(m.t) || 0)); this.reset(t); break; }
      case 'link': {
        if (!(m.profile in PROFILES)) break;
        this.profile = m.profile;
        const P = PROFILES[this.profile];
        // The radio knows its own rate: both ends learn the budget at once (a Digest carries it too).
        this.edge.set_budget(P.budgetBps); this.rx.set_budget(P.budgetBps);
        break;
      }
      case 'focus': {
        const id = Number(m.id);
        let mode = String(m.mode || 'track');
        const tick = Math.round(this.t * TICK_HZ), P = PROFILES[this.profile];
        const held = (JSON.parse(this.rx.snapshot_json(tick)) as any[]).find((c) => c.id === id);
        // A click ('auto'): one of a split group's individuals -> drill to it; a group -> split it
        // so its members come back as individuals; anything else -> track.
        if (mode === 'auto') {
          if (held?.child && held.parent != null && this.focus.has(held.parent)) {
            this.focus.delete(held.parent);
            this.focus.set(id, { mode: 'track', since: this.t, acked: false, lastSend: -1e9, drill: { group: held.parent, tries: 0 } });
            break; // sent by the next step
          }
          mode = held && held.count > 1 ? 'split' : 'track';
        }
        if (mode === 'release') this.focus.delete(id); else this.focus.set(id, { mode, since: this.t, acked: false, lastSend: this.t });
        const bits = mode === 'split' ? FOCUS_BITS.track | FOCUS_BITS.split : FOCUS_BITS[mode] ?? 1;
        const bytes = this.rx.make_focus(id, bits, 60, 1, tick);
        if (this.linkUp(this.t) && Math.random() >= P.loss) this.pending.push({ at: this.t + P.delayS, bytes, up: true });
        break;
      }
    }
  }
}
function m8(q: number) { const e = q >> 5, m = q & 31; return e === 0 ? m / 4 : ((32 + m) / 4) * 2 ** (e - 1); }

// ---- http + ws ------------------------------------------------------------------------------------
const MIME: Record<string, string> = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.json': 'application/json', '.svg': 'image/svg+xml' };
const replay = new Replay();

function serveVideo(req: http.IncomingMessage, res: http.ServerResponse) {
  if (!fs.existsSync(VIDEO)) { res.writeHead(404, { 'content-type': 'text/plain' }); res.end(`missing ${VIDEO}: see README (ffmpeg transcode)`); return; }
  const size = fs.statSync(VIDEO).size;
  const range = req.headers.range;
  const head: Record<string, string | number> = { 'content-type': 'video/mp4', 'accept-ranges': 'bytes', 'cache-control': 'no-cache' };
  if (range) {
    const m = /bytes=(\d*)-(\d*)/.exec(range);
    let start = m && m[1] ? Number(m[1]) : 0, end = m && m[2] ? Number(m[2]) : size - 1;
    if (start >= size) { res.writeHead(416, { 'content-range': `bytes */${size}` }); res.end(); return; }
    end = Math.min(end, size - 1);
    res.writeHead(206, { ...head, 'content-range': `bytes ${start}-${end}/${size}`, 'content-length': end - start + 1 });
    if (req.method === 'HEAD') { res.end(); return; }
    fs.createReadStream(VIDEO, { start, end }).pipe(res);
  } else {
    res.writeHead(200, { ...head, 'content-length': size });
    if (req.method === 'HEAD') { res.end(); return; }
    fs.createReadStream(VIDEO).pipe(res);
  }
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url || '/', 'http://x');
  if (url.pathname === '/meta') { res.writeHead(200, { 'content-type': 'application/json', 'cache-control': 'no-cache' }); res.end(JSON.stringify(meta)); return; }
  if (url.pathname === '/video.mp4') return serveVideo(req, res);
  if (url.pathname === '/state') { res.writeHead(200, { 'content-type': 'application/json' }); res.end(JSON.stringify({ t: replay.t, playing: replay.playing, rate: replay.rate, profile: replay.profile, clients: wss.clients.size, driver: 'real' })); return; }
  const rel = url.pathname === '/' ? '/index.html' : url.pathname;
  const file = path.normalize(path.join(WEB, rel));
  if (!file.startsWith(WEB) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) { res.writeHead(404); res.end('not found'); return; }
  res.writeHead(200, { 'content-type': MIME[path.extname(file)] || 'application/octet-stream', 'cache-control': 'no-cache' });
  fs.createReadStream(file).pipe(res);
});
const wss = new WebSocketServer({ server });
wss.on('connection', (ws) => {
  ws.on('message', (data) => { try { replay.command(JSON.parse(String(data))); } catch (e) { console.warn('bad command', String(data)); } });
});
setInterval(() => {
  let msg: string;
  try { msg = JSON.stringify(replay.step()); } catch (e) { console.error('step', e); return; }
  for (const c of wss.clients) if (c.readyState === WebSocket.OPEN) c.send(msg);
}, STEP_S * 1000);
server.listen(PORT, () => console.log(`driver: http://localhost:${PORT}  profile ${replay.profile}  (video ${fs.existsSync(VIDEO) ? 'ok' : 'MISSING'}: ${VIDEO})`));
