// Baseline A: H.264 reference bitrates. Configured numbers until the phone's VideoToolbox
// measurement lands in runs/baseline_a.json; same file format and merge rule as
// tools/eval/src/baselines.ts `loadBaselineA`, so the viewer and the eval charts agree:
//   { "entries": [{ "id": "h264_720p", "bps": 1234567, "label"?, "resolution"?, "source"? }] }
// File entries replace the configured entry with the same id (measured: true); new ids are appended.
// The file is re-read when its mtime or size changes (checked at most every RECHECK_MS), so a
// measurement dropped in while the server runs shows up without a restart.
import { readFileSync, statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join, resolve } from 'node:path';
import type { BaselineAEntry } from './types.js';

export const CONFIGURED_SOURCE = 'configured, to be replaced by measured VideoToolbox numbers';
export const BASELINE_A_CONFIGURED: readonly BaselineAEntry[] = [
  { id: 'h264_720p', label: 'H.264 720p', bps: 1_500_000, measured: false, source: CONFIGURED_SOURCE },
  { id: 'h264_480p', label: 'H.264 480p', bps: 500_000, measured: false, source: CONFIGURED_SOURCE },
  { id: 'h264_360p', label: 'H.264 360p', bps: 250_000, measured: false, source: CONFIGURED_SOURCE },
];
const RECHECK_MS = 2_000;
const REPO = fileURLToPath(new URL('../../', import.meta.url));

/** MINBAND_BASELINE_A, else baseline_a.json in MINBAND_RUNS (as tools/eval), else <repo>/runs. */
export function defaultBaselineAFile(env: NodeJS.ProcessEnv = process.env): string {
  if (env.MINBAND_BASELINE_A) return resolve(env.MINBAND_BASELINE_A);
  return join(env.MINBAND_RUNS ? resolve(env.MINBAND_RUNS) : join(REPO, 'runs'), 'baseline_a.json');
}

/** Configured table merged with `json` (the file's text). Throws on a malformed file, like tools/eval. */
export function mergeBaselineA(json: string, file: string): BaselineAEntry[] {
  const table = BASELINE_A_CONFIGURED.map(b => ({ ...b }));
  const data = JSON.parse(json) as { entries?: Partial<BaselineAEntry>[] };
  for (const e of data.entries ?? []) {
    if (!e.id || !(typeof e.bps === 'number' && e.bps > 0)) throw new Error(`${file}: each entry needs an id and a positive bps`);
    const m: BaselineAEntry = { id: e.id, label: e.label ?? e.id, bps: e.bps, measured: true, source: e.source ?? `measured (${file})` };
    const i = table.findIndex(b => b.id === e.id);
    if (i >= 0) table[i] = { ...m, label: e.label ?? table[i].label };
    else table.push(m);
  }
  return table;
}

export class BaselineA {
  table: BaselineAEntry[] = BASELINE_A_CONFIGURED.map(b => ({ ...b }));
  /** Why the file was not used (malformed), null when fine or absent. */
  error: string | null = null;
  private stamp: string | null = null;
  private checkedMs = -Infinity;
  constructor(readonly file = defaultBaselineAFile(), private readonly now: () => number = Date.now) {}

  /** The current table; re-reads the file if it changed (`force`: check now, else at most every 2 s). */
  get(force = false): BaselineAEntry[] {
    const t = this.now();
    if (!force && t - this.checkedMs < RECHECK_MS) return this.table;
    this.checkedMs = t;
    let stamp = 'absent';
    try { const s = statSync(this.file); stamp = `${s.mtimeMs}:${s.size}`; } catch { /* absent */ }
    if (stamp === this.stamp) return this.table;
    this.stamp = stamp; this.error = null;
    try {
      this.table = stamp === 'absent' ? BASELINE_A_CONFIGURED.map(b => ({ ...b })) : mergeBaselineA(readFileSync(this.file, 'utf8'), this.file);
    } catch (e) {
      this.error = String((e as Error).message ?? e); // half-written or malformed: configured numbers until it is fixed
      this.table = BASELINE_A_CONFIGURED.map(b => ({ ...b }));
    }
    return this.table;
  }

  /** The legacy `Snapshot.baselines` pair (the current viewer reads it). */
  legacy(): { h264_720p_bps: number; h264_480p_bps: number } {
    const bps = (id: string) => (this.table.find(b => b.id === id) ?? BASELINE_A_CONFIGURED.find(b => b.id === id)!).bps;
    return { h264_720p_bps: bps('h264_720p'), h264_480p_bps: bps('h264_480p') };
  }
}
