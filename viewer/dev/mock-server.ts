// Mock snapshot server for viewer development and the lead's e2e screenshots. Speaks the same
// protocol as server/src/main.ts on one port: WebSocket {type:'snapshot', snap} at 30 Hz and
// {type:'log', lines, shaper} at 2 Hz, plus GET /api/metrics for the twin-error readout. Snapshots
// carry every field of the contract in server/src/types.ts, including the hackathon fields the
// real server does not produce yet (link, packets, baselineA, geo, theta/coasting/ce, cadence,
// airtimeShare).
//
// It is a small deterministic simulation, not canned frames: two edges run core's predictor
// (linearised damping, speed cap) as ghosts and send a Delta only when the twin would be wrong by
// more than theta, a Keyframe on the heartbeat, Pose and Hello refresh on their cadence; a budget
// controller widens theta like core/src/edge.rs; the shaper applies loss, cap and delay; the
// receiver side extrapolates, marks coasting after one keyframe period and stale after three, and
// grows ce at the class max speed while coasting. So deltas really cluster on turns, keyframes
// really mark the heartbeat, and a blackout really leaves the twin dead-reckoning.
//
//   npm run mock                               scripted loop (PHASES below), ws://localhost:8080
//   MOCK_PHASE=blackout@8 npm run mock         start 8 s into the blackout (entities stale)
//   MOCK_PHASE=lora MOCK_HOLD=1 npm run mock   stay in the lora phase
//   MOCK_FREEZE=1                              freeze time after start-up (pixel-stable screenshots)
//   MOCK_GEO=0 | MOCK_MEASURED=1 | MOCK_PORT=8080 | MOCK_SEED=7
//   MOCK_LEGACY=1                              snapshots without the hackathon fields (today's server)
//
// Runtime control (same keys as the env, for e2e): GET /mock?phase=blackout&at=8&hold=1&freeze=0
// &geo=1&measured=1&legacy=0&script=1, or WS {type:'mock', ...} with the same keys.
// A phase jump replays the simulation from the start of the loop, so the result depends only on
// (phase, at, seed), and the first snapshot after it carries the last 20 s of packets so the
// waterfall is full at once. Operator controls (shaper, budget, link) pause the script, as on
// stage; /mock?script=1 resumes it.
import http from 'node:http';
import { WebSocketServer, WebSocket } from 'ws';
import type {
  AirtimeModel, BaselineAEntry, Cadence, ControlMessage, DeviceView, EntityView, GeoPoint, GlobalEntity,
  LinkProfile, PacketEvent, ShaperConfig, Snapshot,
} from '../src/types';

type V3 = [number, number, number];
type Quat = [number, number, number, number];

const env = process.env;
const PORT = Number(env.MOCK_PORT ?? 8080);
const SEED = Number(env.MOCK_SEED ?? 7);
const HZ = 30, STEP_MS = 1000 / HZ, TICK_HZ = 120;
/** Snapshot clock: 09:30 KST on judging day, plus simulated time. */
const EPOCH = Date.UTC(2026, 9, 11, 0, 30);
const UDP_IP = 28, MAX_DATAGRAM = 1200, RATE_WINDOW_MS = 2000, BACKLOG_MS = 20_000;

// ---- link profiles: docs/HACKATHON_PLAN.md section 3.3 ---------------------------------------
const LONGFAST: AirtimeModel = { kind: 'lora', sf: 11, bwHz: 250_000, cr: 5, preamble: 16, crc: true, explicitHeader: true, lowDataRateOptimize: false, overheadBytes: 16 };
const PROFILES: LinkProfile[] = [
  { name: 'clean', bps: 0, delayMs: 0, loss: 0, queue: 0, budgetBps: 0, airtime: { kind: 'none' }, label: 'Wi-Fi reference' },
  { name: 'degraded', bps: 64_000, delayMs: 20, loss: 0.02, queue: 20, budgetBps: 0, airtime: { kind: 'none' }, label: 'Busy mesh' },
  { name: 'hf', bps: 9600, delayMs: 500, loss: 0.01, queue: 8, budgetBps: 8000, airtime: { kind: 'serial', rateBps: 9600, bitsPerByte: 10, overheadBytes: 8 }, label: 'NATO HF ceiling' },
  { name: 'lora', bps: 2000, delayMs: 300, loss: 0.1, queue: 4, budgetBps: 1500, airtime: LONGFAST, label: 'Meshtastic-class LoRa' },
  { name: 'telemetry', bps: 600, delayMs: 50, loss: 0.05, queue: 4, budgetBps: 450, airtime: { kind: 'serial', rateBps: 600, bitsPerByte: 10, overheadBytes: 6 }, label: 'ELRS-class control-link telemetry' },
  { name: 'contested', bps: 2000, delayMs: 300, loss: 0.1, queue: 4, budgetBps: 1500, airtime: LONGFAST, label: 'Intermittent jamming' },
  { name: 'blackout', bps: 0, delayMs: 0, loss: 1, queue: 0, budgetBps: 0, airtime: { kind: 'none' }, label: 'Link cut' },
];
const profileOf = (name: string) => PROFILES.find(p => p.name === name);

// ---- the script ---------------------------------------------------------------------------------
// The blackout runs on the clean profile so the heartbeat is 2 s: coasting at 2.5 s, stale at 6 s,
// both inside the 10 s, then recovery within one keyframe.
interface Phase { name: string; s: number; profile: string; blackoutS?: number }
const PHASES: Phase[] = [
  { name: 'clean', s: 12, profile: 'clean' },
  { name: 'blackout', s: 10, profile: 'clean', blackoutS: 10 },
  { name: 'recovery', s: 8, profile: 'clean' },
  { name: 'hf', s: 12, profile: 'hf' },
  { name: 'lora', s: 16, profile: 'lora' },
  { name: 'telemetry', s: 14, profile: 'telemetry' },
];
const phaseStartMs = (i: number) => PHASES.slice(0, i).reduce((a, p) => a + p.s * 1000, 0);

