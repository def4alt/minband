// Moving / static classification of the fused entities, and the movers feed for an external 3D
// renderer (a reconstruction of the static scene that animates only what moves).
//
// Why a classifier at all: the twin holds everything the edge tracks, including a chair or a person
// standing still. A static-scene reconstruction already contains the static things; the renderer
// wants the actors. Kalman velocity alone flickers around zero on a standing person (tracker noise
// is ~10 cm), so the decision uses net displacement over a window with hysteresis:
//
//  - the position judged is robust: the component-wise median of the entity's positions over the
//    last ROBUST_MS, taken every ROBUST_STEP_MS. One lift flip (the phone places a person whose
//    legs went behind a desk a metre farther for one hit, 0.4 s of 30 Hz samples) does not move it.
//  - the displacement judged is relative to the scene. ARCore's pose ran 26 m in 9 s once, with a
//    walker filling the view, and everything in the room "moved" with it. So: if the entities
//    being observed (reported speed above SPEED_FLOOR) moved together the way the device did, the
//    scene is being dragged by a drifting pose and the device's displacement is subtracted.
//  - static -> moving: the robust position moved, from where it was MOVE_WINDOW_MS ago, at least
//    moveDist(class) across the ray from the device that sees it (bearing: the one thing a
//    monocular lift gets right whatever hid the feet), or along it by RANGE_DIST times the range
//    if that is more (the lift's range wanders with range: 2.4 m needed at 6 m); and it is moving
//    at least SPEED_FLOOR. Without the device position the displacement is judged whole. Speed alone
//    never does it: a track born from two noisy hits (a seated person placed by the width prior)
//    carries a finite-difference velocity that the edge corrects only slowly, while its position
//    goes nowhere. The speed floor and the class distances guard against lift flips too: a cup on
//    a table lands anywhere along its ray; such a step comes with no speed behind it, a walker's does.
//  - moving -> static: for the last STILL_MS the robust position never left a STILL_DIST ball
//    around its current one. A walker who stops stays a mover for STILL_MS, so the renderer does
//    not drop and re-spawn them at a pause.
//  - A new entity starts static and is not judged for its first MIN_AGE_MS: a track born from two
//    hits that never gets a third carries their finite-difference velocity, stops after the edge
//    tracker's coast time and dies; by then it is standing, so it never counts. A walking person
//    flips at ~1 s. A new entity born within INHERIT_DIST of a mover of its own class, alive or gone
//    less than GHOST_MS ago, is that mover under a new id (the edge lost them for a second) and
//    starts moving.
//
// Heading is the direction of travel in the marker frame (+Y up, +X right, -Z forward): 0 deg
// along -Z, 90 deg along +X, clockwise seen from above. It is held while the entity is slow, so a
// rendered figure keeps facing the way it last walked.
import type { FusedEntity } from './fusion.js';

export const MOVE_DIST = 0.4, MOVE_WINDOW_MS = 3000;
/** An entity faster than this is not let go static (the moving -> static rule). */
export const SPEED_MIN = 0.4;
/** Below this speed a displacement is a lift flip, not motion; a walker's filtered speed is well above it. */
export const SPEED_FLOOR = 0.2;
/** An entity is not called moving before this age. */
export const MIN_AGE_MS = 1000;
/** Along the ray the displacement needed is at least this fraction of the range from the device: a standing person's feet ray wanders +-0.4 m at 5 m between hits, a seated person's width-prior range swung 1.2-2.2 m at 1.7 m, and the first hit of a track is as likely off by that (the phone, which knows each lift's sigma, skips the along rule for such lifts; here 60 % of range). */
export const RANGE_DIST = 0.6;
/** How far the along-ray coordinate may step back within the window and still count as one-way motion (m). */
export const ALONG_SLACK = 0.15;
/** The robust position is the median of the raw positions over this long, refreshed this often. */
export const ROBUST_MS = 1000, ROBUST_STEP_MS = 500;
/** A new entity this close (ground, m) to a mover gone less than GHOST_MS ago starts moving. */
export const INHERIT_DIST = 1.0, GHOST_MS = 2000;
/** An entity absent from the update for up to this long keeps its state (a snapshot without it is not a new entity). */
export const KEEP_MS = 1000;
/** The scene counts as dragged by the pose when at least this many observed entities' median displacement is within DRAG_TOLERANCE of the device's, which moved at least DRAG_MIN_M over the window. */
export const DRAG_MIN_ENTITIES = 2, DRAG_MIN_M = 0.5, DRAG_TOLERANCE = 0.5;
const PLACED = new Set([56, 62, 63]);              // chair, tv, laptop
const CARRIED = new Set([24, 26, 39, 41, 67]);     // backpack, handbag, bottle, cup, cell phone
/** Displacement that makes an entity moving: placed objects need twice a person's, carried ones in between. */
export const moveDist = (cls: number) => (PLACED.has(cls) ? 1.0 : CARRIED.has(cls) ? 0.7 : MOVE_DIST);
export const STILL_DIST = 0.3, STILL_MS = 5000;
export const HEADING_SPEED = 0.3;
/** Dead reckoning for /api/movers is capped here: a request after a long gap gets the last snapshot. */
export const MAX_EXTRAPOLATE_MS = 1000;

