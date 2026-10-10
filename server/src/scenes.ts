// Scenes for the synthetic edge (src/sim.ts). A scene is called once per edge tick, in order, and
// returns the tracks the edge sees at that tick.
//   shared: every device sees the same walkers with small per-device noise (fusion demo).
//   spread: device d sees its own walkers in its own area, so N devices are N independent feeds
//           (drones per link). Walkers follow the eval's one_walker tour (tools/eval/src/synth.ts),
//           so one walker per device costs about what the eval measured (~118 B/s on the wire).
// Each device also has a camera path (`sharedCamera`, `spreadCamera`) for its Pose: the viewer
// draws it as the device's frustum.
import { readFileSync } from 'node:fs';
import { TICK_HZ } from './types.js';

export interface Track { id: number; class: number; pos: number[]; vel: number[]; conf: number }
export type Scene = (tick: number) => Track[];
export const SCENES = ['shared', 'spread', 'log'] as const;
export type SceneName = typeof SCENES[number];
/** Distance between the centres of two devices' areas in `spread`, metres (the tour is ~6 x 4.5 m). */
export const SPREAD_M = 10;

export function sharedScene(deviceId: number): Scene {
  // Two walkers on loops, one static chair, one object that appears periodically. Each device
  // sees the same world with small observation noise and a per-device id space.
  return tick => {
    const t = tick / TICK_HZ;
    const n = (k: number) => (Math.sin(t * 7.3 + k * 13.1 + deviceId) * 0.02);
    // Walker 2 paces back and forth along x in [-4, 4] (it used to teleport from +4 to -4, an
    // 8 m jump no tracker reports, which dominated the twin error under loss).
    const u = (t * 0.8) % 16, out = u < 8;
    const tracks = [
      { id: 1, class: 0, pos: [3 * Math.cos(t * 0.4), 0, 3 * Math.sin(t * 0.4)], vel: [-1.2 * Math.sin(t * 0.4), 0, 1.2 * Math.cos(t * 0.4)], conf: 230 },
      { id: 2, class: 0, pos: [out ? u - 4 : 12 - u, 0, 2], vel: [out ? 0.8 : -0.8, 0, 0], conf: 200 },
      { id: 3, class: 56, pos: [-2, 0, -2], vel: [0, 0, 0], conf: 180 },
    ];
    if (Math.floor(t / 5) % 2 === 0) tracks.push({ id: 4, class: 41, pos: [1, 0.8, -1 + 0.3 * Math.sin(t)], vel: [0, 0, 0.3 * Math.cos(t)], conf: 150 });
    return tracks.map(tr => ({ ...tr, pos: tr.pos.map((v, i) => v + n(tr.id + i)) }));
  };
}

interface Waypoint { x: number; z: number; pause?: number }
const TOUR: Waypoint[] = [{ x: 3, z: -2 }, { x: 3, z: 2, pause: 2 }, { x: -1, z: 2.5 }, { x: -3, z: 0 }, { x: -3, z: -2 }];
const PERSON_Y = 0.9;

/** Pedestrian steering towards looping waypoints (port of tools/eval walker(): bounded turn rate
 * and acceleration, slows for sharp turns, smooth stop at pause waypoints). One tick per call. */