// ---- H.264 Baseline A ---------------------------------------------------------------------------
const BASELINE_CONFIGURED: BaselineAEntry[] = [
  { id: '720p', label: 'H.264 720p', bps: 1_500_000, measured: false, source: 'configured' },
  { id: '480p', label: 'H.264 480p', bps: 500_000, measured: false, source: 'configured' },
  { id: '360p', label: 'H.264 360p', bps: 250_000, measured: false, source: 'configured' },
];
const BASELINE_MEASURED: BaselineAEntry[] = [
  { id: '720p', label: 'H.264 720p', bps: 1_180_000, measured: true, source: 'runs/baseline_a.json (mock: iPhone VideoToolbox)' },
  { id: '480p', label: 'H.264 480p', bps: 610_000, measured: true, source: 'runs/baseline_a.json (mock: iPhone VideoToolbox)' },
  { id: '360p', label: 'H.264 360p', bps: 340_000, measured: true, source: 'runs/baseline_a.json (mock: iPhone VideoToolbox)' },
];

// ---- cadence from budget (stand-in for core's cadence_json, S19) ---------------------------------
// 2 s at 8 kbit/s and above (or unlimited), ~15 s at 600 bit/s, log-linear between.
function cadence(budgetBps: number): Cadence {
  const kf = budgetBps <= 0 || budgetBps >= 8000 ? 2000 : Math.min(15_000, Math.round(2000 * Math.pow(8000 / budgetBps, 0.78) / 100) * 100);
  return {
    keyframeMs: kf, helloRefreshMs: Math.max(5000, 2.5 * kf), poseMs: budgetBps > 0 && budgetBps < 4000 ? 10_000 : 500,
    coastMs: kf + 500, staleMs: Math.max(6000, 3 * kf), dropMs: Math.max(15_000, 6 * kf),
  };
}

// ---- class priors and the predictor (core/src/classes.rs, core/src/predictor.rs) -----------------
interface Prior { damping: number; maxSpeed: number; ground: boolean }
const prior = (cls: number): Prior =>
  cls === 0 ? { damping: 0.85, maxSpeed: 3, ground: true }
    : [56, 62, 63].includes(cls) ? { damping: 0.1, maxSpeed: 1, ground: false }
      : { damping: 0.6, maxSpeed: 3, ground: false };

interface State { id: number; cls: number; pos: V3; vel: V3; conf: number; t: number; theta: number }
function predict(s: State, at: number): State {
  if (at <= s.t) return s;
  const p = prior(s.cls), dt = (at - s.t) / 1000;
  const k = Math.max(0, 1 - (1 - p.damping) * dt);
  let vel = s.vel.map(v => v * k) as V3;
  const sp = Math.hypot(...vel);
  if (sp > p.maxSpeed) vel = vel.map(v => v * p.maxSpeed / sp) as V3;
  const pos = s.pos.map((x, i) => x + (s.vel[i] + vel[i]) * 0.5 * dt) as V3;
  if (p.ground && pos[1] < 0) { pos[1] = 0; if (vel[1] < 0) vel[1] = 0; }
  return { ...s, pos, vel, t: at };
}
const dist = (a: V3, b: V3) => Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]);

// ---- the world the edges see ---------------------------------------------------------------------
interface Truth { id: number; cls: number; pos: V3; vel: V3; conf: number }
/** Constant speed along a closed polyline: straight legs (silent) and sharp corners (deltas). */
function along(path: [number, number][], speed: number, t: number): { pos: V3; vel: V3 } {
  const legs = path.map((a, i) => { const b = path[(i + 1) % path.length]; return { a, b, len: Math.hypot(b[0] - a[0], b[1] - a[1]) }; });
  let s = (speed * t) % legs.reduce((x, l) => x + l.len, 0);
  for (const l of legs) {
    if (s <= l.len) {
      const ux = (l.b[0] - l.a[0]) / l.len, uz = (l.b[1] - l.a[1]) / l.len;
      return { pos: [l.a[0] + ux * s, 0, l.a[1] + uz * s], vel: [ux * speed, 0, uz * speed] };
    }
    s -= l.len;
  }
  return { pos: [path[0][0], 0, path[0][1]], vel: [0, 0, 0] };
}
const LOOP: [number, number][] = [[-4.5, 2.5], [3, 2.5], [3, -3], [-1.5, -4]];
function truth(t: number): Truth[] {
  const w2 = along(LOOP, 1.1, t + 3);
  return [
    { id: 1, cls: 0, pos: [3 * Math.cos(t * 0.4), 0, 3 * Math.sin(t * 0.4)], vel: [-1.2 * Math.sin(t * 0.4), 0, 1.2 * Math.cos(t * 0.4)], conf: 230 },
    { id: 2, cls: 0, pos: w2.pos, vel: w2.vel, conf: 205 },
    { id: 3, cls: 56, pos: [-2.2, 0, -1.8], vel: [0, 0, 0], conf: 180 },
    { id: 4, cls: 24, pos: [1.6, 0, 0.9], vel: [0, 0, 0], conf: 150 },
  ];
}