type Vec3 = [number, number, number];
interface Sample { t: number; pos: Vec3; eye?: Vec3 | null }
interface State {
  cls: number;
  /** When the entity was last in the update; one that blinks for a snapshot keeps its state and tag. */
  lastSeen: number;
  /** Raw samples of the last ROBUST_MS. */
  hist: Sample[];
  /** Raw device positions of the last ROBUST_MS: their median is stored with each robust sample, so device and entity displacements lag alike. */
  eyes: Sample[];
  /** Medians of `hist`, every ROBUST_STEP_MS, the last STILL_MS. */
  robust: Sample[];
  lastRobust: number;
  /** First seen (ms). */
  since: number;
  moving: boolean;
  headingDeg: number | null;
}

const dist3 = (a: Vec3, b: Vec3) => Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]);

/** Component-wise median of the sample positions (the upper middle one for an even count). */
export function median(samples: readonly Sample[]): Vec3 { return medianOf(samples.map(s => s.pos)); }

export function medianOf(points: readonly Vec3[]): Vec3 {
  if (!points.length) return [0, 0, 0];
  const pick = (i: 0 | 1 | 2) => points.map(p => p[i]).sort((a, b) => a - b)[points.length >> 1];
  return [pick(0), pick(1), pick(2)];
}

/** Heading of a velocity in the marker frame (see file header); null below HEADING_SPEED. */
export function headingOf(vel: Vec3): number | null {
  if (Math.hypot(vel[0], vel[2]) < HEADING_SPEED) return null;
  const deg = (Math.atan2(vel[0], -vel[2]) * 180) / Math.PI;
  return (deg + 360) % 360;
}

const ground = (a: Vec3, b: Vec3) => Math.hypot(a[0] - b[0], a[2] - b[2]);

export class Motion {
  private readonly states = new Map<string, State>();
  /** Movers gone in the last GHOST_MS: class, when last seen and where. */
  private readonly gone: { cls: number; t: number; pos: Vec3 }[] = [];

  /** Same class only, and only from a mover that is gone: a live one beside a new entity is someone else (the seated neighbour the walker passes). */
  private inherits(pos: Vec3, cls: number, nowMs: number): boolean {
    // The last raw position, not the median: the median trails a walker by up to 1.5 s.
    for (const g of this.gone) if (g.cls === cls && nowMs - g.t <= GHOST_MS && ground(g.pos, pos) <= INHERIT_DIST) return true;
    return false;
  }

