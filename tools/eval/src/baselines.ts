// Baselines the MinBand numbers are compared with (DESIGN 7).
//   Baseline A: H.264 video. Until the phone measures it, these are CONFIGURED reference
//               bitrates; drop measured numbers into runs/baseline_a.json (README) to replace them.
//   Baseline B: naive metadata, the full state of every entity every frame at 30 Hz:
//               entities * 31 B * 30 Hz * 8 + 30 Hz * 40 B * 8   bits/s
//               (31 B per EntityState update, 40 B per message incl. 28 B UDP/IP; same formula
//               as server/src/world.ts), with `entities` the time-averaged count in the log.
//   Baseline C: AI thumbnail (HACKATHON_PLAN 6.1 S23). At a few hundred B/s the realistic
//               competitor is not video but a periodic thumbnail: one 150 B chip, plus 28 B
//               UDP/IP, every N = (150 + 28) * 8 / R seconds at a wire rate of R bits/s.
//               Computed per scenario at MinBand's own wire rate at the default theta_pos (equal
//               bytes), and at each Pi link box profile rate (hf 9600, lora 2000, telemetry 600).
//   npm run baselines                  # from runs/synth/*.csv
//   npm run baselines -- --gt <csv> [--baseline-a runs/baseline_a.json]
import { parseArgs } from 'node:util';
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { entityStats } from './gt.ts';
import { HEADER_BYTES, replayFrames } from './replay.ts';
import { DEFAULT_THETA, loadInputs, synthInputs, type Input } from './sweep.ts';
import { writeCsv, type Row } from './csv.ts';
import { BASELINE_A_FILE, EVAL_DIR, SYNTH_DIR, isMain, userPath } from './paths.ts';

export const CONFIGURED_SOURCE = 'configured, to be replaced by measured VideoToolbox numbers';

export interface BaselineA { id: string; label: string; resolution: string; bps: number; source: string; measured: boolean }

export const BASELINE_A_CONFIGURED: BaselineA[] = [
  { id: 'h264_720p', label: 'H.264 720p', resolution: '1280x720', bps: 1_500_000, source: CONFIGURED_SOURCE, measured: false },
  { id: 'h264_480p', label: 'H.264 480p', resolution: '854x480', bps: 500_000, source: CONFIGURED_SOURCE, measured: false },
  { id: 'h264_360p', label: 'H.264 360p', resolution: '640x360', bps: 250_000, source: CONFIGURED_SOURCE, measured: false },
];

/**
 * Configured table, with any entries from the measured file replacing those with the same id.
 * File shape: { "entries": [{ "id": "h264_720p", "bps": 1234567, "label"?, "resolution"?, "source"? }] }
 * where bps is the measured average bitrate (bytes of the encoded file * 8 / duration).
 */
export function loadBaselineA(file = BASELINE_A_FILE): BaselineA[] {
  const table = BASELINE_A_CONFIGURED.map(b => ({ ...b }));
  if (!existsSync(file)) return table;
  const data = JSON.parse(readFileSync(file, 'utf8')) as { entries?: Partial<BaselineA>[] };
  for (const e of data.entries ?? []) {
    if (!e.id || !(typeof e.bps === 'number' && e.bps > 0)) throw new Error(`${file}: each entry needs an id and a positive bps`);
    const m: BaselineA = {
      id: e.id, label: e.label ?? e.id, resolution: e.resolution ?? '', bps: e.bps,
      source: e.source ?? `measured (${file})`, measured: true,
    };
    const i = table.findIndex(b => b.id === e.id);
    if (i >= 0) table[i] = { ...table[i], ...m, label: e.label ?? table[i].label, resolution: e.resolution ?? table[i].resolution };
    else table.push(m);
  }
  return table;
}

export const NAIVE = { bytesPerEntity: 31, hz: 30, bytesPerMessage: 40 };

export function baselineBbps(entities: number): number {
  return entities * NAIVE.bytesPerEntity * NAIVE.hz * 8 + NAIVE.hz * NAIVE.bytesPerMessage * 8;
}

export interface BaselineB { scenario: string; entitiesMean: number; entitiesMax: number; bps: number }

export function baselineB(inputs: Input[]): BaselineB[] {
  return inputs.map(i => {
    const e = entityStats(i.frames);
    return { scenario: i.name, entitiesMean: e.mean, entitiesMax: e.max, bps: baselineBbps(e.mean) };
  });
}

/** AI thumbnail chip size: a 32x32-class JPEG crop (MeshCore sends 100-200 B images over LoRa). */
export const THUMBNAIL_CHIP_BYTES = 150;

/** Link rates of the Pi link box profiles (HACKATHON_PLAN 3.3, tools/pi-link.sh), bits/s on the wire. */
export const LINK_PROFILES = [
  { id: 'hf', label: 'HF', bps: 9600 },
  { id: 'lora', label: 'LoRa', bps: 2000 },
  { id: 'telemetry', label: 'telemetry', bps: 600 },
];

/** Seconds between thumbnails at `bps` bits/s on the wire: (chip + 28 B UDP/IP) * 8 / bps. */
export function thumbnailIntervalS(bps: number, chipBytes = THUMBNAIL_CHIP_BYTES): number {
  return bps > 0 ? ((chipBytes + HEADER_BYTES) * 8) / bps : Infinity;
}