/** Rotation whose -Z axis looks from `from` to `to` (frustums look down -Z). */
function lookAt(from: V3, to: V3): Quat {
  const f = norm([to[0] - from[0], to[1] - from[1], to[2] - from[2]]), z: V3 = [-f[0], -f[1], -f[2]];
  const x = norm(cross([0, 1, 0], z)), y = cross(z, x);
  const [m00, m01, m02, m10, m11, m12, m20, m21, m22] = [x[0], y[0], z[0], x[1], y[1], z[1], x[2], y[2], z[2]];
  const tr = m00 + m11 + m22;
  if (tr > 0) { const s = 0.5 / Math.sqrt(tr + 1); return [(m21 - m12) * s, (m02 - m20) * s, (m10 - m01) * s, 0.25 / s]; }
  if (m00 > m11 && m00 > m22) { const s = 2 * Math.sqrt(1 + m00 - m11 - m22); return [0.25 * s, (m01 + m10) / s, (m02 + m20) / s, (m21 - m12) / s]; }
  if (m11 > m22) { const s = 2 * Math.sqrt(1 + m11 - m00 - m22); return [(m01 + m10) / s, 0.25 * s, (m12 + m21) / s, (m02 - m20) / s]; }
  const s = 2 * Math.sqrt(1 + m22 - m00 - m11); return [(m02 + m20) / s, (m12 + m21) / s, 0.25 * s, (m10 - m01) / s];
}
function norm(v: V3): V3 { const l = Math.hypot(...v) || 1; return [v[0] / l, v[1] / l, v[2] / l]; }
function cross(a: V3, b: V3): V3 { return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]; }

interface DevSpec { id: number; addr: string; sees: Record<number, number>; pose: { pos: V3; quat: Quat } }
const DEVS: DevSpec[] = [
  // The phone on a pole at the left, a drone-like viewpoint behind: both frustums stay clear of the viewer's default camera.
  { id: 101, addr: '10.42.0.23:51234', sees: { 1: 1, 2: 2, 3: 3, 4: 4 }, pose: { pos: [-5.6, 1.6, 4.4], quat: lookAt([-5.6, 1.6, 4.4], [0, 0.6, 0]) } },
  { id: 102, addr: '10.42.0.31:40112', sees: { 1: 7, 2: 8 }, pose: { pos: [4.2, 3.4, -5.8], quat: lookAt([4.2, 3.4, -5.8], [0, 0, 0]) } },
];

// ---- geodesy (S3): WGS84 -> UTM -> MGRS, enough for a grid reference on screen ---------------------
const ANCHOR = { lat: 37.5512, lon: 126.9882, headingDeg: 32 };
const A = 6378137, F = 1 / 298.257223563, E2 = F * (2 - F), EP2 = E2 / (1 - E2), K0 = 0.9996;
function mgrs(latDeg: number, lonDeg: number): string {
  const zone = Math.floor((lonDeg + 180) / 6) + 1;
  const lat = latDeg * Math.PI / 180, lon0 = ((zone - 1) * 6 - 180 + 3) * Math.PI / 180;
  const N = A / Math.sqrt(1 - E2 * Math.sin(lat) ** 2), T = Math.tan(lat) ** 2, C = EP2 * Math.cos(lat) ** 2;
  const Aa = Math.cos(lat) * (lonDeg * Math.PI / 180 - lon0), e4 = E2 * E2, e6 = e4 * E2;
  const M = A * ((1 - E2 / 4 - 3 * e4 / 64 - 5 * e6 / 256) * lat - (3 * E2 / 8 + 3 * e4 / 32 + 45 * e6 / 1024) * Math.sin(2 * lat)
    + (15 * e4 / 256 + 45 * e6 / 1024) * Math.sin(4 * lat) - (35 * e6 / 3072) * Math.sin(6 * lat));
  const east = K0 * N * (Aa + (1 - T + C) * Aa ** 3 / 6 + (5 - 18 * T + T * T + 72 * C - 58 * EP2) * Aa ** 5 / 120) + 500_000;
  let north = K0 * (M + N * Math.tan(lat) * (Aa * Aa / 2 + (5 - T + 9 * C + 4 * C * C) * Aa ** 4 / 24 + (61 - 58 * T + T * T + 600 * C - 330 * EP2) * Aa ** 6 / 720));
  if (latDeg < 0) north += 10_000_000;
  const band = 'CDEFGHJKLMNPQRSTUVWXX'[Math.floor((latDeg + 80) / 8)];
  const set = zone % 6 || 6;
  const col = ['ABCDEFGH', 'JKLMNPQR', 'STUVWXYZ'][(set - 1) % 3][Math.floor(east / 100_000) - 1];
  const row = 'ABCDEFGHJKLMNPQRSTUV'[(Math.floor(north / 100_000) + (set % 2 ? 0 : 5)) % 20];
  const d5 = (v: number) => String(Math.floor(v % 100_000)).padStart(5, '0');
  return `${zone}${band} ${col}${row} ${d5(east)} ${d5(north)}`;
}
/** Marker frame (x right, -z ahead at `headingDeg` from true north, metres) to WGS84 + MGRS. */
function geoOf(pos: V3): GeoPoint {
  const h = ANCHOR.headingDeg * Math.PI / 180, x = pos[0], ahead = -pos[2];
  const east = x * Math.cos(h) + ahead * Math.sin(h), north = -x * Math.sin(h) + ahead * Math.cos(h);
  const phi = ANCHOR.lat * Math.PI / 180, s2 = Math.sin(phi) ** 2;
  const Rm = A * (1 - E2) / (1 - E2 * s2) ** 1.5, Rn = A / Math.sqrt(1 - E2 * s2);
  const lat = ANCHOR.lat + north / Rm * 180 / Math.PI, lon = ANCHOR.lon + east / (Rn * Math.cos(phi)) * 180 / Math.PI;
  return { lat: +lat.toFixed(7), lon: +lon.toFixed(7), mgrs: mgrs(lat, lon) };
}

