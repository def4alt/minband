#!/usr/bin/env node
// Busy versus quiet, on real footage runs.
//  1. Budget sweep: how the picture degrades as the link shrinks (busy 4K lot, thermal road, quiet lot).
//  2. Collapse: for objects the edge holds unchanged, how close the receiver's position gets to the
//     edge's and how far its error circle shrinks, by how long the object has been unchanged.
//
//   node tools/sidebyside/scripts/collapse.mjs [--clips busy,thermal,best2] [--budgets 600,...] [--seeds 2] [--json out]
import fs from 'node:fs';
import path from 'node:path';
import { loadClip, replay, CLIPS, repo } from './eval.mjs';

const argv = process.argv.slice(2);
const flag = (k, d) => { const i = argv.indexOf(k); return i >= 0 ? argv[i + 1] : d; };
const clips = flag('--clips', 'busy,thermal,best2').split(',');
const budgets = flag('--budgets', '600,1200,2000,4800,9600,19200,64000').split(',').map(Number);
const seeds = Number(flag('--seeds', 2));
const collapseAt = flag('--collapse-at', '600,2000,9600').split(',').map(Number);
const jsonOut = flag('--json', '');
const LINK = { loss: 0.05, delayS: 0.3, up: true, video: false };
const AGES = [[0, 1], [1, 3], [3, 10], [10, 30], [30, 1e9]];

const q = (a, p) => { if (!a.length) return null; const s = Float64Array.from(a).sort(); return s[Math.min(s.length - 1, Math.floor(p * s.length))]; };
const f1 = (x) => (x == null ? '-' : x.toFixed(1)), f2 = (x) => (x == null ? '-' : x.toFixed(2));
const pct = (x) => (x == null ? '-' : Math.round(100 * x) + '%');
const mean = (a) => (a.length ? a.reduce((s, v) => s + v, 0) / a.length : null);
function table(rs) {
  const cols = Object.keys(rs[0]);
  const w = cols.map((c) => Math.max(c.length, ...rs.map((r) => String(r[c]).length)));
  const line = (v) => '| ' + v.map((x, i) => String(x).padEnd(w[i])).join(' | ') + ' |';
  return [line(cols), '|' + w.map((x) => '-'.repeat(x + 2)).join('|') + '|', ...rs.map((r) => line(cols.map((c) => r[c])))].join('\n');
}

const sweep = [], collapse = [], out = { sweep: [], collapse: [] };
for (const name of clips) {
  const clip = loadClip(path.join(repo, CLIPS[name]));
  const duration = Math.ceil(clip.lastS + 2); // only while the footage runs: the tail is all lost/departed
  for (const bps of budgets) {
    const runs = [], samples = [];
    for (let seed = 1; seed <= seeds; seed++) runs.push(replay(clip, { ...LINK, budgetBps: bps }, { seed, blackouts: [], duration, uplink: true, uplinkLoss: LINK.loss, focus: '', samples }));
    const live = samples.filter((s) => s.motion !== 'unknown' || true);
    const lat = runs.flatMap((r) => r.latency.new.median != null ? [r.latency.new.median] : []);
    const row = {
      scene: name, 'bit/s': bps, 'live contacts': f1(mean(runs.map((r) => r.completeness.meanLive))),
      current: pct(mean(runs.map((r) => r.completeness.rev))), known: pct(mean(runs.map((r) => r.completeness.any))),
      'err med/p90 m': `${f1(q(live.map((s) => s.d), 0.5))}/${f1(q(live.map((s) => s.d), 0.9))}`,
      'circle med m': f1(q(live.map((s) => s.ceShown), 0.5)), 'circle holds': pct(live.filter((s) => s.d <= s.ceShown).length / Math.max(1, live.length)),
      'new shown after s': f1(mean(lat)), 'used B/s': f1(mean(runs.map((r) => r.bytes.appPerS))),
    };
    sweep.push(row); out.sweep.push({ ...row, raw: runs.map((r) => ({ completeness: r.completeness, honesty: { frac: r.honesty.frac, meanErr: r.honesty.meanErr }, latency: r.latency.new, bytes: r.bytes.appPerS })) });
    if (collapseAt.includes(bps)) {
      for (const [lo, hi] of AGES) {
        const still = samples.filter((s) => (s.motion === 'static' || s.motion === 'stopped') && s.quiet >= lo && s.quiet < hi);
        if (!still.length) continue;
        const r = {
          scene: name, 'bit/s': bps, 'unchanged for': hi > 1e8 ? `${lo}+ s` : `${lo}-${hi} s`, samples: still.length,
          'err med/p90 m': `${f2(q(still.map((s) => s.d), 0.5))}/${f2(q(still.map((s) => s.d), 0.9))}`,
          'within 1 m': pct(still.filter((s) => s.d <= 1).length / still.length),
          'edge ce med m': f1(q(still.map((s) => s.ce), 0.5)), 'circle med m': f1(q(still.map((s) => s.ceShown), 0.5)),
          'circle / ce': f2(q(still.map((s) => s.ceShown / Math.max(0.1, s.ce)), 0.5)), 'stale rev': pct(still.filter((s) => s.stale).length / still.length),
        };
        collapse.push(r); out.collapse.push(r);
      }
    }
  }
}
console.log(`Budget sweep (loss 5 %, delay 0.3 s, ${seeds} seeds, while the footage runs)`);
console.log(table(sweep));
console.log(`\nStatic and stopped objects: receiver vs edge position and the circle, by time unchanged (position quantum 1 m)`);
console.log(table(collapse));
if (jsonOut) fs.writeFileSync(jsonOut, JSON.stringify(out, null, 1));
