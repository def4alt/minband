// Synthetic ground truth for named scenarios, in the phone's GroundTruthLog CSV format.
//   npm run synth                                   # the standard set into runs/synth/
//   npm run synth -- --scenario crowd --duration 300 --hz 60
//   npm run synth -- --scenario three_walkers --noise 0.03   # -> three_walkers_noisy.csv
// Motion is simulated at the tick rate (120 Hz) and logged every `120/hz` ticks, like the phone
// logging one row per track per frame. Velocities are the true derivatives (a perfect tracker);
// optional gaussian observation noise is added to position (sigma, m) and velocity (sigma_v, m/s).
import { parseArgs } from 'node:util';
import { join } from 'node:path';
import { TICK_HZ, writeGt, type GtRow } from './gt.ts';
import { rng, subSeed, type Rng } from './rng.ts';
import { SYNTH_DIR, isMain, userPath } from './paths.ts';

export const PERSON = 0, CUP = 41, CHAIR = 56, TV = 62, LAPTOP = 63;
const PERSON_Y = 0.9; // bbox centre height of a standing person, metres above the marker plane

export interface SynthOptions {
  durationS?: number;      // default 120
  hz?: number;             // logged frame rate, default 30 (phone tracker rate, DESIGN 3.1)
  noise?: number;          // position sigma, metres; default 0
  velNoise?: number;       // velocity sigma, m/s; default 2 * noise
  seed?: number;           // default 1
}

export interface ScenarioSpec { name: string; base: string; noise: number; description: string }

/** The standard evaluation set: the four scenarios plus one noisy variant. */
export const STANDARD: ScenarioSpec[] = [
  { name: 'static', base: 'static', noise: 0, description: '3 static objects (chair, tv, laptop)' },
  { name: 'one_walker', base: 'one_walker', noise: 0, description: '1 person on a room tour with turns and one 2 s stop' },
  { name: 'one_walker_noisy', base: 'one_walker', noise: 0.03, description: 'one_walker + 3 cm / 6 cm/s gaussian observation noise' },
  { name: 'three_walkers', base: 'three_walkers', noise: 0, description: 'room tour + circular loop + stop-and-go' },
  { name: 'crowd', base: 'crowd', noise: 0, description: '8 concurrent people, random waypoints, pauses, enter/leave churn' },
];

interface Sample { id: number; class: number; pos: [number, number, number]; vel: [number, number, number]; conf: number }
/** A scene advances one tick (1/120 s) per call and returns the entities visible after it. */
type Scene = (tick: number) => Sample[];

// ---------------------------------------------------------------- motion primitives

interface Waypoint { x: number; z: number; pause?: number }

/**
 * A pedestrian steering towards waypoints: bounded turn rate, bounded acceleration, slows down
 * for sharp turns and comes to a smooth stop at waypoints with a pause.
 */
function walker(o: { start: [number, number]; waypoints: Waypoint[]; mode: 'loop' | 'pingpong'; cruise: number; accel?: number; turnRate?: number }) {
  const accel = o.accel ?? 1.0, turnRate = o.turnRate ?? 2.0, dt = 1 / TICK_HZ;
  let x = o.start[0], z = o.start[1], speed = 0, wi = 0, dir = 1, pause = 0;
  const first = o.waypoints[0];
  let h = Math.atan2(first.z - z, first.x - x);
  const advance = () => {
    const n = o.waypoints.length;
    if (o.mode === 'loop') wi = (wi + 1) % n;
    else { if (wi + dir < 0 || wi + dir >= n) dir = -dir; wi += dir; }
  };
  return () => {
    if (pause > 0) {
      pause -= dt; speed = 0;
      // Turn in place while stopped, so the walker sets off towards the next waypoint.
      if (pause <= 0) { const n = o.waypoints[wi]; h = Math.atan2(n.z - z, n.x - x); }
      return { x, z, vx: 0, vz: 0 };
    }
    const wp = o.waypoints[wi];
    const dx = wp.x - x, dz = wp.z - z, dist = Math.hypot(dx, dz);
    let diff = Math.atan2(dz, dx) - h;
    while (diff > Math.PI) diff -= 2 * Math.PI;
    while (diff < -Math.PI) diff += 2 * Math.PI;
    h += Math.max(-turnRate * dt, Math.min(turnRate * dt, diff));
    let want = o.cruise * Math.max(0.35, Math.cos(Math.min(Math.abs(diff), Math.PI / 2)));
    if (wp.pause) want = Math.min(want, Math.sqrt(2 * accel * Math.max(0, dist - 0.02)));
    speed += Math.max(-accel * dt, Math.min(accel * dt, want - speed));
    const vx = Math.cos(h) * speed, vz = Math.sin(h) * speed;
    x += vx * dt; z += vz * dt;
    const arrived = wp.pause ? dist < 0.03 || (dist < 0.3 && speed < 0.03) : dist < 0.35;
    if (arrived) {
      if (wp.pause) { pause = wp.pause; speed = 0; advance(); return { x, z, vx: 0, vz: 0 }; }
      advance();
    }
    return { x, z, vx, vz };
  };
}