function walker(start: [number, number], wps: Waypoint[], cruise: number, accel = 1.0, turnRate = 2.0) {
  const dt = 1 / TICK_HZ;
  let x = start[0], z = start[1], speed = 0, wi = 0, pause = 0;
  let h = Math.atan2(wps[0].z - z, wps[0].x - x);
  return () => {
    if (pause > 0) {
      pause -= dt; speed = 0;
      if (pause <= 0) h = Math.atan2(wps[wi].z - z, wps[wi].x - x);
      return { x, z, vx: 0, vz: 0 };
    }
    const wp = wps[wi];
    const dx = wp.x - x, dz = wp.z - z, dist = Math.hypot(dx, dz);
    let diff = Math.atan2(dz, dx) - h;
    while (diff > Math.PI) diff -= 2 * Math.PI;
    while (diff < -Math.PI) diff += 2 * Math.PI;
    h += Math.max(-turnRate * dt, Math.min(turnRate * dt, diff));
    let want = cruise * Math.max(0.35, Math.cos(Math.min(Math.abs(diff), Math.PI / 2)));
    if (wp.pause) want = Math.min(want, Math.sqrt(2 * accel * Math.max(0, dist - 0.02)));
    speed += Math.max(-accel * dt, Math.min(accel * dt, want - speed));
    const vx = Math.cos(h) * speed, vz = Math.sin(h) * speed;
    x += vx * dt; z += vz * dt;
    const arrived = wp.pause ? dist < 0.03 || (dist < 0.3 && speed < 0.03) : dist < 0.35;
    if (arrived) {
      wi = (wi + 1) % wps.length;
      if (wp.pause) { pause = wp.pause; speed = 0; return { x, z, vx: 0, vz: 0 }; }
    }
    return { x, z, vx, vz };
  };
}

/** Centre of device `d`'s area among `n`, on a square-ish grid around the marker. */
export function spreadCentre(d: number, n: number): [number, number] {
  const cols = Math.ceil(Math.sqrt(n)), rows = Math.ceil(n / cols);
  return [(d % cols - (cols - 1) / 2) * SPREAD_M, (Math.floor(d / cols) - (rows - 1) / 2) * SPREAD_M];
}

/** Camera pose in the marker frame: position (m) and unit quaternion [x, y, z, w]. */
export interface CameraPose { pos: [number, number, number]; quat: [number, number, number, number] }
export type CameraPath = (tick: number) => CameraPose;

/** Quaternion of a camera at `eye` looking at `target` (-Z forward, +Y up, as ARKit and the viewer's frustum). */
export function lookAt(eye: number[], target: number[]): [number, number, number, number] {
  const norm = (v: number[]) => { const l = Math.hypot(v[0], v[1], v[2]); return v.map(x => x / l); };
  const cross = (a: number[], b: number[]) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
  const z = norm([eye[0] - target[0], eye[1] - target[1], eye[2] - target[2]]); // camera +Z points away from the target
  const x = norm(cross([0, 1, 0], z)), y = cross(z, x);
  // Rotation matrix with columns x, y, z -> quaternion (Shepperd).
  const [m11, m12, m13, m21, m22, m23, m31, m32, m33] = [x[0], y[0], z[0], x[1], y[1], z[1], x[2], y[2], z[2]];
  const tr = m11 + m22 + m33;
  if (tr > 0) { const s = 0.5 / Math.sqrt(tr + 1); return [(m32 - m23) * s, (m13 - m31) * s, (m21 - m12) * s, 0.25 / s]; }
  if (m11 > m22 && m11 > m33) { const s = 2 * Math.sqrt(1 + m11 - m22 - m33); return [0.25 * s, (m12 + m21) / s, (m13 + m31) / s, (m32 - m23) / s]; }
  if (m22 > m33) { const s = 2 * Math.sqrt(1 + m22 - m11 - m33); return [(m12 + m21) / s, 0.25 * s, (m23 + m32) / s, (m13 - m31) / s]; }
  const s = 2 * Math.sqrt(1 + m33 - m11 - m22);
  return [(m13 + m31) / s, (m23 + m32) / s, 0.25 * s, (m21 - m12) / s];
}

/** shared: device d of n walks a 6 m circle around the common scene at phone height, one lap per
 * 2 min, starting from its own bearing, always looking at the scene. */
export function sharedCamera(d: number, n: number): CameraPath {
  return tick => {
    const a = 2 * Math.PI * (d / n + 1 / 8) + tick / TICK_HZ * 2 * Math.PI / 120;
    const pos: [number, number, number] = [6 * Math.cos(a), 1.6, 6 * Math.sin(a)];
    return { pos, quat: lookAt(pos, [0, 0.5, 0]) };
  };
}

