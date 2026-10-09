// Twin error against an uploaded ground-truth log.
//
// The world records, per device, the extrapolated entity positions it served at 30 Hz, labelled
// with the edge tick they were extrapolated to (last 60 s). A ground-truth CSV row
// `tick,id,class,x,y,z,vx,vy,vz,conf` (ticks of 1/120 s on the edge clock) is compared with the
// snapshot whose tick is closest: distance between positions, or MISSING_M if the twin had no
// entity with that id. Rows with no snapshot within MAX_TICK_GAP are outside the recording and
// are not counted.
import type { EntityView, TwinError } from './types.js';
import { TICK_HZ } from './types.js';

export const RING_SECONDS = 60;
export const MISSING_M = 2.0;
/** Two snapshot periods at 30 Hz. */
export const MAX_TICK_GAP = 8;

export interface GtRow { tick: number; id: number; class: number; pos: [number, number, number] }

export class SnapshotRing {
  readonly ticks: number[] = [];
  readonly frames: Map<number, [number, number, number]>[] = [];
  constructor(readonly spanTicks = RING_SECONDS * TICK_HZ) {}

  clear() { this.ticks.length = 0; this.frames.length = 0; }
  get size() { return this.ticks.length; }

  /** Record one snapshot. Ticks must increase; a non-increasing tick is ignored. */
  push(tick: number, entities: Pick<EntityView, 'id' | 'pos'>[]): boolean {
    const n = this.ticks.length;
    if (n && tick <= this.ticks[n - 1]) return false;
    this.ticks.push(tick);
    this.frames.push(new Map(entities.map(e => [e.id, [e.pos[0], e.pos[1], e.pos[2]]])));
    let drop = 0;
    while (drop < this.ticks.length - 1 && this.ticks[drop] < tick - this.spanTicks) drop++;
    if (drop) { this.ticks.splice(0, drop); this.frames.splice(0, drop); }
    return true;
  }

  /** Index of the snapshot closest to `tick`, or -1 if empty. */
  nearest(tick: number): number {
    const t = this.ticks; if (!t.length) return -1;
    let lo = 0, hi = t.length - 1;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (t[mid] < tick) lo = mid + 1; else hi = mid; }
    if (lo > 0 && Math.abs(t[lo - 1] - tick) <= Math.abs(t[lo] - tick)) lo--;
    return lo;
  }
}

export function parseGroundTruthCsv(csv: string): { rows: GtRow[]; malformed: number } {
  const rows: GtRow[] = []; let malformed = 0;
  for (const raw of csv.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) continue;
    const f = line.split(',');
    if (f[0].trim() === 'tick') continue; // header
    if (f.length < 6) { malformed++; continue; }
    const n = f.map(Number);
    const [tick, id, cls, x, y, z] = n;
    if (![tick, id, cls, x, y, z].every(Number.isFinite) || !Number.isInteger(tick) || !Number.isInteger(id)) { malformed++; continue; }
    rows.push({ tick, id, class: cls, pos: [x, y, z] });
  }
  return { rows, malformed };
}

export function summarize(dist: ArrayLike<number>): TwinError {
  const n = dist.length;
  if (!n) return { meanM: null, p95M: null, samples: 0 };
  const sorted = Float64Array.from(dist).sort();
  let sum = 0; for (let i = 0; i < n; i++) sum += sorted[i];
  return { meanM: sum / n, p95M: sorted[Math.max(0, Math.ceil(0.95 * n) - 1)], samples: n };
}

export interface TwinEvaluation extends TwinError {
  rows: number; skipped: number; missing: number; distances: Float64Array;
  window: [number, number] | null;
}

export function evaluateTwin(rows: GtRow[], ring: SnapshotRing): TwinEvaluation {
  const d: number[] = []; let skipped = 0, missing = 0;
  for (const r of rows) {
    const i = ring.nearest(r.tick);
    if (i < 0 || Math.abs(ring.ticks[i] - r.tick) > MAX_TICK_GAP) { skipped++; continue; }
    const p = ring.frames[i].get(r.id);
    if (!p) { missing++; d.push(MISSING_M); continue; }
    d.push(Math.hypot(p[0] - r.pos[0], p[1] - r.pos[1], p[2] - r.pos[2]));
  }
  const distances = Float64Array.from(d);
  const window: [number, number] | null = ring.size ? [ring.ticks[0], ring.ticks[ring.size - 1]] : null;
  return { ...summarize(distances), rows: rows.length, skipped, missing, distances, window };
}
