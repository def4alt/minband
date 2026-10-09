// In-process replay of a ground-truth log through the real WASM Edge and Receiver, over a
// simulated link (seeded Bernoulli loss + fixed delay, both directions). No server, no sockets.
//   npm run replay -- runs/synth/one_walker.csv
//   npm run replay -- runs/synth/crowd.csv --theta 0.15 --loss 0.2 --delay 6 --ack gap
//
// Per tick (1/120 s) from the first to the last logged tick:
//   1. deliver acks due at this tick to the edge
//   2. on a logged frame: edge.tick(tracks, tick); each datagram is counted on the wire, then
//      lost with probability `loss` or queued for delivery at tick + delayTicks
//   3. deliver datagrams due at this tick to the receiver
//   4. on a logged frame: compare every GT track with receiver.extrapolate_json(tick)
//   5. acks go back over the same lossy, delayed link. Mode "gap" (default, as in the golden
//      test): every `ackEveryTicks`, if the receiver needs_ack(). Mode "server" (what
//      server/src/world.ts does): after each delivered datagram, if needs_ack() or at least
//      `ackEveryTicks` (100 ms) since the last ack. Mode "none": no acks, so no state repair.
import { parseArgs } from 'node:util';
import { basename } from 'node:path';
import { WasmEdge, WasmReceiver } from 'minband-core';
import { TICK_HZ, readGt, toFrames, entityStats, type Frame, type GtRow } from './gt.ts';
import { rng, subSeed } from './rng.ts';
import { isMain, userPath } from './paths.ts';

/** Ack { last_seq: 0, missing: [], budget_bps: 0 } (protocol v1), so the edge skips the Hello handshake. */
export const PRE_ACK = new Uint8Array([1, 4, 0, 0, 0]);
/** UDP (8) + IPv4 (20) header bytes added to every datagram for wire accounting. */
export const HEADER_BYTES = 28;
const KIND = { hello: 0, delta: 1, keyframe: 2, pose: 3, ack: 4, bye: 5 } as const;

export type AckMode = 'gap' | 'server' | 'none';

export interface ReplayOptions {
  thetaPos: number;
  /** Default 2 * thetaPos. */
  thetaVel?: number;
  /** Forward (edge -> receiver) datagram loss probability. Default 0. */
  loss?: number;
  /** Reverse (ack) loss probability. Default = loss. */
  ackLoss?: number;
  /** One-way delay in ticks, both directions. Default 0. */
  delayTicks?: number;
  seed?: number;
  ackMode?: AckMode;
  ackEveryTicks?: number;
  /** Error charged for a GT row whose entity is absent from the twin. Default 2.0 m. */
  missingPenaltyM?: number;
  /** Advertised in every ack like the server does: the edge's budget controller and keyframe/hello
   *  cadence and the receiver's coast/stale/drop limits follow it. 0 = unlimited (thresholds stay fixed). */
  budgetBps?: number;
  /** Call receiver.gc(tick) every frame like the server does (drops entities silent for 10 s). Default true. */
  gc?: boolean;
}

export interface ReplayResult {
  thetaPos: number; thetaVel: number; loss: number; ackLoss: number; delayTicks: number; ackMode: AckMode; seed: number;
  durationS: number; frames: number; frameHz: number; gtRows: number; entitiesMean: number; entitiesMax: number;
  // edge -> receiver traffic, counted at the sender (what the uplink has to carry)
  datagrams: number; deltas: number; keyframes: number; otherDatagrams: number; updates: number;
  payloadBytes: number; wireBytes: number; bytesPerSec: number; kbps: number;
  lostDatagrams: number;
  // receiver -> edge
  acksSent: number; acksDelivered: number; ackWireBytes: number;
  receiver: { gapsDetected: number; nacksSent: number; outOfOrderDropped: number };
  // twin fidelity, one sample per GT row
  errMean: number; errP95: number; errMax: number;   // missing rows count as missingPenaltyM
  errMeanPresent: number; errP95Present: number; errMaxPresent: number; // present rows only
  missingRows: number; availability: number;          // share of GT rows present in the twin
  phantomRows: number;                                // twin entities absent from the GT frame
  phantomMaxS: number;                                // longest time one phantom stayed in the twin
  missingPenaltyM: number;
}

export function unpack(buf: Uint8Array): Uint8Array[] {
  const out: Uint8Array[] = [];
  let i = 0;
  while (i + 2 <= buf.length) {
    const n = buf[i] | (buf[i + 1] << 8);
    i += 2;
    out.push(buf.slice(i, i + n));
    i += n;
  }
  return out;
}

interface Twin { id: number; pos: [number, number, number] }

