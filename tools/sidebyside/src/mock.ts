// Mock feed for the side-by-side page. Speaks the WebSocket contract in README.md on one port with
// the static page, /video.mp4 (Range) and /meta. The real replay driver replaces this file.
//
//   npm run mock                          http://localhost:8090
//   PORT=8091 RUN_DIR=... VIDEO=... npm run mock
//
// What it fakes (see README "What the mock fakes"): contacts = tracks clustered within 20 m with
// stable ids; the wire = one frame per link period carrying the ego and the contacts whose rev
// changed, trimmed to the budget, dropped at the profile's loss rate; rx = those frames applied
// after the profile's delay; events derived from rx state changes; ceShown = ce + 1 m/s * age.
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { WebSocketServer, WebSocket } from 'ws';
// @ts-ignore plain ESM helper shared with the check scripts
import { loadMeta } from '../scripts/meta.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, '../../..');
const PORT = Number(process.env.PORT || 8090);
const RUN_DIR = process.env.RUN_DIR || path.join(repo, 'runs/footage/meva-2018-03-13.16-00-14-bf');
const VIDEO = process.env.VIDEO || path.join(repo, 'runs/sidebyside/meva-720p.mp4');
const WEB = path.join(here, '../web');
const TICK_HZ = 120, STEP_S = 0.1, DURATION_S = 90.0;

// ---- data ----------------------------------------------------------------------------------------
type Track = { id: number; cls: number; e: number; n: number; ve: number; vn: number; conf: number };
const meta = loadMeta(RUN_DIR) as any;
const byTick = new Map<number, Track[]>();
for (const line of fs.readFileSync(path.join(RUN_DIR, 'tracks.csv'), 'utf8').split('\n').slice(1)) {
  if (!line) continue;
  const c = line.split(',');
  const tick = Number(c[0]);
  // ENU: east = x, north = -z, up = y
  const tr: Track = { id: +c[1], cls: +c[2], e: +c[3], n: -c[5], ve: +c[6], vn: -c[8], conf: +c[9] };
  let arr = byTick.get(tick);
  if (!arr) byTick.set(tick, (arr = []));
  arr.push(tr);
}
const ticks = [...byTick.keys()].sort((a, b) => a - b);
console.log(`tracks: ${ticks.length} ticks, ${[...byTick.values()].reduce((s, v) => s + v.length, 0)} rows, ticks ${ticks[0]}..${ticks[ticks.length - 1]}`);

function tracksAt(t: number): Track[] {
  const tick = Math.round(t * TICK_HZ);
  let lo = 0, hi = ticks.length - 1, best = -1;
  while (lo <= hi) { const m = (lo + hi) >> 1; if (ticks[m] <= tick) { best = m; lo = m + 1; } else hi = m - 1; }
  if (best < 0 || tick - ticks[best] > 60) return [];
  return byTick.get(ticks[best])!;
}

// ---- link profiles ---------------------------------------------------------------------------------
type Profile = 'clean' | 'hf' | 'lora' | 'telemetry' | 'contested' | 'blackout';
const PROFILES: Record<Profile, { budgetBps: number; loss: number; delayS: number; periodS: number; up: boolean }> = {
  clean: { budgetBps: 9600, loss: 0.0, delayS: 0.2, periodS: 0.5, up: true },
  hf: { budgetBps: 2400, loss: 0.02, delayS: 2.0, periodS: 2.0, up: true },
  lora: { budgetBps: 800, loss: 0.05, delayS: 1.0, periodS: 1.0, up: true },
  telemetry: { budgetBps: 57600, loss: 0.01, delayS: 0.1, periodS: 0.2, up: true },
  contested: { budgetBps: 800, loss: 0.3, delayS: 1.5, periodS: 1.0, up: true },
  blackout: { budgetBps: 0, loss: 1.0, delayS: 1.0, periodS: 1.0, up: false },
};

// ---- edge: contacts from tracks ------------------------------------------------------------------
type Mix = { dismount: number; vehicle: number; armour: number; other: number };
type Motion = 'static' | 'moving' | 'stopped' | 'unknown';
type Contact = {
  id: number; rev: number; e: number; n: number; ce: number; radius: number; count: number; mix: Mix;
  motion: Motion; confirmed: boolean; lost: boolean; departed: boolean; focused: boolean;
  course: number; speed: number; members: number[]; firstSeen: number; since: number;
};
type ContactState = Contact & { lostAt: number | null; lastMoving: number; revE: number; revN: number };
const CLUSTER_M = 20, LEAVE_M = 26;
function mixOf(cls: number): keyof Mix {
  if (cls === 0) return 'dismount';
  if (cls === 101) return 'armour';
  if (cls === 100) return 'other';
  return 'vehicle';
}

