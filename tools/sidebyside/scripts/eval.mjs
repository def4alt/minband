#!/usr/bin/env node
// Offline evaluation of the link protocol (docs/PROTOCOL_EVAL.md): a footage tracks.csv goes through
// the real WASM edge, a shaped link (budget, delay, random loss, scripted blackouts, optional uplink
// loss) and the real WASM receiver at 10 Hz steps, faster than real time. Every step compares the
// edge's truth with the receiver's dead-reckoned snapshot.
//
//   node tools/sidebyside/scripts/eval.mjs --clip cons2 --profile lora --blackout 30:60
//   node tools/sidebyside/scripts/eval.mjs --clip all --profile lora,telemetry --blackout 30:20,30:60 --json out.json
//
// Flags (defaults in brackets):
//   --clip cons2|busy|all|<run dir>   [cons2]      cons2 = meva-uav-0307-1720/cons2 (35 tracks), busy = meva-2018-03-13 (206 tracks)
//   --profile a,b|all                 [lora]       clean hf lora telemetry contested blackout
//   --blackout s:len[,s:len]          [none]       scripted blackouts (seconds from the start, length); both directions down
//   --budget bps                      [profile]    override the edge budget
//   --loss p  --delay s               [profile]    random loss (downlink and uplink) and one-way delay
//   --uplink-loss p                   [= loss]     uplink loss; --no-uplink makes the link simplex
//   --duration s                      [clip + 60]  replay length
//   --seed n                          [1]          deterministic loss and nonce
//   --dev-factor k                    [1]          edge position change threshold = k x max(ce, 2 pos_res) (experiment knob)
//   --focus auto|<edge id>            [off]        also run the same replay with one contact focused and report the delta
//   --tail-contacts                   [off]        print the per-contact table (edge vs receiver) at the end
//   --json path                       [off]        write every number to a JSON file
//   --quiet                                        only the tables
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { loadMeta } from './meta.mjs';

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, '../../..');
const core = require(path.join(repo, 'core/pkg-node/minband_core.js'));
const { WasmEdge, WasmReceiver, peek } = core;

const TICK_HZ = 120, STEP_S = 0.1, UDP_IP_OVERHEAD = 28;
const ORIGIN_LAT = 39.0466, ORIGIN_LON = -85.5207;
const CLIPS = {
  cons2: 'runs/footage/meva-uav-0307-1720/cons2',
  busy: 'runs/footage/meva-2018-03-13.16-00-14-bf',
};
const PROFILES = {
  clean: { budgetBps: 0, loss: 0, delayS: 0.02, up: true, video: true },
  hf: { budgetBps: 9600, loss: 0.01, delayS: 0.5, up: true, video: false },
  lora: { budgetBps: 2000, loss: 0.1, delayS: 0.3, up: true, video: false },
  telemetry: { budgetBps: 600, loss: 0.05, delayS: 0.05, up: true, video: false },
  contested: { budgetBps: 2000, loss: 0.1, delayS: 0.3, up: true, video: false, bursts: true },
  blackout: { budgetBps: 2000, loss: 1, delayS: 0.3, up: false, video: false },
};
const EVENT_KINDS = ['new', 'confirmed', 'moving', 'stopped', 'static', 'lost', 'reacquired', 'departed', 'grew', 'shrank'];
const REC_NAMES = { 1: 'session', 2: 'ego', 3: 'pose', 4: 'contact', 5: 'chiphead', 6: 'chipsym', 7: 'note', 0x81: 'digest', 0x82: 'focus', 0x83: 'clock', 0x84: 'chipack' };