function person(id: number, conf: number, s: { x: number; z: number; vx: number; vz: number }): Sample {
  return { id, class: PERSON, pos: [s.x, PERSON_Y, s.z], vel: [s.vx, 0, s.vz], conf };
}

const TOUR: Waypoint[] = [{ x: 3, z: -2 }, { x: 3, z: 2, pause: 2 }, { x: -1, z: 2.5 }, { x: -3, z: 0 }, { x: -3, z: -2 }];

// ---------------------------------------------------------------- scenarios

function staticRoom(): Scene {
  const objs: Sample[] = [
    { id: 1, class: CHAIR, pos: [-1.5, 0.45, 1.2], vel: [0, 0, 0], conf: 170 },
    { id: 2, class: TV, pos: [0.0, 1.2, -3.0], vel: [0, 0, 0], conf: 225 },
    { id: 3, class: LAPTOP, pos: [1.2, 0.75, 0.4], vel: [0, 0, 0], conf: 150 },
  ];
  return () => objs.map(o => ({ ...o, pos: [...o.pos], vel: [...o.vel] }) as Sample);
}

function oneWalker(): Scene {
  const w = walker({ start: [-3, -2], waypoints: TOUR, mode: 'loop', cruise: 1.2 });
  return () => [person(1, 230, w())];
}

function threeWalkers(): Scene {
  const tour = walker({ start: [-3, -2], waypoints: TOUR, mode: 'loop', cruise: 1.2 });
  const stopGo = walker({
    start: [-3.5, -3.5], mode: 'pingpong', cruise: 1.3,
    waypoints: [{ x: -3.5, z: -3.5, pause: 1.5 }, { x: -1.2, z: -3.5, pause: 1.5 }, { x: 1.2, z: -3.5, pause: 1.5 }, { x: 3.5, z: -3.5, pause: 1.5 }],
  });
  const r = 2.0, v = 1.0, w = v / r, cx = 0.5, cz = 0;
  return (tick) => {
    const t = tick / TICK_HZ, a = w * t;
    const loop = { x: cx + r * Math.cos(a), z: cz + r * Math.sin(a), vx: -v * Math.sin(a), vz: v * Math.cos(a) };
    return [person(1, 230, tour()), person(2, 210, loop), person(3, 200, stopGo())];
  };
}

function crowd(r: Rng): Scene {
  const H = 4.5; // room half-size, metres
  let nextId = 1;
  interface Agent { id: number; conf: number; step: () => { x: number; z: number; vx: number; vz: number }; dieAt: number }
  const randomPoint = (): Waypoint => ({ x: r.range(-H, H), z: r.range(-H, H), pause: r.next() < 0.35 ? r.range(0.5, 3) : undefined });
  const edgePoint = (): [number, number] => {
    const s = r.range(-H, H);
    switch (Math.floor(r.next() * 4)) { case 0: return [-H, s]; case 1: return [H, s]; case 2: return [s, -H]; default: return [s, H]; }
  };
  const spawn = (tick: number, start: [number, number], lifeS: number): Agent => {
    const wps = Array.from({ length: 12 }, randomPoint);
    // conf in 140..250, avoiding the 4-bucket boundary at 192 so it never flips buckets.
    let conf = Math.round(r.range(140, 250));
    if (Math.abs(conf - 192) < 6) conf += 12;
    return { id: nextId++, conf, step: walker({ start, waypoints: wps, mode: 'loop', cruise: r.range(0.8, 1.5), turnRate: r.range(1.5, 3) }), dieAt: tick + Math.round(lifeS * TICK_HZ) };
  };
  const agents: Agent[] = Array.from({ length: 8 }, () => spawn(0, [r.range(-H, H), r.range(-H, H)], r.range(10, 60)));
  return (tick) => {
    for (let i = 0; i < agents.length; i++) {
      if (tick >= agents[i].dieAt) agents[i] = spawn(tick, edgePoint(), r.range(25, 60)); // leaves view; a newcomer enters
    }
    return agents.map(a => person(a.id, a.conf, a.step()));
  };
}

