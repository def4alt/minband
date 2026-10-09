// Ground-truth CSV, the format written by ios/MinBand/GroundTruthLog.swift:
//   tick,id,class,x,y,z,vx,vy,vz,conf
// tick is the edge clock (1/120 s since session start); one row per track per logged frame.
// The phone writes nothing for frames with no tracks, so a gap in ticks can mean "nothing seen".
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { dirname } from 'node:path';

export const TICK_HZ = 120;
export const GT_HEADER = 'tick,id,class,x,y,z,vx,vy,vz,conf';

export interface GtRow {
  tick: number; id: number; class: number;
  x: number; y: number; z: number;
  vx: number; vy: number; vz: number;
  conf: number;
}

/** Track as the WASM Edge expects it in `tick(tracks_json, now)`. */
export interface Track { id: number; class: number; pos: [number, number, number]; vel: [number, number, number]; conf: number }

export interface Frame { tick: number; tracks: Track[] }

export function parseGt(text: string): GtRow[] {
  const rows: GtRow[] = [];
  const lines = text.split(/\r?\n/);
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    if (!line || line.startsWith('#') || line.startsWith('tick')) continue;
    const f = line.split(',');
    if (f.length < 10) throw new Error(`gt line ${i + 1}: expected 10 columns, got ${f.length}`);
    const n = f.map(Number);
    if (n.slice(0, 10).some(v => !Number.isFinite(v))) throw new Error(`gt line ${i + 1}: non-numeric field in "${line}"`);
    rows.push({
      tick: Math.round(n[0]), id: Math.round(n[1]), class: Math.round(n[2]),
      x: n[3], y: n[4], z: n[5], vx: n[6], vy: n[7], vz: n[8],
      conf: Math.max(0, Math.min(255, Math.round(n[9]))),
    });
  }
  // Stable sort by tick (logs are append-only, but be tolerant of merged files).
  return rows.map((r, i) => [r, i] as const).sort((a, b) => a[0].tick - b[0].tick || a[1] - b[1]).map(p => p[0]);
}

export function readGt(path: string): GtRow[] {
  return parseGt(readFileSync(path, 'utf8'));
}

/** Fixed 5 decimals (10 um / 10 um/s), trailing zeros trimmed. */
export function fmtNum(v: number): string {
  const s = v.toFixed(5);
  const t = s.includes('.') ? s.replace(/0+$/, '').replace(/\.$/, '') : s;
  return t === '-0' ? '0' : t;
}

export function formatGt(rows: GtRow[]): string {
  const out: string[] = [GT_HEADER];
  for (const r of rows) {
    out.push(`${r.tick},${r.id},${r.class},${fmtNum(r.x)},${fmtNum(r.y)},${fmtNum(r.z)},${fmtNum(r.vx)},${fmtNum(r.vy)},${fmtNum(r.vz)},${r.conf}`);
  }
  return out.join('\n') + '\n';
}

export function writeGt(path: string, rows: GtRow[]): void {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, formatGt(rows));
}

export function rowToTrack(r: GtRow): Track {
  return { id: r.id, class: r.class, pos: [r.x, r.y, r.z], vel: [r.vx, r.vy, r.vz], conf: r.conf };
}

/** Median tick spacing between consecutive logged frames (4 for 30 Hz, 2 for 60 Hz). */
export function frameStep(ticks: number[]): number {
  const d: number[] = [];
  for (let i = 1; i < ticks.length; i++) if (ticks[i] > ticks[i - 1]) d.push(ticks[i] - ticks[i - 1]);
  if (!d.length) return 1;
  d.sort((a, b) => a - b);
  return Math.max(1, d[Math.floor(d.length / 2)]);
}

/**
 * Group rows into frames, one per distinct tick. Gaps longer than 1.5 frame periods are filled
 * with empty frames at the frame period: the phone logs nothing when it sees nothing, and the
 * edge must still be ticked with an empty track list so it emits despawns.
 */
export function toFrames(rows: GtRow[]): { frames: Frame[]; step: number } {
  const byTick = new Map<number, Track[]>();
  for (const r of rows) {
    let v = byTick.get(r.tick);
    if (!v) byTick.set(r.tick, v = []);
    v.push(rowToTrack(r));
  }
  const ticks = [...byTick.keys()].sort((a, b) => a - b);
  const step = frameStep(ticks);
  const frames: Frame[] = [];
  for (let i = 0; i < ticks.length; i++) {
    const t = ticks[i];
    if (i > 0) {
      const prev = ticks[i - 1];
      if (t - prev > 1.5 * step) for (let g = prev + step; g < t - step / 2; g += step) frames.push({ tick: g, tracks: [] });
    }
    frames.push({ tick: t, tracks: byTick.get(t)! });
  }
  return { frames, step };
}

/** Time-averaged and peak number of tracked entities over the logged span. */
export function entityStats(frames: Frame[]): { mean: number; max: number } {
  if (!frames.length) return { mean: 0, max: 0 };
  let sum = 0, max = 0;
  for (const f of frames) { sum += f.tracks.length; max = Math.max(max, f.tracks.length); }
  return { mean: sum / frames.length, max };
}