class Edge {
  contacts = new Map<number, ContactState>();
  nextId = 1;
  focusedId: number | null = null;
  reset() { this.contacts.clear(); this.nextId = 1; }

  step(t: number, tracks: Track[]) {
    const unassigned = new Map(tracks.map((tr) => [tr.id, tr]));
    const groups = new Map<number, Track[]>();
    // keep members that are still near their contact
    for (const c of this.contacts.values()) {
      if (c.lost) continue;
      const g: Track[] = [];
      for (const id of c.members) {
        const tr = unassigned.get(id);
        if (tr && Math.hypot(tr.e - c.e, tr.n - c.n) <= LEAVE_M) { g.push(tr); unassigned.delete(id); }
      }
      groups.set(c.id, g);
    }
    // attach loose tracks to the nearest contact within CLUSTER_M
    for (const tr of [...unassigned.values()]) {
      let best: ContactState | null = null, bd = CLUSTER_M;
      for (const c of this.contacts.values()) {
        if (c.lost) continue;
        const d = Math.hypot(tr.e - c.e, tr.n - c.n);
        if (d < bd) { bd = d; best = c; }
      }
      if (best) { groups.get(best.id)!.push(tr); unassigned.delete(tr.id); }
    }
    // new contacts from what is left
    const loose = [...unassigned.values()];
    while (loose.length) {
      const seed = loose.shift()!;
      const g = [seed];
      for (let i = loose.length - 1; i >= 0; i--) {
        if (Math.hypot(loose[i].e - seed.e, loose[i].n - seed.n) <= CLUSTER_M) g.push(loose.splice(i, 1)[0]);
      }
      const id = this.nextId++;
      this.contacts.set(id, {
        id, rev: 0, e: seed.e, n: seed.n, ce: 4, radius: 5, count: 0, mix: { dismount: 0, vehicle: 0, armour: 0, other: 0 },
        motion: 'unknown', confirmed: false, lost: false, departed: false, focused: false, course: 0, speed: 0,
        members: [], firstSeen: t, since: t, lostAt: null, lastMoving: -1e9, revE: seed.e, revN: seed.n,
      });
      groups.set(id, g);
    }
    // update
    for (const c of this.contacts.values()) {
      const g = groups.get(c.id) || [];
      if (!g.length) {
        if (!c.lost) {
          c.lost = true; c.lostAt = t; c.departed = t - c.lastMoving < 2.0; c.rev++;
        } else if (t - (c.lostAt ?? t) > 3.0) this.contacts.delete(c.id);
        continue;
      }
      const e = g.reduce((s, x) => s + x.e, 0) / g.length, n = g.reduce((s, x) => s + x.n, 0) / g.length;
      const ve = g.reduce((s, x) => s + x.ve, 0) / g.length, vn = g.reduce((s, x) => s + x.vn, 0) / g.length;
      const radius = Math.max(5, Math.max(...g.map((x) => Math.hypot(x.e - e, x.n - n))) + 2);
      const mix: Mix = { dismount: 0, vehicle: 0, armour: 0, other: 0 };
      for (const x of g) mix[mixOf(x.cls)]++;
      const speed = Math.hypot(ve, vn);
      let motion = c.motion;
      if (t - c.firstSeen < 1.0) motion = 'unknown';
      else if (speed > 0.5) motion = 'moving';
      else if (c.motion === 'moving' && speed < 0.3) motion = 'stopped';
      else if (c.motion === 'stopped' && t - c.since > 10) motion = 'static';
      else if (c.motion === 'unknown') motion = 'static';
      const changed = motion !== c.motion || g.length !== c.count || Math.hypot(e - c.revE, n - c.revN) > 3 || (!c.confirmed && t - c.firstSeen >= 1.0);
      if (motion !== c.motion) c.since = t;
      if (speed > 0.5) c.lastMoving = t;
      if (changed) { c.rev++; c.revE = e; c.revN = n; }
      Object.assign(c, {
        e, n, radius, count: g.length, mix, motion, speed, members: g.map((x) => x.id),
        course: speed > 0.2 ? ((Math.atan2(ve, vn) * 180) / Math.PI + 360) % 360 : c.course,
        confirmed: t - c.firstSeen >= 1.0, ce: Math.round((3 + 0.5 * speed) * 10) / 10, focused: this.focusedId === c.id,
      });
    }
  }
  snapshot(): Contact[] {
    return [...this.contacts.values()].map(({ lostAt, lastMoving, revE, revN, ...c }) => ({ ...c, mix: { ...c.mix }, members: [...c.members] }));
  }
}