export const SCENARIOS: Record<string, (r: Rng) => Scene> = {
  static: () => staticRoom(),
  one_walker: () => oneWalker(),
  three_walkers: () => threeWalkers(),
  crowd: (r) => crowd(r),
};

// ---------------------------------------------------------------- generation

export function generate(base: string, opts: SynthOptions = {}): GtRow[] {
  const make = SCENARIOS[base];
  if (!make) throw new Error(`unknown scenario "${base}" (have: ${Object.keys(SCENARIOS).join(', ')})`);
  const durationS = opts.durationS ?? 120, hz = opts.hz ?? 30, seed = opts.seed ?? 1;
  const step = TICK_HZ / hz;
  if (!Number.isInteger(step) || step < 1) throw new Error(`--hz must divide ${TICK_HZ} (got ${hz})`);
  const sigma = opts.noise ?? 0, sigmaV = opts.velNoise ?? 2 * sigma;
  // Motion depends only on (scenario, seed); noise has its own stream so noisy and clean variants
  // of a scenario share the same underlying trajectories.
  const scene = make(rng(subSeed(seed, base)));
  const noise = rng(subSeed(seed, `${base}:noise`));
  const rows: GtRow[] = [];
  const ticks = Math.round(durationS * TICK_HZ);
  for (let tick = 0; tick < ticks; tick++) {
    const samples = scene(tick);
    if (tick % step !== 0) continue;
    for (const s of samples) {
      const p = s.pos, v = s.vel;
      const n = (sd: number) => (sd > 0 ? sd * noise.gauss() : 0);
      rows.push({
        tick, id: s.id, class: s.class,
        x: p[0] + n(sigma), y: p[1] + n(sigma), z: p[2] + n(sigma),
        vx: v[0] + n(sigmaV), vy: v[1] + n(sigmaV), vz: v[2] + n(sigmaV),
        conf: s.conf,
      });
    }
  }
  return rows;
}

/** Write the standard set (or `specs`) into `dir`; returns the written paths by scenario name. */
export function writeStandard(dir = SYNTH_DIR, opts: SynthOptions = {}, specs = STANDARD): Map<string, string> {
  const out = new Map<string, string>();
  for (const s of specs) {
    const path = join(dir, `${s.name}.csv`);
    writeGt(path, generate(s.base, { ...opts, noise: opts.noise ?? s.noise }));
    out.set(s.name, path);
  }
  return out;
}

if (isMain(import.meta.url)) {
  const { values } = parseArgs({
    options: {
      scenario: { type: 'string', multiple: true },
      duration: { type: 'string', default: '120' },
      hz: { type: 'string', default: '30' },
      noise: { type: 'string' },
      'vel-noise': { type: 'string' },
      seed: { type: 'string', default: '1' },
      out: { type: 'string' },
    },
  });
  const dir = values.out ? userPath(values.out) : SYNTH_DIR;
  const opts: SynthOptions = {
    durationS: Number(values.duration), hz: Number(values.hz), seed: Number(values.seed),
    velNoise: values['vel-noise'] !== undefined ? Number(values['vel-noise']) : undefined,
  };
  const noise = values.noise !== undefined ? Number(values.noise) : undefined;
  const specs: ScenarioSpec[] = values.scenario
    ? values.scenario.map(b => ({ name: noise ? `${b}_noisy` : b, base: b, noise: noise ?? 0, description: '' }))
    : STANDARD;
  const written = writeStandard(dir, { ...opts, noise }, specs);
  for (const [name, path] of written) console.log(`synth ${name.padEnd(18)} -> ${path}`);
}