/** p-quantile (nearest rank) of an unsorted array; 0 for an empty one. */
export function quantile(xs: Float64Array | number[], p: number): number {
  if (!xs.length) return 0;
  const s = Float64Array.from(xs).sort();
  return s[Math.min(s.length - 1, Math.max(0, Math.ceil(p * s.length) - 1))];
}

export function replayFrames(frames: Frame[], step: number, opts: ReplayOptions): ReplayResult {
  const thetaPos = opts.thetaPos, thetaVel = opts.thetaVel ?? 2 * thetaPos;
  const loss = opts.loss ?? 0, ackLoss = opts.ackLoss ?? loss, delay = Math.max(0, Math.round(opts.delayTicks ?? 0));
  const seed = opts.seed ?? 1, ackMode = opts.ackMode ?? 'gap', ackEvery = opts.ackEveryTicks ?? 12;
  const penalty = opts.missingPenaltyM ?? 2.0;
  const fwdRng = rng(subSeed(seed, 'link:fwd')), revRng = rng(subSeed(seed, 'link:rev'));

  const budget = opts.budgetBps ?? 0;
  const edge = WasmEdge.with_thresholds(1, 0xE7A1, thetaPos, thetaVel, budget);
  const rx = new WasmReceiver();
  edge.on_datagram(rx.make_ack(budget)); // PRE_ACK carrying the budget

  const fwd: { at: number; d: Uint8Array }[] = [];
  const rev: { at: number; d: Uint8Array }[] = [];
  let datagrams = 0, deltas = 0, keyframes = 0, other = 0, payload = 0, lost = 0;
  let acksSent = 0, acksDelivered = 0, ackBytes = 0, lastAck = -Infinity;
  const sendAck = (tick: number) => {
    const ack = rx.make_ack(budget);
    acksSent++; ackBytes += ack.length + HEADER_BYTES; lastAck = tick;
    if (!(ackLoss > 0 && revRng.next() < ackLoss)) rev.push({ at: tick + delay, d: ack });
    while (rev.length && rev[0].at <= tick) { edge.on_datagram(rev.shift()!.d); acksDelivered++; }
  };
  const errs: number[] = [], present: number[] = [];
  let missing = 0, phantom = 0, gtRows = 0, phantomMax = 0;
  const phantomSince = new Map<number, number>();

  const first = frames.length ? frames[0].tick : 0, last = frames.length ? frames[frames.length - 1].tick : 0;
  let fi = 0;
  try {
    for (let tick = first; tick <= last; tick++) {
      while (rev.length && rev[0].at <= tick) { edge.on_datagram(rev.shift()!.d); acksDelivered++; }

      const frame = fi < frames.length && frames[fi].tick === tick ? frames[fi++] : null;
      if (frame) {
        for (const d of unpack(edge.tick(JSON.stringify(frame.tracks), tick))) {
          datagrams++; payload += d.length;
          if (d[1] === KIND.delta) deltas++; else if (d[1] === KIND.keyframe) keyframes++; else other++;
          if (loss > 0 && fwdRng.next() < loss) { lost++; continue; }
          fwd.push({ at: tick + delay, d });
        }
      }
      while (fwd.length && fwd[0].at <= tick) {
        rx.on_datagram(fwd.shift()!.d);
        if (ackMode === 'server' && (rx.needs_ack() || tick - lastAck >= ackEvery)) sendAck(tick);
      }

      if (frame) {
        if (opts.gc ?? true) rx.gc(tick);
        const twin = new Map<number, Twin>();
        for (const e of JSON.parse(rx.extrapolate_json(tick)) as Twin[]) twin.set(e.id, e);
        for (const t of frame.tracks) {
          gtRows++;
          const e = twin.get(t.id);
          if (!e) { missing++; errs.push(penalty); continue; }
          const d = Math.hypot(e.pos[0] - t.pos[0], e.pos[1] - t.pos[1], e.pos[2] - t.pos[2]);
          errs.push(d); present.push(d);
          twin.delete(t.id);
        }
        phantom += twin.size;
        for (const id of twin.keys()) if (!phantomSince.has(id)) phantomSince.set(id, tick);
        for (const [id, since] of phantomSince) {
          if (!twin.has(id)) phantomSince.delete(id);
          else phantomMax = Math.max(phantomMax, tick - since + step);
        }
      }

      if (ackMode === 'gap' && tick % ackEvery === 0 && rx.needs_ack()) sendAck(tick);
    }
    const es = JSON.parse(edge.stats_json()) as { updates: number };
    const rs = JSON.parse(rx.stats_json()) as { gapsDetected: number; nacksSent: number; outOfOrderDropped: number };
    const durationS = frames.length ? (last - first + step) / TICK_HZ : 0;
    const wireBytes = payload + datagrams * HEADER_BYTES;
    const bytesPerSec = durationS > 0 ? wireBytes / durationS : 0;
    const ent = entityStats(frames);
    const mean = (xs: number[]) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : 0);
    return {
      thetaPos, thetaVel, loss, ackLoss, delayTicks: delay, ackMode, seed,
      durationS, frames: frames.length, frameHz: TICK_HZ / step, gtRows, entitiesMean: ent.mean, entitiesMax: ent.max,
      datagrams, deltas, keyframes, otherDatagrams: other, updates: es.updates,
      payloadBytes: payload, wireBytes, bytesPerSec, kbps: (bytesPerSec * 8) / 1000,
      lostDatagrams: lost,
      acksSent, acksDelivered, ackWireBytes: ackBytes,
      receiver: { gapsDetected: rs.gapsDetected, nacksSent: rs.nacksSent, outOfOrderDropped: rs.outOfOrderDropped },
      errMean: mean(errs), errP95: quantile(errs, 0.95), errMax: errs.length ? Math.max(...errs) : 0,
      errMeanPresent: mean(present), errP95Present: quantile(present, 0.95), errMaxPresent: present.length ? Math.max(...present) : 0,
      missingRows: missing, availability: gtRows ? (gtRows - missing) / gtRows : 1, phantomRows: phantom, phantomMaxS: phantomMax / TICK_HZ,
      missingPenaltyM: penalty,
    };
  } finally {
    edge.free();
    rx.free();
  }
}

