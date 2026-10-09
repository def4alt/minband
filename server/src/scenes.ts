// Scenes for the synthetic edge (src/sim.ts). A scene is called once per edge tick, in order, and
// returns the tracks the edge sees at that tick.
//   shared: every device sees the same walkers with small per-device noise (fusion demo).
//   spread: device d sees its own walkers in its own area, so N devices are N independent feeds
//           (drones per link). Walkers follow the eval's one_walker tour (tools/eval/src/synth.ts),
//           so one walker per device costs about what the eval measured (~118 B/s on the wire).
import { TICK_HZ } from './types.js';

export interface Track { id: number; class: number; pos: number[]; vel: number[]; conf: number }
export type Scene = (tick: number) => Track[];
export const SCENES = ['shared', 'spread'] as const;
export type SceneName = typeof SCENES[number];
/** Distance between the centres of two devices' areas in `spread`, metres (the tour is ~6 x 4.5 m). */
export const SPREAD_M = 10;

export function sharedScene(deviceId: number): Scene {
  // Two walkers on loops, one static chair, one object that appears periodically. Each device
  // sees the same world with small observation noise and a per-device id space.
  return tick => {
    const t = tick / TICK_HZ;
    const n = (k: number) => (Math.sin(t * 7.3 + k * 13.1 + deviceId) * 0.02);
    const tracks = [
      { id: 1, class: 0, pos: [3 * Math.cos(t * 0.4), 0, 3 * Math.sin(t * 0.4)], vel: [-1.2 * Math.sin(t * 0.4), 0, 1.2 * Math.cos(t * 0.4)], conf: 230 },
      { id: 2, class: 0, pos: [((t * 0.8) % 8) - 4, 0, 2], vel: [0.8, 0, 0], conf: 200 },
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