  /**
   * Classify every fused entity at `nowMs` (call once per snapshot, 30 Hz); forgets entities that
   * are gone. `eyeOf`: the position (marker frame) of the device that sees the entity, or null
   * when unknown; with it the displacement is split across / along the ray.
   */
  update<E extends FusedEntity>(entities: E[], nowMs: number, eyeOf?: (e: E) => Vec3 | null): (E & { moving: boolean; headingDeg: number | null })[] {
    const seen = new Set<string>();
    // Pass 1: histories, robust positions, each entity's displacement over the window.
    const judged = entities.map(e => {
      seen.add(e.gid);
      let s = this.states.get(e.gid);
      if (!s) { s = { cls: e.class, lastSeen: nowMs, hist: [], eyes: [], robust: [], lastRobust: -Infinity, since: nowMs, moving: this.inherits(e.pos, e.class, nowMs), headingDeg: null }; this.states.set(e.gid, s); }
      s.lastSeen = nowMs;
      s.hist.push({ t: nowMs, pos: e.pos });
      while (s.hist.length > 1 && s.hist[0].t < nowMs - ROBUST_MS) s.hist.shift();
      const eyeNow = eyeOf?.(e) ?? null;
      if (eyeNow) { s.eyes.push({ t: nowMs, pos: eyeNow }); while (s.eyes.length > 1 && s.eyes[0].t < nowMs - ROBUST_MS) s.eyes.shift(); } else s.eyes.length = 0;
      if (nowMs - s.lastRobust >= ROBUST_STEP_MS) {
        s.lastRobust = nowMs;
        s.robust.push({ t: nowMs, pos: median(s.hist), eye: s.eyes.length ? median(s.eyes) : null });
        const keepFrom = nowMs - Math.max(STILL_MS, MOVE_WINDOW_MS) - ROBUST_STEP_MS;
        while (s.robust.length > 1 && s.robust[0].t < keepFrom) s.robust.shift();
      }
      const cur = s.robust[s.robust.length - 1].pos;
      // The medians trail the raw positions by ~ROBUST_STEP_MS; the reference is older by as much.
      const ref = s.robust.find(h => h.t >= nowMs - MOVE_WINDOW_MS - ROBUST_STEP_MS) ?? s.robust[0];
      const d: Vec3 = [cur[0] - ref.pos[0], cur[1] - ref.pos[1], cur[2] - ref.pos[2]];
      const refT = ref.t, refPos = ref.pos;
      const curEye = s.robust[s.robust.length - 1].eye ?? null;
      const eyeDisp: Vec3 | null = curEye && ref.eye ? [curEye[0] - ref.eye[0], curEye[1] - ref.eye[1], curEye[2] - ref.eye[2]] : null;
      const observed = Math.hypot(e.vel[0], e.vel[1], e.vel[2]) >= SPEED_FLOOR;
      return { e, s, cur, d, eyeDisp, observed, refT, refPos };
    });
    // The scene's own displacement: only a pose drift, which drags the scene along with the
    // device, is corrected, by the device's own displacement. Only entities still being observed
    // can show it (a coasting track holds its place while a dragged scene moves on). A median of
    // the entities' own displacements was tried as the drift and made things worse: their
    // along-ray noise became everyone's.
    const observed = judged.filter(j => j.observed);
    const eyeDisp = observed.find(j => j.eyeDisp)?.eyeDisp ?? null;
    const dragged = !!eyeDisp && observed.length >= DRAG_MIN_ENTITIES && ground(eyeDisp, [0, 0, 0]) >= DRAG_MIN_M &&
      ground(medianOf(observed.map(j => j.d)), eyeDisp) <= DRAG_TOLERANCE * ground(eyeDisp, [0, 0, 0]) + 0.3;
    const drift: Vec3 = dragged ? eyeDisp! : [0, 0, 0];

    // Pass 2: decisions.
    const out = judged.map(({ e, s, cur, d: d0, refT, refPos }) => {
      const speed = Math.hypot(e.vel[0], e.vel[1], e.vel[2]);

      if (!s.moving) {
        if (nowMs - s.since >= MIN_AGE_MS) {
          const base = moveDist(e.class);
          const dx = d0[0] - drift[0], dz = d0[2] - drift[2];
          const eye = eyeOf?.(e) ?? null;
          const rx = eye ? cur[0] - eye[0] : 0, rz = eye ? cur[2] - eye[2] : 0, range = Math.hypot(rx, rz);
          let moved: boolean;
          if (eye && range > 1e-3) {
            const ux = rx / range, uz = rz / range;
            const along = dx * ux + dz * uz;
            const across = Math.hypot(dx - ux * along, dz - uz * along);
            // Placed and carried objects are judged across the ray only: a chair's range steps
            // when someone walks in front of it; a chair carried away crosses the view.
            // And only when the range changed one way over the window: a walker approaching
            // comes steadily closer, a lift flip goes and comes back.
            const window = s.robust.filter(h => h.t >= refT);
            const a = window.map(h => (h.pos[0] - refPos[0]) * ux + (h.pos[2] - refPos[2]) * uz);
            const dir = a[a.length - 1] >= 0 ? 1 : -1;
            let oneWay = true;
            for (let k = 1; k < a.length; k++) if ((a[k] - a[k - 1]) * dir < -ALONG_SLACK) { oneWay = false; break; }
            const alongCounts = !PLACED.has(e.class) && !CARRIED.has(e.class) && oneWay;
            moved = across >= base || (alongCounts && Math.abs(along) >= Math.max(base, RANGE_DIST * range));
          } else moved = Math.hypot(dx, d0[1] - drift[1], dz) >= base;
          if (moved && speed >= SPEED_FLOOR) s.moving = true;
        }
      } else {
        const oldest = s.robust[0];
        if (nowMs - oldest.t >= STILL_MS) {
          let left = false;
          for (const h of s.robust) if (h.t >= nowMs - STILL_MS && dist3(h.pos, cur) >= STILL_DIST) { left = true; break; }
          if (!left && speed < SPEED_MIN) s.moving = false;
        }
      }
      const h = headingOf(e.vel);
      if (h !== null) s.headingDeg = h;
      return { ...e, moving: s.moving, headingDeg: s.headingDeg };
    });
    for (const [gid, s] of [...this.states.entries()]) if (!seen.has(gid) && nowMs - s.lastSeen > KEEP_MS) {
      if (s.moving && s.hist.length) this.gone.push({ cls: s.cls, t: s.lastSeen, pos: s.hist[s.hist.length - 1].pos });
      this.states.delete(gid);
    }
    for (let i = this.gone.length - 1; i >= 0; i--) if (nowMs - this.gone[i].t > GHOST_MS) this.gone.splice(i, 1);
    return out;
  }

