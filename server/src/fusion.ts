// Merge observations of the same physical object from different devices into one global entity.
// Hysteresis: merge when close for MERGE_MS, split when far for SPLIT_MS.
//
// Invariants:
//  - A group never holds two tracks from the same device (merges are refused, also transitively).
//  - Pair timers only live while both tracks are evaluated on consecutive updates; a pair that
//    is skipped (track vanished, fusion disabled) starts its hysteresis from scratch.
//  - Devices are identified by DeviceView.key, not deviceId (provisional devices share id 0).
import type { DeviceView, EntityView, GlobalEntity } from './types.js';

export const MERGE_DIST = 0.5, MERGE_VEL = 0.5, SPLIT_DIST = 1.0, MERGE_MS = 1000, SPLIT_MS = 1000;

type Key = string; // `${deviceKey}#${id}`
/** World.snapshot adds `geo` (geo.ts) after fusion. */
export type FusedEntity = Omit<GlobalEntity, 'geo'>;
const devKey = (d: DeviceView): string => d.key ?? `id:${d.deviceId}`;

export class Fusion {
  enabled = true;
  private groups = new Map<string, Set<Key>>(); // gid -> members
  private memberOf = new Map<Key, string>();
  private deviceOf = new Map<Key, string>();
  private closeSince = new Map<string, number>(); // pair key -> ms
  private farSince = new Map<string, number>();
  private nextGid = 1;

  update(devices: DeviceView[], nowMs: number): FusedEntity[] {
    const all: { k: Key; dk: string; d: number; e: EntityView }[] = [];
    for (const d of devices) {
      const dk = devKey(d);
      for (const e of d.entities) all.push({ k: `${dk}#${e.id}`, dk, d: d.deviceId, e });
    }
    const alive = new Set(all.map(a => a.k));

    // Remove dead tracks, then make sure every live track has a group.
    for (const k of [...this.memberOf.keys()]) if (!alive.has(k)) { this.leave(k); this.deviceOf.delete(k); }
    for (const a of all) { this.deviceOf.set(a.k, a.dk); if (!this.memberOf.has(a.k)) this.newGroup(a.k); }

    const touched = new Set<string>();
    if (this.enabled) {
      for (let i = 0; i < all.length; i++) for (let j = i + 1; j < all.length; j++) {
        const a = all[i], b = all[j];
        if (a.dk === b.dk) continue;
        const pk = a.k < b.k ? `${a.k}|${b.k}` : `${b.k}|${a.k}`;
        const same = this.memberOf.get(a.k) === this.memberOf.get(b.k);
        if (!same && a.e.class !== b.e.class) continue;
        touched.add(pk);
        const dist = Math.hypot(a.e.pos[0] - b.e.pos[0], a.e.pos[1] - b.e.pos[1], a.e.pos[2] - b.e.pos[2]);
        const dvel = Math.hypot(a.e.vel[0] - b.e.vel[0], a.e.vel[1] - b.e.vel[1], a.e.vel[2] - b.e.vel[2]);
        if (!same) {
          this.farSince.delete(pk);
          if (dist < MERGE_DIST && dvel < MERGE_VEL && this.canMerge(a.k, b.k)) {
            const since = this.closeSince.get(pk) ?? nowMs; this.closeSince.set(pk, since);
            if (nowMs - since >= MERGE_MS) { this.merge(a.k, b.k); this.closeSince.delete(pk); }
          } else this.closeSince.delete(pk);
        } else {
          this.closeSince.delete(pk);
          // A re-classified member counts as far: it is no longer the same object.
          if (dist > SPLIT_DIST || a.e.class !== b.e.class) {
            const since = this.farSince.get(pk) ?? nowMs; this.farSince.set(pk, since);
            if (nowMs - since >= SPLIT_MS) { this.leave(b.k); this.newGroup(b.k); this.farSince.delete(pk); }
          } else this.farSince.delete(pk);
        }
      }
    } else {
      for (const a of all) if ((this.groups.get(this.memberOf.get(a.k)!)?.size ?? 1) > 1) { this.leave(a.k); this.newGroup(a.k); }
    }
    for (const pk of [...this.closeSince.keys()]) if (!touched.has(pk)) this.closeSince.delete(pk);
    for (const pk of [...this.farSince.keys()]) if (!touched.has(pk)) this.farSince.delete(pk);

    const byKey = new Map(all.map(a => [a.k, a]));
    const out: FusedEntity[] = [];
    for (const [gid, members] of this.groups) {
      const ms = [...members].map(k => byKey.get(k)!).filter(Boolean);
      if (!ms.length) continue;
      let w = 0; const pos = [0, 0, 0], vel = [0, 0, 0];
      for (const m of ms) { const c = m.e.stale ? 1 : m.e.conf + 1; w += c; for (let i = 0; i < 3; i++) { pos[i] += m.e.pos[i] * c; vel[i] += m.e.vel[i] * c; } }
      out.push({
        gid, class: ms[0].e.class,
        pos: [pos[0] / w, pos[1] / w, pos[2] / w], vel: [vel[0] / w, vel[1] / w, vel[2] / w],
        sources: ms.map(m => ({ deviceId: m.d, id: m.e.id })), stale: ms.every(m => m.e.stale),
      });
    }
    return out;
  }

  /** Number of pending hysteresis timers (for tests / leak checks). */
  get pendingTimers(): number { return this.closeSince.size + this.farSince.size; }

  private devicesOf(gid: string): Set<string> {
    const s = new Set<string>();
    for (const k of this.groups.get(gid) ?? []) s.add(this.deviceOf.get(k)!);
    return s;
  }

  private canMerge(a: Key, b: Key): boolean {
    const ga = this.memberOf.get(a)!, gb = this.memberOf.get(b)!;
    if (ga === gb) return false;
    const da = this.devicesOf(ga);
    for (const d of this.devicesOf(gb)) if (da.has(d)) return false;
    return true;
  }

  private newGroup(k: Key) { const gid = `g${this.nextGid++}`; this.groups.set(gid, new Set([k])); this.memberOf.set(k, gid); }
  private leave(k: Key) { const gid = this.memberOf.get(k); if (!gid) return; const g = this.groups.get(gid); g?.delete(k); if (g && g.size === 0) this.groups.delete(gid); this.memberOf.delete(k); }
  private merge(a: Key, b: Key) { const ga = this.memberOf.get(a)!, gb = this.memberOf.get(b)!; if (ga === gb) return; for (const k of this.groups.get(gb)!) { this.groups.get(ga)!.add(k); this.memberOf.set(k, ga); } this.groups.delete(gb); }
}