// ---- ego ------------------------------------------------------------------------------------------
const CAM_E = meta.camera_m[0], CAM_N = -meta.camera_m[2];
function egoAt(t: number, up: boolean) {
  return {
    e: CAM_E, n: CAM_N, altAgl: Math.round(meta.ground.height_m), heading: 0, speed: 0, nav: 'loiter',
    gnss: 'fix', link: up ? 'hears' : 'silent', battery: Math.round(83 - t / 30),
    fpE: CAM_E, fpN: CAM_N + 9.7, fpRadius: 74,
  };
}

// ---- wire + rx ------------------------------------------------------------------------------------
type Frame = { seq: number; bytes: number; delivered: boolean; lines: string[] };
type Pending = { at: number; frame: Frame; contacts: Contact[]; lostIds: number[]; ego: any };
type RxContact = Contact & { ceShown: number; liveness: string; ageS: number; heardAt: number; edgeGone: number | null };
type Event = { t: number; kind: string; id: number | null; text: string };

const fmt = (x: number, d = 1) => x.toFixed(d);
function contactLine(c: Contact) {
  return `Contact id=${c.id} rev=${c.rev} e=${fmt(c.e)} n=${fmt(c.n)} r=${fmt(c.radius, 0)} n=${c.count} ` +
    `dis=${c.mix.dismount} veh=${c.mix.vehicle} arm=${c.mix.armour} oth=${c.mix.other} ${c.motion}` +
    (c.motion === 'moving' ? ` crs=${String(Math.round(c.course)).padStart(3, '0')} spd=${fmt(c.speed)}` : '') +
    ` ce=${fmt(c.ce)}${c.confirmed ? ' confirmed' : ''}${c.focused ? ' focused' : ''}`;
}
function describe(c: Contact) {
  const parts: string[] = [];
  if (c.mix.dismount) parts.push(`${c.mix.dismount} dismount${c.mix.dismount > 1 ? 's' : ''}`);
  if (c.mix.vehicle) parts.push(`${c.mix.vehicle} vehicle${c.mix.vehicle > 1 ? 's' : ''}`);
  if (c.mix.armour) parts.push(`${c.mix.armour} armour`);
  if (c.mix.other) parts.push(`${c.mix.other} other`);
  return parts.join(', ') || 'empty';
}

class Replay {
  t = 0; playing = true; rate = 1; profile: Profile = 'lora';
  edge = new Edge();
  pending: Pending[] = [];
  rx = new Map<number, RxContact>();
  rxEgo: any = null;
  seq = 0; dropped = 0; bytesTotal = 0;
  sentRev = new Map<number, number>();
  lostSent = new Set<number>();
  delivered: { t: number; bytes: number }[] = [];
  sinceFrame = 1e9; lastEgoEvent = -1e9; rr = 0;
  events: Event[] = [];

  reset(t: number) {
    this.t = t; this.edge.reset(); this.pending = []; this.rx.clear(); this.rxEgo = null;
    this.sentRev.clear(); this.lostSent.clear(); this.delivered = []; this.sinceFrame = 1e9; this.lastEgoEvent = -1e9;
    this.events = []; this.bytesTotal = 0;
  }