  /** Entities currently tracked by the classifier (tests, leak checks). */
  get size(): number { return this.states.size; }
}

/** One row of the movers feed. Positions in the marker frame, metres; `t` is the snapshot's server ms. */
export interface MoverRow {
  gid: string; class: number; name: string;
  pos: Vec3; vel: Vec3; speed: number; headingDeg: number | null;
  moving: boolean; ce: number; coasting: boolean; stale: boolean;
  sources: { deviceId: number; id: number }[];
}
export interface MoversFeed {
  /** Server ms the positions are valid for (snapshot time + the dead reckoning applied). */
  t: number;
  /** Age of the underlying snapshot in ms (how much dead reckoning was applied). */
  ageMs: number;
  frame: { origin: string; units: string; up: string; forward: string; right: string; heading: string };
  movers: MoverRow[];
}

export const FRAME = {
  origin: 'marker centre (ORIGIN HERE: camera dropped onto the floor)', units: 'm',
  up: '+Y', forward: '-Z', right: '+X', heading: 'deg, 0 along -Z, 90 along +X, clockwise from above',
};

/** The movers feed from the last snapshot, dead-reckoned to `nowMs` (at most MAX_EXTRAPOLATE_MS). */
export function moversFeed(
  snap: { t: number; global: (FusedEntity & { moving: boolean; headingDeg: number | null })[] } | null,
  nowMs: number, all: boolean, label: (c: number) => string,
): MoversFeed {
  if (!snap) return { t: nowMs, ageMs: 0, frame: FRAME, movers: [] };
  const dtMs = Math.max(0, Math.min(MAX_EXTRAPOLATE_MS, nowMs - snap.t)), dt = dtMs / 1000;
  const movers: MoverRow[] = [];
  for (const g of snap.global) {
    if (!all && !g.moving) continue;
    const pos: Vec3 = [g.pos[0] + g.vel[0] * dt, g.pos[1] + g.vel[1] * dt, g.pos[2] + g.vel[2] * dt];
    movers.push({
      gid: g.gid, class: g.class, name: label(g.class), pos, vel: g.vel,
      speed: Math.hypot(g.vel[0], g.vel[1], g.vel[2]), headingDeg: g.headingDeg,
      moving: g.moving, ce: g.ce, coasting: g.coasting, stale: g.stale, sources: g.sources,
    });
  }
  return { t: snap.t + dtMs, ageMs: dtMs, frame: FRAME, movers };
}