// ---- time on air (S2) ------------------------------------------------------------------------------
/** Seconds of channel time one datagram of `wire` bytes (incl. the 28 B UDP/IP header) costs. */
function airtimeS(m: AirtimeModel, wire: number): number {
  const payload = Math.max(0, wire - UDP_IP) + (m.kind === 'none' ? 0 : m.overheadBytes);
  if (m.kind === 'serial') return payload * m.bitsPerByte / m.rateBps;
  if (m.kind === 'lora') { // Semtech AN1200.13; cr is the 4/x denominator (5 = 4/5)
    const tsym = 2 ** m.sf / m.bwHz, de = m.lowDataRateOptimize ? 1 : 0;
    const n = Math.ceil((8 * payload - 4 * m.sf + 28 + 16 * (m.crc ? 1 : 0) - 20 * (m.explicitHeader ? 0 : 1)) / (4 * (m.sf - 2 * de)));
    return (m.preamble + 4.25 + 8 + Math.max(n * m.cr, 0)) * tsym;
  }
  return 0;
}

// ---- helpers ------------------------------------------------------------------------------------
/** mulberry32, as in server/src/shaper.ts. */
function seededRng(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6D2B79F5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
class Window {
  private items: { t: number; v: number }[] = [];
  push(t: number, v: number) { this.items.push({ t, v }); }
  sum(now: number, ms = RATE_WINDOW_MS): { sum: number; n: number } {
    while (this.items.length && now - this.items[0].t >= ms) this.items.shift();
    return { sum: this.items.reduce((a, i) => a + i.v, 0), n: this.items.length };
  }
}
const clock = (ms: number) => new Date(ms).toISOString().slice(11, 23);

// ---- edge: ghosts, threshold, budget controller (core/src/edge.rs) -------------------------------
type Kind = PacketEvent['kind'];
interface Datagram { kind: Kind; wire: number; seq?: number; ids?: number[]; states?: State[]; text: string }
const THETA_POS = 0.15, THETA_VEL = 0.3;

class Edge {
  ghosts = new Map<number, State>();
  seq = 0; lastKf = -Infinity; lastHello = -Infinity; lastPose = -Infinity;
  thetaScale = 1; windowStart = 0; windowBytes = 0; forceKf = false;
  constructor(readonly spec: DevSpec) {}

  step(now: number, tracks: Truth[], budget: number, cad: Cadence): Datagram[] {
    const out: Datagram[] = [];
    const theta = THETA_POS * this.thetaScale, thetaV = THETA_VEL * this.thetaScale;
    const tick = Math.round(now / 1000 * TICK_HZ);
    if (now - this.lastHello >= cad.helloRefreshMs) {
      this.lastHello = now;
      out.push({ kind: 'hello', wire: 14 + UDP_IP, text: `Hello { device_id: ${this.spec.id}, session_nonce: 4242, caps: 0, tick: ${tick} }` });
    }
    if (this.forceKf || now - this.lastKf >= cad.keyframeMs) {
      this.forceKf = false; this.lastKf = now;
      this.ghosts = new Map(tracks.map(t => [t.id, { ...t, t: now, theta }]));
      const states = [...this.ghosts.values()];
      const per = Math.floor((MAX_DATAGRAM - 12) / 32);
      for (let i = 0, of = Math.max(1, Math.ceil(states.length / per)); i < of; i++) {
        const part = states.slice(i * per, (i + 1) * per);
        out.push({ kind: 'keyframe', wire: 12 + 32 * part.length + UDP_IP, seq: ++this.seq, ids: part.map(s => s.id), states: part, text: `Keyframe seq=${this.seq} tick=${tick} part=${i}/${of} entities=${part.length}` });
      }
    } else {
      const ups: State[] = [];
      for (const t of tracks) {
        const g = this.ghosts.get(t.id);
        const p = g ? predict(g, now) : null;
        if (!p || dist(p.pos, t.pos) > theta || dist(p.vel, t.vel) > thetaV) {
          const s = { ...t, t: now, theta }; this.ghosts.set(t.id, s); ups.push(s);
        }
      }
      if (ups.length) out.push({ kind: 'delta', wire: 9 + 31 * ups.length + UDP_IP, seq: ++this.seq, ids: ups.map(s => s.id), states: ups, text: `Delta seq=${this.seq} tick=${tick} updates=${ups.length}` });
    }
    if (now - this.lastPose >= cad.poseMs) {
      this.lastPose = now;
      out.push({ kind: 'pose', wire: 41 + UDP_IP, seq: ++this.seq, text: `Pose { seq: ${this.seq}, tick: ${tick}, origin_locked: true }` });
    }
    // Budget controller, every 0.5 s, counting the header like the link does (HACKATHON_PLAN 3.4.2).
    for (const d of out) this.windowBytes += d.wire;
    if (now - this.windowStart >= 500) {
      const bps = this.windowBytes * 8 / ((now - this.windowStart) / 1000);
      if (budget > 0) {
        if (bps > budget) this.thetaScale *= 1.25; else if (bps < budget * 0.7) this.thetaScale *= 0.9;
        this.thetaScale = Math.min(13, Math.max(0.33, this.thetaScale));
      } else this.thetaScale = Math.max(1, this.thetaScale * 0.9); // unlimited: settle back to the default theta
      this.windowStart = now; this.windowBytes = 0;
    }
    return out;
  }
}

// ---- receiver side: per-device twin state (core/src/receiver.rs + server/src/world.ts) -----------
class Rx {
  ents = new Map<number, State>();
  lastRx = -Infinity; lastSeq = 0; lastAck = -Infinity; wasCoasting = false;
  stats = { datagrams: 0, keyframes: 0, deltas: 0, updates: 0, poses: 0, gapsDetected: 0, nacksSent: 0 };
  delivered = new Window(); offered = new Window(); air = new Window();

  apply(d: Datagram, now: number): { ack: boolean; missing: number } {
    this.stats.datagrams++;
    let missing = 0;
    if (d.seq !== undefined) {
      if (d.seq > this.lastSeq + 1 && this.lastSeq > 0) { missing = d.seq - this.lastSeq - 1; this.stats.gapsDetected++; }
      this.lastSeq = Math.max(this.lastSeq, d.seq);
    }
    if (d.kind === 'keyframe') {
      this.stats.keyframes++;
      const ids = new Set(d.ids);
      if (d.text.includes('part=0/')) for (const id of [...this.ents.keys()]) if (!ids.has(id)) this.ents.delete(id);
      for (const s of d.states ?? []) this.ents.set(s.id, s);
    } else if (d.kind === 'delta') {
      this.stats.deltas++; this.stats.updates += d.states?.length ?? 0;
      for (const s of d.states ?? []) this.ents.set(s.id, s);
    } else if (d.kind === 'pose') this.stats.poses++;
    this.lastRx = now;
    if (missing) this.stats.nacksSent++;
    const ack = missing > 0 || now - this.lastAck >= 100;
    if (ack) this.lastAck = now;
    return { ack, missing };
  }
}

// ---- the world --------------------------------------------------------------------------------
interface Dev { spec: DevSpec; edge: Edge; rx: Rx }
interface Pending { due: number; dev: Dev; d: Datagram }

class Mock {
  sim = 0;
  rng = seededRng(SEED);
  shaper: ShaperConfig = { bps: 0, delayMs: 0, loss: 0, enabled: false, burstSec: 0.5 };
  revert: { prev: ShaperConfig; at: number } | null = null;
  budget = 0; profile = 'clean'; model: AirtimeModel = { kind: 'none' }; fusion = true;
  /** LinkView.rateBps under 'external': the rate of the profile the box emulates. */
  externalRate = 0;
  tokens = Infinity; lastRefill = 0;
  queue: Pending[] = [];
  packets: PacketEvent[] = [];
  log: string[] = [];
  counters = { offered: 0, offeredBytes: 0, passed: 0, passedBytes: 0, delivered: 0, deliveredBytes: 0, dropped: 0, droppedBytes: 0, droppedLoss: 0, droppedCap: 0, inFlight: 0 };
  devs: Dev[] = DEVS.map(spec => ({ spec, edge: new Edge(spec), rx: new Rx() }));
  script = { on: true, hold: false, idx: 0, since: 0 };
  contested = { down: false, next: 0 };
  err: { t: number; d: number }[] = [];
  linkMsgs = new Window(); linkAir = new Window();

  constructor(public geo: boolean, public measured: boolean) { this.enter(0); }

  // ---- control ----
  setShaper(c: Partial<ShaperConfig>) {
    if (this.revert) { this.shaper = this.revert.prev; this.revert = null; }
    this.shaper = { ...this.shaper, ...c };
  }
  setFor(c: Partial<ShaperConfig>, ms: number) {
    const prev = this.revert ? this.revert.prev : { ...this.shaper };
    this.setShaper(c);
    this.revert = { prev, at: this.sim + ms };
  }
  applyProfile(name: string, as?: string) {
    if (name === 'external') {
      this.revert = null; this.shaper = { ...this.shaper, enabled: false };
      const p = profileOf(as ?? '') ?? profileOf('clean')!;
      this.profile = 'external'; this.model = p.airtime; this.budget = p.budgetBps; this.externalRate = p.bps;
      return;
    }
    const p = profileOf(name); if (!p) return;
    this.revert = null;
    this.shaper = { ...this.shaper, enabled: name !== 'clean', bps: p.bps, delayMs: p.delayMs, loss: p.loss };
    this.budget = p.budgetBps; this.profile = p.name; this.model = p.airtime;
    this.contested = { down: false, next: this.sim + 4000 };
  }
  private enter(i: number) {
    const p = PHASES[i];
    this.script.idx = i; this.script.since = this.sim;
    this.applyProfile(p.profile);
    if (p.blackoutS) this.setFor({ enabled: true, loss: 1 }, p.blackoutS * 1000);
  }
  phaseName(): string { return PHASES[this.script.idx].name; }

  // ---- one 30 Hz step ----
  step() {
    this.sim += STEP_MS;
    const now = this.sim;
    if (this.script.on && !this.script.hold && now - this.script.since >= PHASES[this.script.idx].s * 1000) this.enter((this.script.idx + 1) % PHASES.length);
    if (this.revert && now >= this.revert.at) { this.shaper = this.revert.prev; this.revert = null; }
    if (this.profile === 'contested' && !this.revert && now >= this.contested.next) {
      // Intermittent jamming: 4-8 s of lora, then a 1-5 s blackout (HACKATHON_PLAN 3.3).
      this.contested.down = !this.contested.down;
      this.shaper = { ...this.shaper, loss: this.contested.down ? 1 : profileOf('contested')!.loss };
      this.contested.next = now + (this.contested.down ? 1000 + 4000 * this.rng() : 4000 + 4000 * this.rng());
    }
    const cad = cadence(this.budget), world = truth(now / 1000);
    const perDev = this.budget > 0 ? this.budget / this.devs.length : 0; // stand-in: the budget is shared
    for (const dev of this.devs) {
      const tracks = world.filter(t => dev.spec.sees[t.id] !== undefined).map(t => {
        const n = (k: number) => 0.012 * Math.sin(now / 1000 * 7.3 + k * 13.1 + dev.spec.id);
        return { ...t, id: dev.spec.sees[t.id], pos: t.pos.map((v, i) => i === 1 ? v : v + n(t.id + i)) as V3 };
      });
      for (const d of dev.edge.step(now, tracks, perDev, cad)) this.offer(dev, d, now);
    }
    this.queue.sort((a, b) => a.due - b.due);
    while (this.queue.length && this.queue[0].due <= now) { const q = this.queue.shift()!; this.counters.inFlight--; this.deliver(q.dev, q.d, q.due); }
    for (const dev of this.devs) if (now - dev.rx.lastRx >= cad.dropMs) dev.rx.ents.clear();
    this.sampleError(now, world);
    const cut = now - BACKLOG_MS;
    while (this.packets.length && this.packets[0].t - EPOCH < cut) this.packets.shift();
  }

  /** In-process shaper, in the server's order: loss, token bucket, delay (server/src/shaper.ts). */
  private offer(dev: Dev, d: Datagram, now: number) {
    const c = this.shaper, k = this.counters;
    k.offered++; k.offeredBytes += d.wire;
    dev.rx.offered.push(now, d.wire);
    let dropped = false;
    if (c.enabled) {
      if (c.loss > 0 && this.rng() < c.loss) { dropped = true; k.droppedLoss++; }
      else if (c.bps > 0) {
        const cap = c.bps / 8 * c.burstSec;
        this.tokens = Math.min(cap, this.tokens + Math.max(0, now - this.lastRefill) / 1000 * c.bps / 8);
        this.lastRefill = now;
        if (this.tokens <= 0) { dropped = true; k.droppedCap++; } else this.tokens -= d.wire;
      }
    }
    this.packets.push({ t: EPOCH + now, dir: 'up', key: `id:${dev.spec.id}`, kind: d.kind, bytes: d.wire, seq: d.seq, ids: d.ids, dropped });
    // Time on air is spent whether or not the datagram survives the link.
    const air = airtimeS(this.model, d.wire);
    dev.rx.air.push(now, air); this.linkAir.push(now, air); this.linkMsgs.push(now, 1);
    if (dropped) { k.dropped++; k.droppedBytes += d.wire; return; }
    k.passed++; k.passedBytes += d.wire; k.inFlight++;
    this.queue.push({ due: now + (c.enabled ? c.delayMs : 0), dev, d });
  }

  private deliver(dev: Dev, d: Datagram, now: number) {
    const k = this.counters;
    k.delivered++; k.deliveredBytes += d.wire;
    const wasSilent = now - dev.rx.lastRx;
    dev.rx.delivered.push(now, d.wire);
    const { ack, missing } = dev.rx.apply(d, now);
    this.note(now, `${dev.spec.addr} ${d.text}`);
    if (ack) {
      const wire = 14 + 2 * Math.min(missing, 16) + UDP_IP;
      this.packets.push({ t: EPOCH + now, dir: 'down', key: `id:${dev.spec.id}`, kind: 'ack', bytes: wire, dropped: false });
      // More than 8 seqs missing (a blackout): the edge sends a keyframe at once (core on_ack).
      if (missing > 8 || wasSilent > cadence(this.budget).coastMs) dev.edge.forceKf = true;
    }
  }

  private note(now: number, line: string) {
    if (this.log.length >= 200) this.log.shift();
    this.log.push(`${clock(EPOCH + now)} ${line}`);
  }

  private sampleError(now: number, world: Truth[]) {
    for (const dev of this.devs) for (const [id, s] of dev.rx.ents) {
      const tid = Number(Object.keys(dev.spec.sees).find(k => dev.spec.sees[+k] === id));
      const w = world.find(t => t.id === tid);
      if (w) this.err.push({ t: now, d: dist(predict(s, now).pos, w.pos) });
    }
    while (this.err.length && now - this.err[0].t > 10_000) this.err.shift();
  }

  twinError(): { meanM: number | null; p95M: number | null; samples: number } {
    if (!this.err.length) return { meanM: null, p95M: null, samples: 0 };
    const d = this.err.map(e => e.d).sort((a, b) => a - b);
    return { meanM: d.reduce((a, b) => a + b, 0) / d.length, p95M: d[Math.min(d.length - 1, Math.floor(d.length * 0.95))], samples: d.length };
  }

  // ---- snapshot ----
  snapshot(): Snapshot {
    const now = this.sim, cad = cadence(this.budget);
    const devices: DeviceView[] = [];
    for (const dev of this.devs) {
      const rx = dev.rx;
      if (!Number.isFinite(rx.lastRx)) continue;
      const silence = now - rx.lastRx;
      if (silence > 30_000) continue; // DEVICE_TIMEOUT_MS
      const coasting = silence >= cad.coastMs, silent = silence >= cad.staleMs;
      const entities: EntityView[] = [...rx.ents.values()].map(s => {
        const p = predict(s, now), age = now - s.t;
        return {
          id: s.id, class: s.cls, pos: p.pos, vel: p.vel, conf: s.conf, tick: Math.round(s.t / 1000 * TICK_HZ),
          age: Math.round(age / 1000 * TICK_HZ), stale: silent || age >= cad.staleMs,
          theta: s.theta, coasting, ce: coasting ? s.theta + prior(s.cls).maxSpeed * (silence - cad.coastMs) / 1000 : s.theta,
        };
      });
      const del = rx.delivered.sum(now), off = rx.offered.sum(now), air = rx.air.sum(now);
      devices.push({
        deviceId: dev.spec.id, addr: dev.spec.addr, entities,
        pose: { pos: dev.spec.pose.pos, quat: dev.spec.pose.quat, originLocked: true, tick: Math.round(now / 1000 * TICK_HZ) },
        bps: del.sum * 8 / (RATE_WINDOW_MS / 1000), msgsPerSec: del.n / (RATE_WINDOW_MS / 1000), stats: { ...rx.stats, thetaScale: +dev.edge.thetaScale.toFixed(2) },
        lastSeenMs: EPOCH + rx.lastRx, key: `id:${dev.spec.id}`, provisional: false, offeredBps: off.sum * 8 / (RATE_WINDOW_MS / 1000),
        edgeTick: Math.round(now / 1000 * TICK_HZ), silent, addrChanges: 0, clockOffsetMs: 12,
        cadence: cad, coasting, airtimeShare: air.sum / (RATE_WINDOW_MS / 1000),
      });
    }
    const global = this.fuse(devices);
    const entityCount = devices.reduce((a, d) => a + d.entities.length, 0);
    const linkAir = this.linkAir.sum(now), linkMsgs = this.linkMsgs.sum(now);
    return {
      t: EPOCH + now, devices, global, shaper: { ...this.shaper }, fusion: this.fusion,
      baselines: { h264_720p_bps: 1_500_000, h264_480p_bps: 500_000, naiveMetadataBps: entityCount * 31 * 30 * 8 + 30 * 40 * 8 },
      budgetBps: this.budget, shaperRevertMs: this.revert ? Math.max(0, this.revert.at - now) : null,
      link: {
        profile: this.profile, model: this.model, profiles: PROFILES,
        rateBps: this.profile === 'external' ? this.externalRate : this.shaper.enabled ? this.shaper.bps : 0,
        airtimeShare: linkAir.sum / (RATE_WINDOW_MS / 1000), msgsPerSec: linkMsgs.n / (RATE_WINDOW_MS / 1000),
      },
      packets: this.packets.splice(0),
      baselineA: this.measured ? BASELINE_MEASURED : BASELINE_CONFIGURED,
      geo: this.geo ? { ...ANCHOR, mgrs: mgrs(ANCHOR.lat, ANCHOR.lon) } : null,
    };
  }

  /** Fusion stand-in: devices see the same physical objects, so group by the truth id they map to. */
  private fuse(devices: DeviceView[]): GlobalEntity[] {
    const groups = new Map<string, { d: DeviceView; e: EntityView }[]>();
    for (const d of devices) {
      const spec = DEVS.find(s => s.id === d.deviceId)!;
      for (const e of d.entities) {
        const tid = Number(Object.keys(spec.sees).find(k => spec.sees[+k] === e.id));
        const gid = this.fusion || spec.id === 101 ? `g${tid}` : `g${10 + tid}`;
        groups.set(gid, [...(groups.get(gid) ?? []), { d, e }]);
      }
    }
    return [...groups].map(([gid, ms]) => {
      let w = 0; const pos = [0, 0, 0], vel = [0, 0, 0];
      for (const { e } of ms) { const c = e.stale ? 1 : e.conf + 1; w += c; for (let i = 0; i < 3; i++) { pos[i] += e.pos[i] * c; vel[i] += e.vel[i] * c; } }
      const p = pos.map(v => v / w) as V3;
      return {
        gid, class: ms[0].e.class, pos: p, vel: vel.map(v => v / w) as V3,
        sources: ms.map(({ d, e }) => ({ deviceId: d.deviceId, id: e.id })), stale: ms.every(m => m.e.stale),
        ce: Math.min(...ms.map(m => m.e.ce)), coasting: ms.every(m => m.e.coasting), geo: this.geo ? geoOf(p) : null,
      };
    });
  }
}

// ---- driver: phase jumps replay from the start of the loop ----------------------------------------
let mock: Mock;
let frozen = false;
const flags = { geo: env.MOCK_GEO !== '0', measured: env.MOCK_MEASURED === '1', legacy: env.MOCK_LEGACY === '1' };

function goto(phase: string, atS: number, hold: boolean) {
  const i = Math.max(0, PHASES.findIndex(p => p.name === phase));
  mock = new Mock(flags.geo, flags.measured);
  const target = phaseStartMs(i) + Math.max(0, Math.min(atS, PHASES[i].s - 0.001)) * 1000;
  while (mock.sim + STEP_MS <= target) mock.step();
  mock.script.hold = hold;
}
{
  const [phase, at] = (env.MOCK_PHASE ?? 'clean').split('@');
  goto(phase, Number(at ?? 0), env.MOCK_HOLD === '1');
}

/** Apply mock controls (env-like keys) from /mock or a WS {type:'mock'} message. */
function control(q: Record<string, unknown>) {
  const on = (v: unknown) => v === true || v === 1 || v === '1' || v === 'true' || v === 'on';
  if (q.geo !== undefined) { flags.geo = on(q.geo); mock.geo = flags.geo; }
  if (q.measured !== undefined) { flags.measured = on(q.measured); mock.measured = flags.measured; }
  if (q.legacy !== undefined) flags.legacy = on(q.legacy);
  if (q.phase !== undefined) {
    const [phase, at] = String(q.phase).split('@');
    goto(phase, Number(q.at ?? at ?? 0), on(q.hold));
  } else if (q.hold !== undefined) mock.script.hold = on(q.hold);
  if (q.script !== undefined) mock.script.on = on(q.script);
  if (q.freeze !== undefined) frozen = on(q.freeze);
  return state();
}
const state = () => ({
  phase: mock.phaseName(), at: +((mock.sim - mock.script.since) / 1000).toFixed(2), script: mock.script.on, hold: mock.script.hold,
  frozen, geo: mock.geo, measured: mock.measured, legacy: flags.legacy, profile: mock.profile, phases: PHASES.map(p => `${p.name} ${p.s} s`),
});

/** MOCK_LEGACY: the snapshot as the server sends it before the hackathon fields, to exercise the viewer's fallbacks. */
function legacyOf(s: Snapshot): unknown {
  const { link: _l, packets: _p, baselineA: _b, geo: _g, ...rest } = s;
  return {
    ...rest,
    devices: s.devices.map(({ cadence: _c, coasting: _co, airtimeShare: _a, ...d }) => ({ ...d, entities: d.entities.map(({ theta: _t, coasting: _ec, ce: _ce, ...e }) => e) })),
    global: s.global.map(({ ce: _ce, coasting: _co, geo: _gg, ...g }) => g),
  };
}

function operator(m: ControlMessage) {
  if (m.type !== 'fusion') mock.script.on = false; // link changes: manual from here on, like a presenter taking over
  if (m.type === 'shaper') {
    const r = Number(m.revertAfterMs);
    if (m.revertAfterMs !== undefined && Number.isFinite(r) && r > 0) mock.setFor(m.config ?? {}, Math.min(r, 3_600_000));
    else { mock.setShaper(m.config ?? {}); mock.profile = 'custom'; }
  } else if (m.type === 'budget') { const b = Number(m.bps); if (Number.isFinite(b) && b >= 0) { mock.budget = Math.round(b); mock.profile = 'custom'; } }
  else if (m.type === 'fusion') mock.fusion = !!m.enabled;
  else if (m.type === 'link') mock.applyProfile(m.profile, m.as);
}

// ---- HTTP + WebSocket, one port like the real server ---------------------------------------------
const CORS = { 'access-control-allow-origin': '*', 'access-control-allow-methods': 'GET, POST, OPTIONS', 'access-control-allow-headers': 'content-type' };
const server = http.createServer((req, res) => {
  const url = new URL(req.url ?? '/', 'http://localhost');
  const q = Object.fromEntries(url.searchParams);
  const send = (status: number, body: unknown) => { res.writeHead(status, { ...CORS, 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' }); res.end(JSON.stringify(body, null, 2)); };
  if (req.method === 'OPTIONS') { res.writeHead(204, CORS); res.end(); return; }
  switch (url.pathname) {
    case '/mock': send(200, control(q)); return;
    case '/api/metrics': {
      const te = mock.twinError();
      send(200, { t: EPOCH + mock.sim, mock: true, twinError: te, devices: mock.devs.map(d => ({ key: `id:${d.spec.id}`, twinError: te.meanM === null ? null : { ...te, updatedMs: EPOCH + mock.sim } })), budgetBps: mock.budget, fusion: mock.fusion });
      return;
    }
    case '/api/link': if (q.profile) operator({ type: 'link', profile: q.profile, as: q.as }); send(200, { profile: mock.profile }); return;
    case '/api/budget': if (q.bps !== undefined) operator({ type: 'budget', bps: Number(q.bps) }); send(200, { budgetBps: mock.budget }); return;
    case '/api/fusion': if (q.enabled !== undefined) operator({ type: 'fusion', enabled: ['1', 'true', 'on'].includes(q.enabled) }); send(200, { fusion: mock.fusion }); return;
    case '/api/shaper': {
      const { revertAfterMs, ...rest } = q;
      const c: Partial<ShaperConfig> = {};
      for (const k of ['bps', 'delayMs', 'loss', 'burstSec'] as const) if (rest[k] !== undefined) c[k] = Number(rest[k]);
      if (rest.enabled !== undefined) c.enabled = ['1', 'true', 'on'].includes(rest.enabled);
      if (Object.keys(c).length) operator({ type: 'shaper', config: c, revertAfterMs: revertAfterMs ? Number(revertAfterMs) : undefined });
      send(200, { config: mock.shaper, revertInMs: mock.revert ? mock.revert.at - mock.sim : null }); return;
    }
    default: send(404, { error: 'not found', mock: true, endpoints: ['GET /mock?phase=&at=&hold=&freeze=&geo=&measured=&legacy=&script=', 'GET /api/metrics', 'GET /api/link?profile=', 'GET /api/shaper?...', 'GET /api/budget?bps=', 'GET /api/fusion?enabled='], ...state() });
  }
});
const wss = new WebSocketServer({ server });
wss.on('connection', ws => {
  ws.on('message', raw => {
    let m: ControlMessage | ({ type: 'mock' } & Record<string, unknown>);
    try { m = JSON.parse(raw.toString()); } catch { return; }
    if (m.type === 'mock') control(m); else operator(m as ControlMessage);
  });
});
server.on('error', e => { console.error(`mock: ${e.message}`); process.exit(1); });
server.listen(PORT, () => console.log(`mock snapshot server on ws://localhost:${PORT} (phase ${mock.phaseName()}${mock.script.hold ? ', hold' : ''}${frozen ? ', frozen' : ''}); control: http://localhost:${PORT}/mock`));

const broadcast = (payload: string) => { for (const c of wss.clients) if (c.readyState === WebSocket.OPEN) c.send(payload); };
// MOCK_FREEZE: let the first viewer collect 1.5 s of history, then stop the clock.
let freezeAt = env.MOCK_FREEZE === '1' ? Infinity : null as number | null;
const timers = [
  setInterval(() => {
    if (!frozen) mock.step();
    // Snapshots consume `packets`: build them only for a viewer, so a phase jump's backlog reaches it.
    if (!wss.clients.size) return;
    const snap = mock.snapshot();
    broadcast(JSON.stringify({ type: 'snapshot', snap: flags.legacy ? legacyOf(snap) : snap }));
    if (freezeAt === Infinity) freezeAt = mock.sim + 1500;
    if (freezeAt !== null && mock.sim >= freezeAt) { frozen = true; freezeAt = null; }
  }, STEP_MS),
  setInterval(() => broadcast(JSON.stringify({ type: 'log', lines: mock.log.splice(0).slice(-60), shaper: { ...mock.counters } })), 500),
];
const shutdown = () => {
  timers.forEach(clearInterval);
  for (const c of wss.clients) c.terminate();
  wss.close(); server.close();
  setTimeout(() => process.exit(0), 200).unref();
};
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