// ---- args ----------------------------------------------------------------------------------------
function parseArgs(argv) {
  const a = { debug: false, clip: 'cons2', profile: 'lora', blackout: '', seed: 1, focus: '', json: '', quiet: false, tailContacts: false, noUplink: false };
  for (let i = 0; i < argv.length; i++) {
    const k = argv[i];
    const next = () => argv[++i];
    switch (k) {
      case '--clip': a.clip = next(); break;
      case '--profile': a.profile = next(); break;
      case '--blackout': a.blackout = next(); break;
      case '--budget': a.budget = Number(next()); break;
      case '--loss': a.loss = Number(next()); break;
      case '--delay': a.delay = Number(next()); break;
      case '--uplink-loss': a.uplinkLoss = Number(next()); break;
      case '--no-uplink': a.noUplink = true; break;
      case '--duration': a.duration = Number(next()); break;
      case '--seed': a.seed = Number(next()); break;
      case '--dev-factor': a.devFactor = Number(next()); break;
      case '--focus': a.focus = next(); break;
      case '--json': a.json = next(); break;
      case '--quiet': a.quiet = true; break;
      case '--debug': a.debug = true; break;
      case '--tail-contacts': a.tailContacts = true; break;
      case '-h': case '--help': console.log(fs.readFileSync(fileURLToPath(import.meta.url), 'utf8').split('\n').slice(1, 24).map((l) => l.replace(/^\/\/ ?/, '')).join('\n')); process.exit(0);
      default: throw new Error(`unknown flag ${k}`);
    }
  }
  return a;
}

