// Predictor ablation (analysis only, not part of `npm run eval`): a TypeScript mirror of the
// core's divergence trigger (core/src/edge.rs) and predictor (core/src/predictor.rs) with the
// per-class damping as a parameter, to ask "how many updates does the damping prior cost?"
// without touching core/. The mirror runs in f64, so counts can differ from the WASM Edge by a
// few updates; the first line of output validates it against the real core.
//   npm run ablation -- runs/synth/one_walker.csv [--theta 0.15] [--damping 1.0,0.95,0.85]
import { parseArgs } from 'node:util';
import { basename } from 'node:path';
import { TICK_HZ, readGt, toFrames, type Frame, type Track } from './gt.ts';
import { replayFrames } from './replay.ts';
import { isMain, userPath } from './paths.ts';

// core/src/classes.rs priors; person damping is the ablated parameter.
const PERSON = 0;
function prior(cls: number, personDamping: number) {
  if (cls === PERSON) return { damping: personDamping, maxSpeed: 3.0, groundY: 0 as number | null };
  if ([56, 63, 62].includes(cls)) return { damping: 0.1, maxSpeed: 1.0, groundY: null };
  return { damping: 0.6, maxSpeed: 3.0, groundY: null };
}

interface State { id: number; class: number; pos: number[]; vel: number[]; conf: number; tick: number }

function step(s: State, to: number, personDamping: number): State {
  if (to <= s.tick) return s;
  const p = prior(s.class, personDamping), dt = (to - s.tick) / TICK_HZ;
  const k = Math.max(0, 1 - (1 - p.damping) * dt);
  let vel = s.vel.map(v => v * k);
  const sp2 = vel[0] ** 2 + vel[1] ** 2 + vel[2] ** 2;
  if (sp2 > p.maxSpeed ** 2) { const sc = p.maxSpeed / Math.sqrt(sp2); vel = vel.map(v => v * sc); }
  const pos = s.pos.map((x, i) => x + (s.vel[i] + vel[i]) * 0.5 * dt);
  if (p.groundY !== null && pos[1] < p.groundY) { pos[1] = p.groundY; if (vel[1] < 0) vel[1] = 0; }
  return { ...s, pos, vel, tick: to };
}

export interface AblationResult { damping: number; deltas: number; updates: number; reasons: Record<string, number> }

export function ablate(frames: Frame[], thetaPos: number, thetaVel: number, personDamping: number): AblationResult {
  const KF = 2 * TICK_HZ, TMAX = 3 * TICK_HZ;
  const bucket = (c: number) => Math.floor((c * 4) / 256);
  const st = (t: Track, tick: number): State => ({ id: t.id, class: t.class, pos: [...t.pos], vel: [...t.vel], conf: t.conf, tick });
  let ghosts: State[] = [], lastKf = 0, deltas = 0, updates = 0;
  const reasons: Record<string, number> = { spawn: 0, despawn: 0, pos: 0, vel: 0, 'pos+vel': 0, age: 0, class: 0 };
  for (const f of frames) {
    const now = f.tick;
    if (f.tracks.length && now - lastKf >= KF) { lastKf = now; ghosts = f.tracks.map(t => st(t, now)); continue; }
    let n = 0;
    ghosts = ghosts.filter(g => { const keep = f.tracks.some(t => t.id === g.id); if (!keep) { n++; reasons.despawn++; } return keep; });
    for (const t of f.tracks) {
      const real = st(t, now);
      const gi = ghosts.findIndex(g => g.id === t.id);
      if (gi < 0) { ghosts.push(real); n++; reasons.spawn++; continue; }
      const g = ghosts[gi], pred = step(g, now, personDamping);
      const dp = Math.hypot(...pred.pos.map((x, i) => x - real.pos[i])) > thetaPos;
      const dv = Math.hypot(...pred.vel.map((x, i) => x - real.vel[i])) > thetaVel;
      const aged = now - g.tick >= TMAX;
      const cls = g.class !== real.class || bucket(g.conf) !== bucket(real.conf);
      if (dp || dv || aged || cls) {
        n++; ghosts[gi] = real;
        reasons[dp && dv ? 'pos+vel' : dp ? 'pos' : dv ? 'vel' : aged ? 'age' : 'class']++;
      }
    }
    if (n) { deltas++; updates += n; }
  }
  return { damping: personDamping, deltas, updates, reasons };
}

if (isMain(import.meta.url)) {
  const { values, positionals } = parseArgs({
    allowPositionals: true,
    options: { theta: { type: 'string', default: '0.15' }, damping: { type: 'string', default: '1.0,0.95,0.9,0.85' } },
  });
  if (!positionals.length) { console.error('usage: npm run ablation -- <gt.csv> [--theta 0.15] [--damping 1.0,0.85]'); process.exit(2); }
  const theta = Number(values.theta);
  for (const p of positionals) {
    const { frames, step: fs } = toFrames(readGt(userPath(p)));
    const real = replayFrames(frames, fs, { thetaPos: theta, thetaVel: 2 * theta });
    const dur = real.durationS;
    const mirror = ablate(frames, theta, 2 * theta, 0.85);
    console.log(`${basename(p, '.csv')} theta ${theta}: core (WASM) ${real.deltas} deltas / ${real.updates} updates; mirror at 0.85: ${mirror.deltas} / ${mirror.updates}`);
    for (const d of values.damping.split(',').map(Number)) {
      const r = ablate(frames, theta, 2 * theta, d);
      const why = Object.entries(r.reasons).filter(([, v]) => v).map(([k, v]) => `${k} ${v}`).join(', ');
      console.log(`  person damping ${d.toFixed(2)}/s: ${r.updates} updates (${(r.updates / dur).toFixed(2)}/s) in ${r.deltas} deltas  [${why}]`);
    }
  }
}
