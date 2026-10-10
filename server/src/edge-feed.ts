// Pure logic for edge-bridge.ts (P5a), split out so it's unit-testable without opening real
// sockets -- same split as scenes.ts (pure) / sim.ts (script) already in this codebase.
import type { Track } from './scenes.js';

/** edge.py's track datagram (one JSON object per UDP packet, see edge/edge.py's module
 * docstring): {"t", "tick", "tracks": [{"id", "cls", "pos":[x,y,z], "vel":[vx,vy,vz], "conf",
 * "age"}, ...]}. */
export interface EdgeTrackMsg { t: number; tick: number; tracks: { id: number; cls: number; pos: number[]; vel: number[]; conf: number }[] }
interface LiveTrack { track: Track; lastMs: number }

/** Drop a track if edge.py hasn't refreshed it in this long (detector cadence is ~1-2 Hz per
 * reports/CLIKA_SC1.md, so this must stay well above one detector period). */
export const STALE_MS = 4000;

export class EdgeFeed {
  private live = new Map<number, LiveTrack>();

  /** Parses one of edge.py's track datagrams and merges its tracks into the live set, keyed by
   * id so a newer datagram always overwrites an older one for the same track. Malformed JSON is
   * silently dropped (a live UDP feed, not a validated API). */
  ingest(raw: string, nowMs: number): void {
    let m: EdgeTrackMsg;
    try { m = JSON.parse(raw); } catch { return; }
    for (const tr of m.tracks ?? []) {
      this.live.set(tr.id, {
        track: { id: tr.id, class: tr.cls, pos: tr.pos, vel: tr.vel, conf: Math.max(0, Math.min(255, Math.round(tr.conf))) },
        lastMs: nowMs,
      });
    }
  }

  /** Live tracks, each extrapolated by its last known velocity to `nowMs` -- the detector only
   * refreshes a track every ~0.5-2 s, but the WASM edge ticks at 120 Hz, so between refreshes we
   * predict forward exactly as scenes.ts's `logScene` does between logged frames. Stale tracks are
   * dropped here so the edge despawns them. */
  currentTracks(nowMs: number): Track[] {
    const out: Track[] = [];
    for (const [id, lt] of this.live) {
      if (nowMs - lt.lastMs > STALE_MS) { this.live.delete(id); continue; }
      const dt = (nowMs - lt.lastMs) / 1000, p = lt.track.pos, v = lt.track.vel;
      out.push({ ...lt.track, pos: [p[0] + v[0] * dt, p[1] + v[1] * dt, p[2] + v[2] * dt] });
    }
    return out;
  }

  get size(): number { return this.live.size; }
}

/** core's datagrams come back from `tick()`/`pose()` length-prefixed (u16 LE) when multiple are
 * queued in one call -- same unpacking as sim.ts's `unpack`. */
export function unpack(buf: Uint8Array): Uint8Array[] {
  const out: Uint8Array[] = []; let i = 0;
  while (i + 2 <= buf.length) { const n = buf[i] | (buf[i + 1] << 8); i += 2; out.push(buf.subarray(i, i + n)); i += n; }
  return out;
}
