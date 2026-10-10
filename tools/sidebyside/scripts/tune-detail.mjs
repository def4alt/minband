#!/usr/bin/env node
// The detail ladder, level by level: each level pinned on each clip and profile, against the
// adaptive edge, so the cost of a coarser level (per-track error) sits next to what it buys
// (picture up to date, k of n). Used to tune contacts::DETAIL (docs/PROTOCOL_EVAL.md 11).
//
//   node tools/sidebyside/scripts/tune-detail.mjs [--clips busy,best2] [--profiles lora,telemetry] [--levels 0,1,2,3,4,auto] [--set 4.pos_floor_m=30,3.count_tol=0.3] [--coarsen-s 2] [--seeds 1]
import path from 'node:path';
import { createRequire } from 'node:module';
import { loadClip, replay, PROFILES, CLIPS, repo } from './eval.mjs';

const argv = process.argv.slice(2);
const flag = (k, d) => { const i = argv.indexOf(k); return i >= 0 ? argv[i + 1] : d; };
const clips = flag('--clips', 'busy,best2').split(',');
const profiles = flag('--profiles', 'lora,telemetry').split(',');
const levels = flag('--levels', '1,2,3,4,auto').split(',');
// --set level.field=value,...: changes to the built-in ladder (read from a default edge).
let ladder;
if (flag('--set', '')) {
  const { WasmEdge } = createRequire(import.meta.url)(path.join(repo, 'core/pkg-node/minband_core.js'));
  ladder = JSON.parse(new WasmEdge('').snapshot_json(0)).detail.ladder;
  for (const kv of flag('--set', '').split(',')) { const [k, v] = kv.split('='); const [lv, field] = k.split('.'); ladder[+lv][field] = field === 'report_moving' ? v === 'true' : Number(v); }
}
const seeds = Number(flag('--seeds', 1));
const coarsenS = flag('--coarsen-s', '') ? Number(flag('--coarsen-s', '')) : undefined;
const f1 = (x) => (x == null ? '-' : x.toFixed(1)), pct = (x) => (x == null ? '-' : (100 * x).toFixed(1) + '%');
const mean = (a) => a.reduce((s, v) => s + v, 0) / a.length;
function table(rs) {
  const cols = Object.keys(rs[0]);
  const w = cols.map((c) => Math.max(c.length, ...rs.map((r) => String(r[c]).length)));
  const line = (v) => '| ' + v.map((x, i) => String(x).padEnd(w[i])).join(' | ') + ' |';
  return [line(cols), '|' + w.map((x) => '-'.repeat(x + 2)).join('|') + '|', ...rs.map((r) => line(cols.map((c) => r[c])))].join('\n');
}

const rows = [];
for (const name of clips) {
  const clip = loadClip(path.join(repo, CLIPS[name]));
  const duration = Math.ceil(clip.lastS + 75);
  for (const prof of profiles) {
    const P = PROFILES[prof];
    for (const lv of levels) {
      const rs = [];
      for (let seed = 1; seed <= seeds; seed++) rs.push(replay(clip, P, { seed, blackouts: [], duration, uplink: P.up, uplinkLoss: P.loss, focus: '', level: lv === 'auto' ? undefined : Number(lv), ladder, coarsenS }));
      const m = (f) => mean(rs.map(f));
      rows.push({
        clip: name, profile: prof, level: lv === 'auto' ? `auto ${f1(m((r) => r.detail.mean))} (${rs[0].detail.changes} ch, ${rs[0].detail.flaps} flap)` : lv,
        'comp@rev': pct(m((r) => r.completeness.rev)), 'comp@any': pct(m((r) => r.completeness.any)), 'k/n': pct(m((r) => r.integrity.kOfN)), honest: pct(m((r) => r.honesty.frac)),
        'trk shown': pct(m((r) => r.perTrack.shown)), 'trk err med/p90': `${f1(m((r) => r.perTrack.med))}/${f1(m((r) => r.perTrack.p90))}`,
        revisions: Math.round(m((r) => r.cmStats.revisions)), 'contacts': f1(m((r) => r.completeness.meanLive)), 'app B/s': f1(m((r) => r.bytes.appPerS)),
      });
    }
  }
}
console.log(table(rows));