export function replayRows(rows: GtRow[], opts: ReplayOptions): ReplayResult {
  const { frames, step } = toFrames(rows);
  return replayFrames(frames, step, opts);
}

export function replayFile(path: string, opts: ReplayOptions): ReplayResult {
  return replayRows(readGt(path), opts);
}

const f = (v: number, d = 3) => v.toFixed(d);
export function oneLine(name: string, r: ReplayResult): string {
  return `${name}: theta ${f(r.thetaPos)}/${f(r.thetaVel)} loss ${f(r.loss, 2)} delay ${r.delayTicks}t ack ${r.ackMode} | `
    + `${f(r.bytesPerSec, 1)} B/s (${f(r.kbps, 2)} kbps) ${r.datagrams} dgrams (${r.deltas} delta, ${r.keyframes} kf, ${r.lostDatagrams} lost) ${r.updates} updates | `
    + `err mean ${f(r.errMean)} p95 ${f(r.errP95)} max ${f(r.errMax)} m (present-only mean ${f(r.errMeanPresent)}) | `
    + `avail ${f(r.availability * 100, 2)}% missing ${r.missingRows} phantom ${r.phantomRows} (max ${f(r.phantomMaxS, 1)} s) | ${r.entitiesMean.toFixed(1)} ent, ${f(r.durationS, 1)} s`;
}

if (isMain(import.meta.url)) {
  const { values, positionals } = parseArgs({
    allowPositionals: true,
    options: {
      theta: { type: 'string', default: '0.15' },
      'theta-vel': { type: 'string' },
      loss: { type: 'string', default: '0' },
      'ack-loss': { type: 'string' },
      delay: { type: 'string', default: '0' },
      seed: { type: 'string', default: '1' },
      ack: { type: 'string', default: 'gap' },
      penalty: { type: 'string', default: '2.0' },
      budget: { type: 'string', default: '0' },
      json: { type: 'boolean', default: false },
    },
  });
  if (!positionals.length) {
    console.error('usage: npm run replay -- <gt.csv> [--theta 0.15] [--theta-vel 0.3] [--loss 0.2] [--ack-loss 0.2] [--delay 6] [--seed 1] [--ack gap|server|none] [--penalty 2.0] [--budget 0] [--json]');
    process.exit(2);
  }
  const ack = values.ack as AckMode;
  if (!['gap', 'server', 'none'].includes(ack)) throw new Error(`--ack must be gap, server or none`);
  const opts: ReplayOptions = {
    thetaPos: Number(values.theta),
    thetaVel: values['theta-vel'] !== undefined ? Number(values['theta-vel']) : undefined,
    loss: Number(values.loss),
    ackLoss: values['ack-loss'] !== undefined ? Number(values['ack-loss']) : undefined,
    delayTicks: Number(values.delay), seed: Number(values.seed), ackMode: ack,
    missingPenaltyM: Number(values.penalty), budgetBps: Number(values.budget),
  };
  for (const p of positionals) {
    const r = replayFile(userPath(p), opts);
    console.log(values.json ? JSON.stringify({ file: p, ...r }) : oneLine(basename(p, '.csv'), r));
  }
}
