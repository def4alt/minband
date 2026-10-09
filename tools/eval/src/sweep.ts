// Threshold and loss sweeps over ground-truth logs.
//   npm run sweep                                  # every runs/synth/*.csv
//   npm run sweep -- --gt runs/phone/gt-1712345.csv --gt runs/phone/gt-1712399.csv
// Writes runs/eval/fidelity_vs_bytes.csv and runs/eval/resilience.csv (columns: tools/README.md).
import { parseArgs } from 'node:util';
import { readdirSync, existsSync } from 'node:fs';
import { basename, join } from 'node:path';
import { readGt, toFrames, type Frame } from './gt.ts';
import { replayFrames, type AckMode, type ReplayResult } from './replay.ts';
import { writeCsv, type Row } from './csv.ts';
import { STANDARD } from './synth.ts';
import { EVAL_DIR, SYNTH_DIR, isMain, userPath } from './paths.ts';

/** 13 log-spaced points from 0.02 to 2.0 m (6 per decade), plus the default 0.15. */
export const THETAS: number[] = [...Array.from({ length: 13 }, (_, k) => Number((0.02 * 10 ** (k / 6)).toPrecision(3))), 0.15].sort((a, b) => a - b);
export const LOSSES = [0, 0.05, 0.2, 0.5];
export const DEFAULT_THETA = 0.15;
/** One-way delay used for the resilience sweep: 6 ticks = 50 ms (100 ms RTT). */
export const RESILIENCE_DELAY_TICKS = 6;
export const RESILIENCE_SEEDS = 10;

export interface Input { name: string; frames: Frame[]; step: number }

export function loadInputs(paths: string[]): Input[] {
  return paths.map(p => {
    const { frames, step } = toFrames(readGt(p));
    return { name: basename(p, '.csv'), frames, step };
  });
}

/** runs/synth/*.csv, standard scenarios first in their canonical order. */
export function synthInputs(dir = SYNTH_DIR): string[] {
  if (!existsSync(dir)) return [];
  const files = readdirSync(dir).filter(f => f.endsWith('.csv'));
  const order = STANDARD.map(s => `${s.name}.csv`);
  files.sort((a, b) => (order.indexOf(a) + 1 || 99) - (order.indexOf(b) + 1 || 99) || a.localeCompare(b));
  return files.map(f => join(dir, f));
}

export const FIDELITY_COLUMNS = [
  'scenario', 'theta_pos', 'theta_vel', 'loss', 'delay_ticks', 'duration_s', 'frame_hz', 'entities_mean',
  'datagrams', 'deltas', 'keyframes', 'updates', 'payload_bytes', 'wire_bytes', 'bytes_per_s', 'kbps',
  'err_mean_m', 'err_p95_m', 'err_max_m', 'err_mean_present_m', 'availability', 'missing_rows', 'phantom_rows', 'gt_rows',
];

export const RESILIENCE_COLUMNS = [
  'scenario', 'theta_pos', 'theta_vel', 'loss', 'repair', 'ack_mode', 'delay_ticks', 'seeds',
  'bytes_per_s', 'kbps', 'datagrams', 'lost_datagrams', 'updates', 'acks_sent', 'gaps_detected', 'nacks_sent',
  'err_mean_m', 'err_p95_m', 'err_max_m', 'err_mean_present_m', 'err_p95_present_m',
  'availability', 'missing_rows', 'phantom_rows', 'phantom_max_s', 'gt_rows',
];

function fidelityRow(name: string, r: ReplayResult): Row {
  return {
    scenario: name, theta_pos: r.thetaPos, theta_vel: r.thetaVel, loss: r.loss, delay_ticks: r.delayTicks,
    duration_s: r.durationS, frame_hz: r.frameHz, entities_mean: r.entitiesMean,
    datagrams: r.datagrams, deltas: r.deltas, keyframes: r.keyframes, updates: r.updates,
    payload_bytes: r.payloadBytes, wire_bytes: r.wireBytes, bytes_per_s: r.bytesPerSec, kbps: r.kbps,
    err_mean_m: r.errMean, err_p95_m: r.errP95, err_max_m: r.errMax, err_mean_present_m: r.errMeanPresent,
    availability: r.availability, missing_rows: r.missingRows, phantom_rows: r.phantomRows, gt_rows: r.gtRows,
  };
}