  step(): any {
    if (this.playing) {
      this.t += STEP_S * this.rate;
      if (this.t >= DURATION_S) this.reset(0);
    }
    const t = this.t, P = PROFILES[this.profile];
    const tracks = tracksAt(t);
    this.edge.step(t, tracks);
    const contacts = this.edge.snapshot();
    const ego = egoAt(t, P.up);
    const frames: Frame[] = [];
    this.sinceFrame += this.playing ? STEP_S * this.rate : 0;
    if (this.sinceFrame >= P.periodS) {
      this.sinceFrame = 0;
      frames.push(this.buildFrame(t, contacts, ego, P));
    }
    // deliver pending frames
    const events: Event[] = [];
    while (this.pending.length && this.pending[0].at <= t) {
      const p = this.pending.shift()!;
      if (!p.frame.delivered) continue;
      this.bytesTotal += p.frame.bytes;
      this.delivered.push({ t, bytes: p.frame.bytes });
      this.apply(t, p, events);
    }
    this.delivered = this.delivered.filter((d) => t - d.t <= 10);
    // age rx
    const live = new Set(contacts.map((c) => c.id));
    for (const r of this.rx.values()) {
      r.ageS = Math.max(0, t - r.heardAt);
      r.ceShown = Math.round((r.ce + 1.0 * r.ageS) * 10) / 10;
      if (!live.has(r.id)) { if (r.edgeGone === null) r.edgeGone = t; } else r.edgeGone = null;
      r.liveness = r.departed ? 'departed' : r.lost ? 'lost' : r.ageS > 3 ? 'unheard' : 'fresh';
      if ((r.lost || r.departed) && t - r.heardAt > 10) this.rx.delete(r.id);
      else if (r.edgeGone !== null && t - r.edgeGone > 20) this.rx.delete(r.id);
    }
    if (t - this.lastEgoEvent >= 10 && this.rxEgo) {
      this.lastEgoEvent = t;
      events.push({ t: Math.round(t * 10) / 10, kind: 'ego', id: null, text: `ego: ${this.rxEgo.nav} at ${this.rxEgo.altAgl} m AGL, heading ${String(this.rxEgo.heading).padStart(3, '0')}, battery ${this.rxEgo.battery} %` });
    }
    events.push(...this.events.splice(0));
    const bytesPerS = this.delivered.reduce((s, d) => s + d.bytes, 0) / 10;
    const rxContacts = [...this.rx.values()].map(({ heardAt, edgeGone, ...r }) => r);
    return {
      t: Math.round(t * 1000) / 1000, clipT: Math.round(t * 1000) / 1000,
      playing: this.playing, rate: this.rate,
      edge: { tracks, contacts, ego },
      wire: { frames, budgetBps: P.budgetBps, profile: this.profile, up: P.up, bytesPerS: Math.round(bytesPerS * 10) / 10, dropped: this.dropped },
      rx: {
        contacts: rxContacts, ego: this.rxEgo ? { ...this.rxEgo, ageS: Math.round((t - this.rxEgo._at) * 10) / 10, _at: undefined } : null,
        events, known: rxContacts.filter((r) => r.liveness === 'fresh' || r.liveness === 'unheard').length,
        of: contacts.filter((c) => c.confirmed && !c.lost).length, bytesTotal: this.bytesTotal,
      },
    };
  }

  buildFrame(t: number, contacts: Contact[], ego: any, P: (typeof PROFILES)[Profile]): Frame {
    const lines = [`Ego e=${fmt(ego.e)} n=${fmt(ego.n)} alt=${ego.altAgl} hdg=${String(ego.heading).padStart(3, '0')} spd=${fmt(ego.speed)} nav=${ego.nav} gnss=${ego.gnss} link=${ego.link} batt=${ego.battery} fp=${fmt(ego.fpE, 0)},${fmt(ego.fpN, 0)},${ego.fpRadius}`];
    let bytes = 4 + 16;
    const budget = P.budgetBps > 0 ? Math.max(40, (P.budgetBps * P.periodS) / 8) : 60;
    const changed = contacts.filter((c) => c.confirmed && !c.lost && (this.sentRev.get(c.id) ?? -1) < c.rev);
    const lostNow = contacts.filter((c) => c.lost && !this.lostSent.has(c.id) && this.sentRev.has(c.id));
    const fresh = contacts.filter((c) => c.confirmed && !c.lost && !changed.includes(c));
    const included: Contact[] = [], lostIds: number[] = [];
    for (const c of lostNow) {
      if (bytes + 3 > budget) break;
      lines.push(`Contact id=${c.id} rev=${c.rev} ${c.departed ? 'departed' : 'lost'}`); bytes += 3; lostIds.push(c.id); this.lostSent.add(c.id);
    }
    for (const c of changed) {
      if (bytes + 14 > budget) break;
      lines.push(contactLine(c)); bytes += 14; included.push(c); this.sentRev.set(c.id, c.rev);
    }
    if (fresh.length && bytes + 14 <= budget) { // round-robin refresh of one unchanged contact
      const c = fresh[this.rr++ % fresh.length];
      lines.push(contactLine(c)); bytes += 14; included.push(c);
    }
    bytes += Math.floor(Math.random() * 3);
    const delivered = P.up && Math.random() >= P.loss;
    if (!delivered) {
      this.dropped++;
      // undelivered changes are re-sent next frame
      for (const c of included) if (this.sentRev.get(c.id) === c.rev) this.sentRev.set(c.id, c.rev - 1);
      for (const id of lostIds) this.lostSent.delete(id);
    }
    const frame: Frame = { seq: ++this.seq, bytes, delivered, lines };
    this.pending.push({ at: t + P.delayS, frame, contacts: included, lostIds, ego: { ...ego, _at: t } });
    return frame;
  }

