#!/usr/bin/env node
// Operator focus, measured: the operator clicks one object on the receiver at `at`; the receiver
// sends Focus up the lossy uplink (every 1 s until acknowledged, then every 5 s) and the edge gives that contact priority.
// Each target x profile is replayed with and without the click over several loss seeds, and the
// same object is measured identically in both runs against the footage track (the truth).
//
//   node tools/sidebyside/scripts/focus.mjs [--profile hf,lora,telemetry,contested] [--seeds 5] [--json out.json]
import fs from 'node:fs';
import path from 'node:path';
import { loadClip, replay, PROFILES, CLIPS, repo } from './eval.mjs';

const argv = process.argv.slice(2);
const flag = (k, d) => { const i = argv.indexOf(k); return i >= 0 ? argv[i + 1] : d; };
const profiles = flag('--profile', 'hf,lora,telemetry,contested').split(',');
const seeds = Number(flag('--seeds', 5));
const jsonOut = flag('--json', '');

// Targets (track ids from each run's tracks.csv). best2 = the 1080p consensus run (24 tracks, no
// real mover); busy = the 4K parking-lot run (206 tracks, saturated at 2 kbit/s) with walkers and
// moving cars. `until` ends the measurement where the footage track stops being trustworthy.
const TARGETS = [
  { name: 'standing person', clip: 'best2', track: 1, at: 10, until: 28, note: 'next to a second person; the track coasts as a ghost after 28 s' },
  { name: 'person in group', clip: 'best2', track: 21, at: 14, note: 'one of four people standing together' },
  { name: 'parked car', clip: 'best2', track: 24, at: 16, note: 'parked beside another car' },
  { name: 'walker (busy)', clip: 'busy', track: 24, at: 14, until: 60, note: 'walks about 1 m/s through the crowd' },
  { name: 'moving car (busy)', clip: 'busy', track: 56, at: 4, until: 22, note: 'drives 33 m across the lot' },
];
const only = flag('--target', '');
const clips = {};
const clipOf = (name) => clips[name] || (clips[name] = loadClip(path.join(repo, CLIPS[name])));
const q = (a, p) => { if (!a.length) return null; const s = [...a].sort((x, y) => x - y); return s[Math.min(s.length - 1, Math.floor(p * s.length))]; };
const f1 = (x) => (x == null ? '-' : x.toFixed(1));
const f2 = (x) => (x == null ? '-' : x.toFixed(2));
const pct = (x) => (x == null ? '-' : Math.round(100 * x) + '%');
const mean = (a) => (a.length ? a.reduce((s, v) => s + v, 0) / a.length : null);

function pooled(runs) {
  const W = runs.map((r) => r.watch);
  const all = (k) => W.flatMap((w) => w[k]);
  const sum = (k) => W.reduce((s, w) => s + w[k], 0);
  const steps = sum('steps'), known = sum('known');
  const span = steps * 0.1;
  return {
    known: known / steps, indiv: known ? sum('indiv') / known : null, inside: known ? sum('inside') / known : null,
    updPerS: sum('arrivals') / span, ageMed: q(all('ages'), 0.5), ageP90: q(all('ages'), 0.9),
    errMed: q(all('errs'), 0.5), errP90: q(all('errs'), 0.9), spdErr: mean(all('spdErr')),
    ackT: W[0].select ? mean(W.map((w) => w.ackT).filter((x) => x != null)) : null, acked: W.filter((w) => w.ackT != null).length,
    indivT: mean(W.map((w) => w.indivT).filter((x) => x != null)), sends: sum('sends') / W.length,
    targetBps: W.reduce((s, w) => s + w.bytes, 0) / runs.length / runs[0].duration,
    appBps: mean(runs.map((r) => r.bytes.appPerS)),
    othersComp: mean(runs.map((r) => r.others.compRev)), othersErr: mean(runs.map((r) => r.others.meanErr)), othersHonest: mean(runs.map((r) => r.others.honesty)),
  };
}

// base: nobody clicks. group: the operator clicks the object's contact; a group is split and all
// its members are focused. drill: same click, then the operator picks the one individual and
// releases the group (a lone contact is the same in both).
const RUNS = [['base', false, null], ['group', true, 'group'], ['drill', true, 'drill']];
const LABEL = { base: 'not selected', group: 'selected, whole group', drill: 'selected, drilled to one' };
const rows = [], out = [];
for (const tg of TARGETS.filter((x) => !only || x.name.startsWith(only))) {
  const clip = clipOf(tg.clip);
  const duration = Math.ceil(tg.until != null ? tg.until + 2 : clip.lastS + 5);
  for (const prof of profiles) {
    const P = { ...PROFILES[prof] };
    const res = {};
    for (const [k, select, flow] of RUNS) {
      const runs = [];
      for (let seed = 1; seed <= seeds; seed++) {
        runs.push(replay(clip, P, { seed, blackouts: [], duration, uplink: P.up, uplinkLoss: P.loss, focus: '', watch: { track: tg.track, at: tg.at, until: tg.until, select, flow } }));
      }
      res[k] = pooled(runs);
    }
    out.push({ target: tg, profile: prof, P, ...res });
    for (const [k] of RUNS) {
      const r = res[k];
      rows.push({
        target: tg.name, link: `${prof} ${P.budgetBps / 1000}k`, run: LABEL[k],
        'priority after s': k !== 'base' ? `${f1(r.ackT)} (${r.acked}/${seeds})` : '-',
        'updates/s': f2(r.updPerS), 'info age s med/p90': `${f1(r.ageMed)}/${f1(r.ageP90)}`,
        'err m med/p90': `${f1(r.errMed)}/${f1(r.errP90)}`, 'inside circle': pct(r.inside), 'as individual': pct(r.indiv),
        'target B/s': f1(r.targetBps), 'total B/s': f1(r.appBps),
        'others comp@rev': pct(r.othersComp), 'others err m': f1(r.othersErr),
      });
    }
  }
}

function table(rs) {
  const cols = Object.keys(rs[0]);
  const w = cols.map((c) => Math.max(c.length, ...rs.map((r) => String(r[c]).length)));
  const line = (vals) => '| ' + vals.map((v, i) => String(v).padEnd(w[i])).join(' | ') + ' |';
  return [line(cols), '|' + w.map((x) => '-'.repeat(x + 2)).join('|') + '|', ...rs.map((r) => line(cols.map((c) => r[c])))].join('\n');
}
console.log(`${seeds} seeds per cell; measured from the click until the object leaves the footage (or \`until\`)`);
console.log(table(rows));
if (jsonOut) fs.writeFileSync(jsonOut, JSON.stringify(out, null, 1));