export function fidelitySweep(inputs: Input[], thetas = THETAS): Row[] {
  const rows: Row[] = [];
  for (const inp of inputs) {
    for (const theta of thetas) {
      rows.push(fidelityRow(inp.name, replayFrames(inp.frames, inp.step, { thetaPos: theta, thetaVel: 2 * theta, loss: 0, delayTicks: 0 })));
    }
  }
  return rows;
}

/**
 * Loss sweep at a fixed theta, averaged over seeds, with three ack policies: `on` (acks when the
 * receiver needs_ack(), checked every 12 ticks, as in the golden test), `off` (no acks: no state
 * repair, keyframes only) and `server` (the live server's cadence: after each datagram when
 * needs_ack() or 100 ms since the last ack).
 */
export function resilienceSweep(inputs: Input[], losses = LOSSES, theta = DEFAULT_THETA, delayTicks = RESILIENCE_DELAY_TICKS, seeds = RESILIENCE_SEEDS): Row[] {
  const rows: Row[] = [];
  const modes: [string, AckMode][] = [['on', 'gap'], ['off', 'none'], ['server', 'server']];
  for (const inp of inputs) {
    for (const loss of losses) {
      for (const [repair, ackMode] of modes) {
        const n = loss > 0 ? seeds : 1; // loss 0 is deterministic
        const rs: ReplayResult[] = [];
        for (let s = 1; s <= n; s++) rs.push(replayFrames(inp.frames, inp.step, { thetaPos: theta, thetaVel: 2 * theta, loss, delayTicks, seed: s, ackMode }));
        const avg = (k: (r: ReplayResult) => number) => rs.reduce((a, r) => a + k(r), 0) / rs.length;
        rows.push({
          scenario: inp.name, theta_pos: theta, theta_vel: 2 * theta, loss, repair, ack_mode: ackMode, delay_ticks: delayTicks, seeds: n,
          bytes_per_s: avg(r => r.bytesPerSec), kbps: avg(r => r.kbps), datagrams: avg(r => r.datagrams),
          lost_datagrams: avg(r => r.lostDatagrams), updates: avg(r => r.updates), acks_sent: avg(r => r.acksSent),
          gaps_detected: avg(r => r.receiver.gapsDetected), nacks_sent: avg(r => r.receiver.nacksSent),
          err_mean_m: avg(r => r.errMean), err_p95_m: avg(r => r.errP95), err_max_m: Math.max(...rs.map(r => r.errMax)),
          err_mean_present_m: avg(r => r.errMeanPresent), err_p95_present_m: avg(r => r.errP95Present),
          availability: avg(r => r.availability), missing_rows: avg(r => r.missingRows),
          phantom_rows: avg(r => r.phantomRows), phantom_max_s: Math.max(...rs.map(r => r.phantomMaxS)), gt_rows: rs[0].gtRows,
        });
      }
    }
  }
  return rows;
}

export function runSweeps(paths: string[], outDir = EVAL_DIR): { fidelity: Row[]; resilience: Row[] } {
  const inputs = loadInputs(paths);
  const fidelity = fidelitySweep(inputs);
  const resilience = resilienceSweep(inputs);
  writeCsv(join(outDir, 'fidelity_vs_bytes.csv'), FIDELITY_COLUMNS, fidelity);
  writeCsv(join(outDir, 'resilience.csv'), RESILIENCE_COLUMNS, resilience);
  return { fidelity, resilience };
}

if (isMain(import.meta.url)) {
  const { values } = parseArgs({ options: { gt: { type: 'string', multiple: true }, in: { type: 'string' }, out: { type: 'string' } } });
  const paths = values.gt ? values.gt.map(userPath) : synthInputs(values.in ? userPath(values.in) : SYNTH_DIR);
  if (!paths.length) { console.error('no ground-truth CSVs; run `npm run synth` or pass --gt <file>'); process.exit(1); }
  const out = values.out ? userPath(values.out) : EVAL_DIR;
  const t0 = performance.now();
  const { fidelity, resilience } = runSweeps(paths, out);
  console.log(`sweep: ${paths.length} logs, ${fidelity.length} fidelity rows, ${resilience.length} resilience rows -> ${out} (${((performance.now() - t0) / 1000).toFixed(1)} s)`);
}