  apply(t: number, p: Pending, events: Event[]) {
    const ev = (kind: string, id: number | null, text: string) => events.push({ t: Math.round(t * 10) / 10, kind, id, text });
    this.rxEgo = p.ego;
    for (const c of p.contacts) {
      const prev = this.rx.get(c.id);
      const next: RxContact = { ...c, ceShown: c.ce, liveness: 'fresh', ageS: 0, heardAt: t, edgeGone: null };
      this.rx.set(c.id, next);
      const where = `at e=${fmt(c.e, 0)} n=${fmt(c.n, 0)}`;
      if (!prev) { ev('new', c.id, `contact ${c.id}: new, ${describe(c)} ${where}`); continue; }
      if (!prev.confirmed && c.confirmed) ev('confirmed', c.id, `contact ${c.id}: confirmed, ${describe(c)}`);
      if (c.count > prev.count) ev('grew', c.id, `contact ${c.id}: grew to ${describe(c)}`);
      else if (c.count < prev.count) ev('shrank', c.id, `contact ${c.id}: shrank to ${describe(c)}`);
      if (c.motion !== prev.motion) {
        if (c.motion === 'moving') ev('moving', c.id, `contact ${c.id}: ${describe(c)} started moving, course ${String(Math.round(c.course)).padStart(3, '0')} at ${fmt(c.speed)} m/s`);
        else if (c.motion === 'stopped') ev('stopped', c.id, `contact ${c.id}: ${describe(c)} stopped ${where}`);
        else if (c.motion === 'static') ev('static', c.id, `contact ${c.id}: ${describe(c)} static ${where}`);
      }
    }
    for (const id of p.lostIds) {
      const r = this.rx.get(id);
      if (!r) continue;
      const departed = p.frame.lines.some((l) => l.startsWith(`Contact id=${id} `) && l.endsWith('departed'));
      r.lost = true; r.departed = departed; r.heardAt = t;
      ev(departed ? 'departed' : 'lost', id, `contact ${id}: ${describe(r)} ${departed ? 'departed' : 'lost'}`);
    }
  }

  command(m: any) {
    switch (m.cmd) {
      case 'pause': this.playing = false; break;
      case 'play': this.playing = true; break;
      case 'rate': this.rate = Math.max(0.25, Math.min(8, Number(m.x) || 1)); break;
      case 'seek': { const t = Math.max(0, Math.min(DURATION_S - 0.1, Number(m.t) || 0)); this.reset(t); break; }
      case 'link': if (m.profile in PROFILES) this.profile = m.profile; break;
      case 'focus': {
        const id = Number(m.id), mode = String(m.mode || 'track');
        if (mode === 'release') { if (this.edge.focusedId === id) this.edge.focusedId = null; }
        else this.edge.focusedId = id;
        const c = this.edge.contacts.get(id);
        if (c) { c.rev++; c.focused = this.edge.focusedId === id; }
        this.events.push({ t: Math.round(this.t * 10) / 10, kind: 'focus', id, text: `focus: ${mode} contact ${id}` + (mode === 'split' ? ' (mock: no split performed)' : mode === 'chip' ? ' (mock: no chip available)' : '') });
        break;
      }
    }
  }
}

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
  if (url.pathname === '/state') { res.writeHead(200, { 'content-type': 'application/json' }); res.end(JSON.stringify({ t: replay.t, playing: replay.playing, rate: replay.rate, profile: replay.profile, clients: wss.clients.size })); return; }
  const rel = url.pathname === '/' ? '/index.html' : url.pathname;
  const file = path.normalize(path.join(WEB, rel));
  if (!file.startsWith(WEB) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) { res.writeHead(404); res.end('not found'); return; }
  res.writeHead(200, { 'content-type': MIME[path.extname(file)] || 'application/octet-stream', 'cache-control': 'no-cache' });
  fs.createReadStream(file).pipe(res);
});
const wss = new WebSocketServer({ server });
wss.on('connection', (ws) => {
  ws.on('message', (data) => {
    try { replay.command(JSON.parse(String(data))); } catch (e) { console.warn('bad command', String(data)); }
  });
});
setInterval(() => {
  const msg = JSON.stringify(replay.step());
  for (const c of wss.clients) if (c.readyState === WebSocket.OPEN) c.send(msg);
}, STEP_S * 1000);
server.listen(PORT, () => console.log(`mock: http://localhost:${PORT}  (video ${fs.existsSync(VIDEO) ? 'ok' : 'MISSING'}: ${VIDEO})`));