/** `scenario` set: equal bytes with MinBand on that log; empty: a link profile rate. */
export interface BaselineC { id: string; label: string; scenario: string; bps: number; chipBytes: number; intervalS: number }

/** MinBand's wire rate (bits/s) on a log at the default operating point: the sweep's default-theta row. */
export function minbandBps(i: Input): number {
  return replayFrames(i.frames, i.step, { thetaPos: DEFAULT_THETA, thetaVel: 2 * DEFAULT_THETA, loss: 0, delayTicks: 0 }).bytesPerSec * 8;
}

export function baselineC(inputs: Input[], chipBytes = THUMBNAIL_CHIP_BYTES): BaselineC[] {
  const row = (id: string, label: string, scenario: string, bps: number): BaselineC =>
    ({ id, label, scenario, bps, chipBytes, intervalS: thumbnailIntervalS(bps, chipBytes) });
  return [
    ...inputs.map(i => row('thumb_equal_bytes', `AI thumbnail ${chipBytes} B at MinBand's bytes`, i.name, minbandBps(i))),
    ...LINK_PROFILES.map(l => row(`thumb_${l.id}`, `AI thumbnail ${chipBytes} B on ${l.label}`, '', l.bps)),
  ];
}

export const BASELINE_COLUMNS = ['kind', 'id', 'label', 'scenario', 'resolution', 'entities_mean', 'entities_max', 'bps', 'kbps', 'bytes_per_s', 'chip_bytes', 'interval_s', 'measured', 'source'];

export function baselineRows(a: BaselineA[], b: BaselineB[], c: BaselineC[] = []): Row[] {
  return [
    ...a.map(x => ({ kind: 'A', id: x.id, label: x.label, scenario: '', resolution: x.resolution, entities_mean: '', entities_max: '', bps: x.bps, kbps: x.bps / 1000, bytes_per_s: x.bps / 8, measured: x.measured, source: x.source })),
    ...b.map(x => ({ kind: 'B', id: 'naive_30hz', label: 'naive metadata 30 Hz', scenario: x.scenario, resolution: '', entities_mean: x.entitiesMean, entities_max: x.entitiesMax, bps: x.bps, kbps: x.bps / 1000, bytes_per_s: x.bps / 8, measured: false, source: 'computed from the ground-truth log' })),
    ...c.map(x => ({
      kind: 'C', id: x.id, label: x.label, scenario: x.scenario, resolution: '', entities_mean: '', entities_max: '',
      bps: x.bps, kbps: x.bps / 1000, bytes_per_s: x.bps / 8, chip_bytes: x.chipBytes, interval_s: x.intervalS, measured: false,
      source: `${x.scenario ? `equal bytes: MinBand's wire rate at θ_pos ${DEFAULT_THETA} m` : 'Pi link box profile rate (tools/pi-link.sh)'}; N = (${x.chipBytes} + ${HEADER_BYTES}) B × 8 / rate`,
    })),
  ];
}

export function writeBaselines(inputs: Input[], outDir = EVAL_DIR, aFile = BASELINE_A_FILE): { a: BaselineA[]; b: BaselineB[]; c: BaselineC[] } {
  const a = loadBaselineA(aFile), b = baselineB(inputs), c = baselineC(inputs);
  writeCsv(join(outDir, 'baselines.csv'), BASELINE_COLUMNS, baselineRows(a, b, c));
  return { a, b, c };
}

if (isMain(import.meta.url)) {
  const { values } = parseArgs({ options: { gt: { type: 'string', multiple: true }, in: { type: 'string' }, out: { type: 'string' }, 'baseline-a': { type: 'string' } } });
  const paths = values.gt ? values.gt.map(userPath) : synthInputs(values.in ? userPath(values.in) : SYNTH_DIR);
  const { a, b, c } = writeBaselines(loadInputs(paths), values.out ? userPath(values.out) : EVAL_DIR, values['baseline-a'] ? userPath(values['baseline-a']) : BASELINE_A_FILE);
  console.log('Baseline A (H.264):');
  for (const x of a) console.log(`  ${x.label.padEnd(12)} ${(x.bps / 1000).toFixed(0).padStart(6)} kbps  ${x.measured ? x.source : `[${CONFIGURED_SOURCE}]`}`);
  console.log('Baseline B (naive 30 Hz metadata):');
  for (const x of b) console.log(`  ${x.scenario.padEnd(18)} ${x.entitiesMean.toFixed(2)} ent -> ${(x.bps / 1000).toFixed(2)} kbps (${(x.bps / 8).toFixed(0)} B/s)`);
  console.log(`Baseline C (AI thumbnail, one ${THUMBNAIL_CHIP_BYTES} B chip + ${HEADER_BYTES} B UDP/IP every N s):`);
  for (const x of c) console.log(`  ${(x.scenario ? `${x.scenario} (equal bytes)` : x.id.replace('thumb_', '') + ' link').padEnd(30)} ${(x.bps / 1000).toFixed(2).padStart(6)} kbps -> every ${x.intervalS.toFixed(2)} s`);
}
