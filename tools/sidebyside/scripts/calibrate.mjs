#!/usr/bin/env node
// Is the receiver's circle calibrated? (PROTOCOL.md 5.3: it should hold the edge's estimate 95 %
// of the time.) On real footage runs and link profiles:
//  - tracked contacts: share of steps where |receiver - edge| <= ce_shown (the edge's estimate at
//    that step, coasted from its last observation when moving), by how long the link
//    could not vouch for the copy, and the circle's size;
//  - lost contacts the edge later sees again: was the reappearance inside the circle shown just
//    before it? And how many lost circles still locate the contact (no wider than the footprint).
//
//   node tools/sidebyside/scripts/calibrate.mjs [--clips busy,thermal,best2,convoy1] [--profiles hf,lora,telemetry,contested] [--seeds 2]
import path from 'node:path';
import { loadClip, replay, PROFILES, CLIPS, repo } from './eval.mjs';

const argv = process.argv.slice(2);
const flag = (k, d) => { const i = argv.indexOf(k); return i >= 0 ? argv[i + 1] : d; };
const clips = flag('--clips', 'busy,thermal,best2,convoy1').split(',');
const profiles = flag('--profiles', 'hf,lora,telemetry,contested').split(',');
const seeds = Number(flag('--seeds', 2));
const BUCKETS = [[0, 0.5], [0.5, 2], [2, 5], [5, 15], [15, 1e9]];
const q = (a, p) => { if (!a.length) return null; const s = Float64Array.from(a).sort(); return s[Math.min(s.length - 1, Math.floor(p * s.length))]; };
const f1 = (x) => (x == null ? '-' : x.toFixed(1)), pct = (x) => (x == null ? '-' : (100 * x).toFixed(1) + '%');
function table(rs) {
  const cols = Object.keys(rs[0]);
  const w = cols.map((c) => Math.max(c.length, ...rs.map((r) => String(r[c]).length)));
  const line = (v) => '| ' + v.map((x, i) => String(x).padEnd(w[i])).join(' | ') + ' |';
  return [line(cols), '|' + w.map((x) => '-'.repeat(x + 2)).join('|') + '|', ...rs.map((r) => line(cols.map((c) => r[c])))].join('\n');
}

const rows = [], byLag = [], lostRows = [];
for (const name of clips) {
  const clip = loadClip(path.join(repo, CLIPS[name]));
  for (const prof of profiles) {
    const P = PROFILES[prof];
    const tracked = [], lostSeen = [], reacq = [];
    for (let seed = 1; seed <= seeds; seed++) {
      const prevRx = new Map(), prevLost = new Map();
      replay(clip, P, { seed, blackouts: [], duration: Math.ceil(clip.lastS + 30), uplink: P.up, uplinkLoss: P.loss, focus: '', edgeSampler: (es, t, rxAll) => {
        const rx = new Map(rxAll.filter((c) => !c.departed).map((c) => [c.id, c]));
        for (const c of es.contacts) {
          if (c.departed) continue;
          const r = rx.get(c.id);
          if (r && !c.lost && !r.lost) tracked.push({ d: Math.hypot(c.now_e - r.e, c.now_n - r.n), ce: r.ce_shown, un: r.unassured_s, pm: r.p_miss });
          if (r && r.lost) lostSeen.push({ ce: r.ce_shown, located: r.located, liveness: r.liveness });
          // The edge sees a lost contact again: was it inside the circle the receiver showed?
          if (prevLost.get(c.id) && !c.lost) { const pr = prevRx.get(c.id); if (pr && pr.lost) reacq.push({ d: Math.hypot(c.e - pr.e, c.n - pr.n), ce: pr.ce_shown, located: pr.located }); }
          prevLost.set(c.id, c.lost);
        }
        prevRx.clear(); for (const [k, v] of rx) prevRx.set(k, v);
      } });
    }
    const cov = (a) => (a.length ? a.filter((s) => s.d <= s.ce).length / a.length : null);
    rows.push({ scene: name, link: prof, samples: tracked.length, 'holds edge': pct(cov(tracked)), 'circle med/p90 m': `${f1(q(tracked.map((s) => s.ce), 0.5))}/${f1(q(tracked.map((s) => s.ce), 0.9))}`,
      'err med/p90 m': `${f1(q(tracked.map((s) => s.d), 0.5))}/${f1(q(tracked.map((s) => s.d), 0.9))}`,
      'lost circle med m': f1(q(lostSeen.map((s) => s.ce), 0.5)), 'lost located': pct(lostSeen.length ? lostSeen.filter((s) => s.located).length / lostSeen.length : null),
      'seen again inside': reacq.length ? `${pct(cov(reacq))} of ${reacq.length}` : '-' });
    for (const [lo, hi] of BUCKETS) {
      const b = tracked.filter((s) => s.un >= lo && s.un < hi);
      if (b.length >= 50) byLag.push({ scene: name, link: prof, 'link cannot vouch for': hi > 1e8 ? `${lo}+ s` : `${lo}-${hi} s`, samples: b.length, 'holds edge': pct(cov(b)), 'circle med m': f1(q(b.map((s) => s.ce), 0.5)), 'p_miss med': f1(q(b.map((s) => s.pm), 0.5)) });
    }
  }
}
console.log('Tracked and lost contacts (coverage target 95 %)');
console.log(table(rows));
console.log('\nTracked contacts by time the link cannot vouch for');
console.log(table(byLag));