/** spread: a drone over device d's area, 6 m up on a 4 m orbit around the area centre, one lap per
 * 90 s, looking at the area (where its walkers are). */
export function spreadCamera(d: number, n: number): CameraPath {
  const [cx, cz] = spreadCentre(d, n);
  return tick => {
    const a = 2 * Math.PI * d / n + tick / TICK_HZ * 2 * Math.PI / 90;
    const pos: [number, number, number] = [cx + 4 * Math.cos(a), 6, cz + 4 * Math.sin(a)];
    return { pos, quat: lookAt(pos, [cx, PERSON_Y, cz]) };
  };
}

/** Device `d` of `n`: `walkers` people on the tour in its own area. Walker k's tour is turned by
 * k x 90 degrees, and each device starts at a different waypoint so the feeds are not in lockstep. */
export function spreadScene(d: number, n: number, walkers = 1): Scene {
  const [cx, cz] = spreadCentre(d, n);
  const ws = Array.from({ length: walkers }, (_, k) => {
    const c = Math.cos(k * Math.PI / 2), s = Math.sin(k * Math.PI / 2);
    const turned = TOUR.map(w => ({ ...w, x: cx + c * w.x - s * w.z, z: cz + s * w.x + c * w.z }));
    const i = (d + k) % turned.length;
    const order = [...turned.slice(i), ...turned.slice(0, i)];
    const from = order[order.length - 1]; // like the eval: start at the waypoint before the first target
    return walker([from.x, from.z], order, 1.2);
  });
  return () => ws.map((w, k) => {
    const p = w();
    return { id: k + 1, class: 0, pos: [p.x, PERSON_Y, p.z], vel: [p.vx, 0, p.vz], conf: 230 - 10 * k };
  });
}

/** log: replay a track log (`tick,id,class,x,y,z,vx,vy,vz,conf`, the phone's GroundTruthLog CSV or
 * tools/footage/track.py on real drone footage) as the tracker output, looping. Each loop gets a
 * fresh id range, so the edge despawns the old entities instead of seeing them jump. */
export function logScene(path: string): { scene: Scene; durationTicks: number } {
  const frames = new Map<number, Track[]>();
  for (const line of readFileSync(path, 'utf8').split(/\r?\n/)) {
    if (!line || line.startsWith('tick') || line.startsWith('#')) continue;
    const f = line.split(',').map(Number);
    if (f.length < 10 || f.some(v => !Number.isFinite(v))) continue;
    const t = Math.round(f[0]);
    let list = frames.get(t); if (!list) frames.set(t, list = []);
    list.push({ id: f[1], class: f[2], pos: [f[3], f[4], f[5]], vel: [f[6], f[7], f[8]], conf: Math.max(0, Math.min(255, Math.round(f[9]))) });
  }
  const ticks = [...frames.keys()].sort((a, b) => a - b);
  if (!ticks.length) throw new Error(`${path}: no rows`);
  const t0 = ticks[0], span = ticks[ticks.length - 1] - t0 + 1;
  let i = 0, lastLoop = -1;
  return {
    durationTicks: span,
    scene: tick => {
      const loop = Math.floor(tick / span), rel = t0 + (tick % span);
      if (loop !== lastLoop) { i = 0; lastLoop = loop; }
      while (i + 1 < ticks.length && ticks[i + 1] <= rel) i++;
      return (frames.get(ticks[i]) ?? []).map(tr => ({ ...tr, id: tr.id + loop * 100_000 }));
    },
  };
}

/** log: a fixed camera (the drone that filmed the log), looking at the log's origin. */
export function fixedCamera(pos: [number, number, number]): CameraPath {
  const quat = lookAt(pos, [0, 0, 0]);
  return () => ({ pos, quat });
}