// ---- rng (mulberry32) ----------------------------------------------------------------------------
function rng(seed) {
  let s = seed >>> 0;
  return () => { s = (s + 0x6d2b79f5) >>> 0; let t = s; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
}

// ---- data ----------------------------------------------------------------------------------------
function loadClip(runDir) {
  const meta = loadMeta(runDir);
  const byTick = new Map();
  const spans = new Map();
  for (const line of fs.readFileSync(path.join(runDir, 'tracks.csv'), 'utf8').split('\n').slice(1)) {
    if (!line) continue;
    const c = line.split(',');
    const tick = Number(c[0]);
    const tr = { id: +c[1], class: +c[2], e: +c[3], n: -c[5], ve: +c[6], vn: -c[8], conf: Math.max(0, Math.min(255, Math.round(+c[9]))) };
    let arr = byTick.get(tick); if (!arr) byTick.set(tick, (arr = [])); arr.push(tr);
    const s = spans.get(tr.id) || { first: tick, last: tick }; s.last = tick; spans.set(tr.id, s);
  }
  const ticks = [...byTick.keys()].sort((a, b) => a - b);
  const tracksAt = (t) => {
    const tick = Math.round(t * TICK_HZ);
    let lo = 0, hi = ticks.length - 1, best = -1;
    while (lo <= hi) { const m = (lo + hi) >> 1; if (ticks[m] <= tick) { best = m; lo = m + 1; } else hi = m - 1; }
    if (best < 0 || tick - ticks[best] > 60) return [];
    return byTick.get(ticks[best]);
  };
  const pitch = (meta.ground.pitch_deg * Math.PI) / 180;
  const cam = { e: meta.camera_m[0], n: -meta.camera_m[2], alt: meta.ground.height_m, pitchDeg: -meta.ground.pitch_deg };
  cam.fpN = cam.n + cam.alt / Math.tan(pitch);
  cam.fpR = (cam.alt * Math.tan((meta.ground.hfov_deg / 2) * Math.PI / 180)) / Math.sin(pitch);
  const lastTick = ticks[ticks.length - 1];
  // The longest-lived track, for `--focus auto`.
  let longest = null; for (const [id, s] of spans) if (!longest || s.last - s.first > longest.len) longest = { id, len: s.last - s.first, first: s.first };
  return { meta, tracksAt, firstS: ticks[0] / TICK_HZ, lastS: lastTick / TICK_HZ, rows: [...byTick.values()].reduce((s, v) => s + v.length, 0), nTracks: spans.size, longest, cam };
}

// ---- one replay ------------------------------------------------------------------------------------
function replay(clip, P, opt) {
  const rand = rng(opt.seed);
  const nonce = (opt.seed * 2654435761) >>> 0 || 1;
  const meta = clip.meta, cam = clip.cam;
  const edge = new WasmEdge(JSON.stringify({
    device_id: 1, nonce, origin_lat_e7: Math.round(ORIGIN_LAT * 1e7), origin_lon_e7: Math.round(ORIGIN_LON * 1e7), origin_alt: 32767, pos_res: 2,
    caps: 1 | 2 | 4 | 8 | (opt.uplink ? 0x400 : 0) | 0x800, hfov_x10: Math.round(meta.ground.hfov_deg * 10), img_w: meta.width, img_h: meta.height,
    video_frame0: meta.start_frame, fps_x100: Math.round(meta.fps * 100), budget_bps: P.budgetBps, max_frame: 1200,
    sigma_own: 3.0, sigma_att_deg: 1.0, sigma_h: 2.0, sigma_px: 2.0, f_px: meta.ground.f_px,
    contacts: opt.devFactor ? { dev_factor: opt.devFactor } : undefined,
  }));
  const rx = new WasmReceiver(P.budgetBps);
  const pending = [];
  const blackouts = opt.blackouts.map((b) => ({ start: b.start, end: b.start + b.len, recoveredRev: null, recoveredAny: null, compAtEnd: null, maxAfter: 0 }));
  let burstUntil = -1, nextBurst = -1;
  const inBlackout = (t) => blackouts.some((b) => t >= b.start && t < b.end);
  const linkUp = (t) => {
    if (!P.up || inBlackout(t)) return false;
    if (P.bursts) {
      if (nextBurst < 0) nextBurst = t + 3 + rand() * 5;
      if (t >= nextBurst && t > burstUntil) { burstUntil = t + 1 + rand() * 4; nextBurst = burstUntil + 3 + rand() * 5; }
      if (t <= burstUntil) return false;
    }
    return true;
  };

  // Accumulators.
  const bytes = { down: {}, up: {}, header: 0, carrier: 0, downFrames: 0, downDelivered: 0, downDeliveredBytes: 0, upFrames: 0, upDelivered: 0, maxFrame: 0 };
  const steps = { n: 0, compRev: 0, compAny: 0, withLive: 0, kOfN: 0, withOf: 0, ofExact: 0, unheardSteps: 0, liveSum: 0, rxLiveSum: 0 };
  const honest = { in: 0, all: 0, inBlackout: 0, allBlackout: 0, errSum: 0, ghostSum: 0, ghostN: 0, by: {} };
  const latency = Object.fromEntries(EVENT_KINDS.map((k) => [k, { edge: 0, matched: 0, samples: [], rxUnmatched: 0 }]));
  const pendingEdge = []; // {id, kind, t}
  const prevEdge = new Map(); // id -> {motion, lost, departed, count, confirmed, rev}
  const revTick = new Map(); // `${id}:${rev}` -> t the edge moved to that revision
  const focusTarget = { id: null, since: null, steps: 0, compRev: 0, errSum: 0, n: 0, in: 0 };
  const series = [];
  let lastDigest = -1e9, digestsSent = 0, focusSent = 0;
  const duration = opt.duration;
  const focusWanted = opt.focus;

  for (let step = 0; step * STEP_S < duration; step++) {
    const t = step * STEP_S, tick = Math.round(t * TICK_HZ);
    const up = linkUp(t);
    const tracks = clip.tracksAt(t);
    const ego = { e: cam.e, n: cam.n, alt_agl: cam.alt, heading_deg: 0, speed: 0, climb: 0, nav_mode: 2, gnss: 2, battery: Math.max(0, Math.round(83 - t / 30)),
      pos_ce: 3.0, fp_e: cam.e, fp_n: cam.fpN, fp_radius: cam.fpR, video: P.video && up };
    edge.pose(tick, cam.e, cam.n, cam.alt, 0, cam.pitchDeg, 0);
    const out = edge.tick(JSON.stringify(tracks), JSON.stringify(ego), tick);
    for (const b of unpack(out)) {
      const fb = new Uint8Array(b);
      const pk = JSON.parse(peek(fb));
      bytes.downFrames++; bytes.header += fb.length - pk.records.reduce((s, r) => s + r.len + 2, 0); bytes.carrier += UDP_IP_OVERHEAD;
      bytes.maxFrame = Math.max(bytes.maxFrame, fb.length);
      for (const r of pk.records) { const k = REC_NAMES[r.type] || `t${r.type}`; bytes.down[k] = (bytes.down[k] || 0) + r.len + 2; }
      const delivered = up && rand() >= P.loss;
      if (delivered) { bytes.downDelivered++; bytes.downDeliveredBytes += fb.length; pending.push({ at: t + P.delayS, bytes: fb, up: false }); }
    }
    // Uplink: a digest every 5 s while frames arrive, focus renewals with it.
    if (opt.uplink && up && t - lastDigest >= 5 && rx.needs_digest()) {
      lastDigest = t;
      sendUp(rx.make_digest(P.budgetBps, tick), t);
      if (focusTarget.id != null && focusTarget.steps) sendUp(rx.make_focus(focusTarget.id, 1, 60, 1, tick), t);
    }
    pending.sort((a, b) => a.at - b.at);
    while (pending.length && pending[0].at <= t) {
      const p = pending.shift();
      try { if (p.up) edge.on_uplink(p.bytes, tick); else rx.on_frame(p.bytes); } catch (e) { console.warn('decode', String(e)); }
    }
    if (tick % (10 * TICK_HZ) === 0) rx.gc(tick);

    // ---- truth vs picture ----
    const es = JSON.parse(edge.snapshot_json(tick));
    const edgeAll = es.contacts.filter((c) => c.parent == null);
    const live = edgeAll.filter((c) => !c.departed && !c.lost);
    const rxAll = JSON.parse(rx.snapshot_json(tick));
    const rxMap = new Map(rxAll.filter((c) => !c.departed && !c.child).map((c) => [c.id, c]));
    const edgeMap = new Map(edgeAll.map((c) => [c.id, c]));
    // Focus target: the contact holding the longest track, from 2 s after it is born.
    if (focusWanted && focusTarget.id == null) {
      const want = focusWanted === 'auto' ? edgeAll.find((c) => !c.departed && c.members.includes(clip.longest.id)) : edgeMap.get(Number(focusWanted));
      if (want) { focusTarget.id = want.id; focusTarget.since = t + 2; }
    }
    if (focusTarget.id != null && t >= focusTarget.since && focusTarget.steps === 0 && opt.uplink) { sendUp(rx.make_focus(focusTarget.id, 1, 60, 1, tick), t); focusTarget.steps = 1; }

    let atRev = 0, any = 0;
    for (const c of live) { const r = rxMap.get(c.id); if (r) { any++; if (r.rev === c.rev) atRev++; } }
    const compRev = live.length ? atRev / live.length : null, compAny = live.length ? any / live.length : null;
    steps.n++;
    if (compRev != null) { steps.withLive++; steps.compRev += compRev; steps.compAny += compAny; }
    steps.liveSum += live.length; steps.rxLiveSum += [...rxMap.values()].filter((c) => !c.lost).length;
    const known = rx.known(), of = rx.of();
    if (of >= 0 && live.length) { steps.withOf++; steps.kOfN += Math.min(1, known / Math.max(1, of)); if (of === live.length) steps.ofExact++; }
    if (rx.device_unheard(tick)) steps.unheardSteps++;
    const bo = inBlackout(t);
    for (const r of rxMap.values()) {
      if (r.lost) continue;
      const c = edgeMap.get(r.id);
      if (!c || c.departed || c.lost) continue;
      const d = Math.hypot(c.e - r.e, c.n - r.n);
      honest.all++; honest.errSum += d; if (d <= r.ce_shown) honest.in++;
      { const key = `${r.motion}${r.rev === c.rev ? '' : '/stale'}${c.motion !== r.motion ? '->' + c.motion : ''}`; const h = honest.by[key] || (honest.by[key] = { n: 0, in: 0, err: 0, ce: 0 }); h.n++; h.err += d; h.ce += r.ce_shown; if (d <= r.ce_shown) h.in++; }
      if (bo) { honest.allBlackout++; if (d <= r.ce_shown) honest.inBlackout++; }
      honest.ghostSum += r.ce_shown; honest.ghostN++;
      if (focusTarget.id === r.id && focusTarget.steps) { focusTarget.n++; focusTarget.errSum += d; if (d <= r.ce_shown) focusTarget.in++; if (r.rev === c.rev) focusTarget.compRev++; }
    }
    for (const b of blackouts) {
      if (t < b.end || compRev == null) continue;
      if (b.compAtEnd == null) b.compAtEnd = compRev;
      b.maxAfter = Math.max(b.maxAfter, compRev);
      if (b.recoveredRev == null && compRev >= 0.95) b.recoveredRev = t - b.end;
      if (b.recoveredAny == null && compAny >= 0.95) b.recoveredAny = t - b.end;
    }
    // Edge transitions (what the receiver should derive).
    for (const c of edgeAll) {
      const p = prevEdge.get(c.id);
      const push = (kind) => { pendingEdge.push({ id: c.id, kind, t }); latency[kind].edge++; };
      if (!p) {
        if (!c.departed) { push('new'); if (c.motion === 'moving') push('moving'); if (c.lost) push('lost'); }
      } else {
        if (!p.departed && c.departed) push('departed');
        else if (!c.departed) {
          if (!p.confirmed && c.confirmed) push('confirmed');
          if (p.motion !== c.motion && ['moving', 'stopped', 'static'].includes(c.motion)) push(c.motion);
          if (!p.lost && c.lost) push('lost');
          if (p.lost && !c.lost) push('reacquired');
          if (p.count !== c.count) push(c.count > p.count ? 'grew' : 'shrank');
        }
      }
      if (!p || p.rev !== c.rev) revTick.set(`${c.id}:${c.rev}`, t);
      prevEdge.set(c.id, { motion: c.motion, lost: c.lost, departed: c.departed, count: c.count, confirmed: c.confirmed, rev: c.rev });
    }
    for (const ev of JSON.parse(rx.events_json())) {
      const L = latency[ev.kind]; if (!L) continue;
      const i = pendingEdge.findIndex((p) => p.id === ev.id && p.kind === ev.kind);
      if (i < 0) { L.rxUnmatched++; continue; }
      const p = pendingEdge.splice(i, 1)[0];
      L.matched++; L.samples.push(t - p.t); // arrival step at the receiver, not the frame tick
    }
    if (step % 10 === 0) series.push({ t: +t.toFixed(1), live: live.length, rx: rxMap.size, compRev: compRev == null ? null : +compRev.toFixed(3), compAny: compAny == null ? null : +compAny.toFixed(3), up, known, of });
  }

  function sendUp(b, tNow) {
    bytes.upFrames++;
    const pk = JSON.parse(peek(b));
    for (const r of pk.records) { const k = REC_NAMES[r.type] || `t${r.type}`; bytes.up[k] = (bytes.up[k] || 0) + r.len + 2; }
    bytes.up.header = (bytes.up.header || 0) + b.length - pk.records.reduce((s, r) => s + r.len + 2, 0);
    if (rand() >= opt.uplinkLoss) { bytes.upDelivered++; pending.push({ at: tNow + P.delayS, bytes: b, up: true }); }
    if (pk.records.some((r) => r.type === 0x81)) digestsSent++; else focusSent++;
  }

  const q = (arr, p) => { if (!arr.length) return null; const s = [...arr].sort((a, b) => a - b); return s[Math.min(s.length - 1, Math.floor(p * s.length))]; };
  const lat = {};
  const unmatchedEdge = pendingEdge.map((p) => `${p.kind}#${p.id}@${p.t.toFixed(1)}`);
  for (const k of EVENT_KINDS) { const L = latency[k]; lat[k] = { edge: L.edge, matched: L.matched, missed: L.edge - L.matched, rxUnmatched: L.rxUnmatched, median: q(L.samples, 0.5), p90: q(L.samples, 0.9), max: q(L.samples, 1) }; }
  const downApp = Object.values(bytes.down).reduce((s, v) => s + v, 0) + bytes.header;
  const cmStats = JSON.parse(edge.snapshot_json(Math.round(duration * TICK_HZ))).stats;
  const edgeStats = JSON.parse(edge.stats_json()), rxStats = JSON.parse(rx.stats_json()), timing = JSON.parse(edge.timing_json());
  return {
    duration, steps: steps.n,
    completeness: { rev: steps.withLive ? steps.compRev / steps.withLive : null, any: steps.withLive ? steps.compAny / steps.withLive : null, meanLive: steps.liveSum / steps.n, meanRxLive: steps.rxLiveSum / steps.n },
    integrity: { kOfN: steps.withOf ? steps.kOfN / steps.withOf : null, ofExact: steps.withOf ? steps.ofExact / steps.withOf : null, deviceUnheard: steps.unheardSteps / steps.n },
    honesty: { by: Object.fromEntries(Object.entries(honest.by).map(([k, h]) => [k, { n: h.n, frac: h.in / h.n, meanErr: h.err / h.n, meanCe: h.ce / h.n }])), frac: honest.all ? honest.in / honest.all : null, blackout: honest.allBlackout ? honest.inBlackout / honest.allBlackout : null, meanErr: honest.all ? honest.errSum / honest.all : null, meanCeShown: honest.ghostN ? honest.ghostSum / honest.ghostN : null },
    latency: lat, unmatchedEdge,
    blackouts: blackouts.map((b) => ({ start: b.start, len: b.end - b.start, recoveredRev: b.recoveredRev, recoveredAny: b.recoveredAny, compAtEnd: b.compAtEnd, maxAfter: b.maxAfter })),
    bytes: { perS: Object.fromEntries(Object.entries(bytes.down).map(([k, v]) => [k, v / duration])), headerPerS: bytes.header / duration, appPerS: downApp / duration, wirePerS: (downApp + bytes.carrier) / duration,
      deliveredPerS: bytes.downDeliveredBytes / duration, frames: bytes.downFrames, delivered: bytes.downDelivered, meanFrame: bytes.downFrames ? downApp / bytes.downFrames : 0, maxFrame: bytes.maxFrame,
      upPerS: Object.values(bytes.up).reduce((s, v) => s + v, 0) / duration, upFrames: bytes.upFrames, upDelivered: bytes.upDelivered, digests: digestsSent, focusCmds: focusSent },
    focus: focusTarget.id == null ? null : { id: focusTarget.id, since: focusTarget.since, steps: focusTarget.n, compRev: focusTarget.n ? focusTarget.compRev / focusTarget.n : null, meanErr: focusTarget.n ? focusTarget.errSum / focusTarget.n : null, honesty: focusTarget.n ? focusTarget.in / focusTarget.n : null },
    edgeStats, rxStats, cmStats, timing, series,
    contacts: opt.tailContacts ? { edge: edgeAll, rx: rxAll } : undefined,
  };
}

function unpack(buf) {
  const out = []; let i = 0;
  while (i + 2 <= buf.length) { const n = buf[i] | (buf[i + 1] << 8); i += 2; out.push(buf.subarray(i, i + n)); i += n; }
  return out;
}

// ---- tables -----------------------------------------------------------------------------------------
const f2 = (x) => (x == null ? '-' : x.toFixed(2));
const f1 = (x) => (x == null ? '-' : x.toFixed(1));
const pct = (x) => (x == null ? '-' : (100 * x).toFixed(1) + '%');
function table(rows) {
  const cols = Object.keys(rows[0]);
  const w = cols.map((c) => Math.max(c.length, ...rows.map((r) => String(r[c]).length)));
  const line = (vals) => '| ' + vals.map((v, i) => String(v).padEnd(w[i])).join(' | ') + ' |';
  return [line(cols), '|' + w.map((x) => '-'.repeat(x + 2)).join('|') + '|', ...rows.map((r) => line(cols.map((c) => r[c])))].join('\n');
}

function summaryRow(clipName, profName, P, r) {
  const L = r.latency;
  const lat = (k) => (L[k].matched ? `${f1(L[k].median)}/${f1(L[k].p90)}` : '-') + (L[k].missed ? ` (${L[k].missed} miss)` : '');
  return {
    clip: clipName, profile: profName, 'bit/s': P.budgetBps, loss: P.loss, 'comp@rev': pct(r.completeness.rev), 'comp@any': pct(r.completeness.any),
    honest: pct(r.honesty.frac), 'err m': f1(r.honesty.meanErr), 'ce_shown m': f1(r.honesty.meanCeShown), 'k/n': pct(r.integrity.kOfN),
    'new s': lat('new'), 'moving s': lat('moving'), 'stopped s': lat('stopped'), 'lost s': lat('lost'), 'departed s': lat('departed'),
    'app B/s': f1(r.bytes.appPerS), 'wire B/s': f1(r.bytes.wirePerS), 'contact B/s': f1(r.bytes.perS.contact || 0), 'ego B/s': f1(r.bytes.perS.ego || 0), 'hdr B/s': f1(r.bytes.headerPerS),
    'frame B': `${f1(r.bytes.meanFrame)}/${r.bytes.maxFrame}`,
    recovery: r.blackouts.length ? r.blackouts.map((b) => `${b.len}s: ${b.recoveredRev == null ? `never (max ${pct(b.maxAfter)})` : f1(b.recoveredRev) + 's'}`).join('; ') : '-',
  };
}

// ---- main -------------------------------------------------------------------------------------------
const args = parseArgs(process.argv.slice(2));
const clipNames = args.clip === 'all' ? Object.keys(CLIPS) : args.clip.split(',');
const profNames = args.profile === 'all' ? Object.keys(PROFILES) : args.profile.split(',');
const blackouts = args.blackout ? args.blackout.split(',').map((s) => { const [a, b] = s.split(':').map(Number); return { start: a, len: b }; }) : [];
const results = [];
const rows = [];
for (const clipName of clipNames) {
  const runDir = CLIPS[clipName] ? path.join(repo, CLIPS[clipName]) : clipName;
  const clip = loadClip(runDir);
  const duration = args.duration || Math.ceil(clip.lastS + 75);
  if (!args.quiet) console.log(`clip ${clipName}: ${clip.nTracks} tracks, ${clip.rows} rows, ${clip.firstS.toFixed(1)}..${clip.lastS.toFixed(1)} s, replay ${duration} s, camera (${clip.cam.e.toFixed(1)}, ${clip.cam.n.toFixed(1)}, alt ${clip.cam.alt.toFixed(1)})`);
  for (const profName of profNames) {
    const base = PROFILES[profName]; if (!base) throw new Error(`unknown profile ${profName}`);
    const P = { ...base };
    if (args.budget != null) P.budgetBps = args.budget;
    if (args.loss != null) P.loss = args.loss;
    if (args.delay != null) P.delayS = args.delay;
    const opt = { seed: args.seed, devFactor: args.devFactor, blackouts, duration, uplink: !args.noUplink && P.up, uplinkLoss: args.uplinkLoss ?? P.loss, focus: '', tailContacts: args.tailContacts };
    const t0 = Date.now();
    const r = replay(clip, P, opt);
    const ms = Date.now() - t0;
    let focused = null;
    if (args.focus) {
      focused = replay(clip, P, { ...opt, focus: args.focus });
      focused.costPerS = focused.bytes.appPerS - r.bytes.appPerS;
    }
    results.push({ clip: clipName, profile: profName, P, opt: { ...opt, blackouts }, result: r, focused });
    rows.push(summaryRow(clipName, profName, P, r));
    if (!args.quiet) {
      console.log(`  ${profName}: ${r.steps} steps in ${ms} ms; regime ${r.timing.regime} f=${r.timing.f.toFixed(2)} floor ${(r.timing.floor / TICK_HZ).toFixed(0)} s; frames ${r.bytes.frames} (${r.bytes.delivered} delivered), digests ${r.bytes.digests} (${r.bytes.upDelivered} up delivered); rx rejected ${r.rxStats.rejected} stale ${r.rxStats.stale_copies}; contact sends ${r.edgeStats.contacts_sent}; revisions ${r.cmStats.revisions} by reason [first ${r.cmStats.rev_why[0]}, state ${r.cmStats.rev_why[1]}, position ${r.cmStats.rev_why[2]}, ce ${r.cmStats.rev_why[3]}, course ${r.cmStats.rev_why[4]}, speed ${r.cmStats.rev_why[5]}]`);
      if (focused) console.log(`  ${profName} focus ${focused.focus?.id ?? '-'}: +${f1(focused.costPerS)} B/s (${f1(r.bytes.appPerS)} -> ${f1(focused.bytes.appPerS)}), target comp@rev ${pct(focused.focus?.compRev)}, err ${f1(focused.focus?.meanErr)} m, honest ${pct(focused.focus?.honesty)}; overall comp@rev ${pct(focused.completeness.rev)} honest ${pct(focused.honesty.frac)}`);
      if (r.blackouts.length) for (const b of r.blackouts) console.log(`  ${profName} blackout ${b.start}+${b.len}s: comp@rev at end ${pct(b.compAtEnd)}, back to 95% after ${b.recoveredRev == null ? 'never' : f1(b.recoveredRev) + ' s'} (any rev: ${b.recoveredAny == null ? 'never' : f1(b.recoveredAny) + ' s'}); honesty during ${pct(r.honesty.blackout)}`);
      if (args.debug) {
        console.log(`  ${profName} unmatched edge transitions: ${r.unmatchedEdge.join(' ') || 'none'}`);
        console.log(`  ${profName} honesty by state: ` + Object.entries(r.honesty.by).sort((a, b) => b[1].n - a[1].n).map(([k, h]) => `${k} n=${h.n} in=${pct(h.frac)} err=${f1(h.meanErr)} ce=${f1(h.meanCe)}`).join(' | '));
        console.log(`  ${profName} mean live at edge ${f1(r.completeness.meanLive)}, held live at rx ${f1(r.completeness.meanRxLive)}`);
      }
      if (args.tailContacts) {
        const rxm = new Map(r.contacts.rx.map((c) => [c.id, c]));
        console.log(table(r.contacts.edge.filter((c) => !c.departed).map((c) => { const x = rxm.get(c.id); return { id: c.id, rev: c.rev, motion: c.motion, lost: c.lost, count: c.count, step: c.step, sends: c.sends, rx_rev: x ? x.rev : '-', rx_live: x ? x.liveness : '-', err: x ? f1(Math.hypot(c.e - x.e, c.n - x.n)) : '-', ce_shown: x ? f1(x.ce_shown) : '-' }; })));
      }
    }
  }
}
console.log(table(rows));
if (args.json) { fs.writeFileSync(args.json, JSON.stringify({ args, results }, null, 1)); if (!args.quiet) console.log(`wrote ${args.json}`); }
