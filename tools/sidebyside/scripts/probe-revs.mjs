#!/usr/bin/env node
// Which contacts revise, at a pinned detail level: revisions per contact with its size, motion
// and radius, from the edge snapshot every step; then the totals by single/group and cause
// (docs/PROTOCOL_EVAL.md 11).
//   node tools/sidebyside/scripts/probe-revs.mjs [--clip busy] [--profile lora] [--level 4]
import path from 'node:path';
import { loadClip, replay, PROFILES, CLIPS, repo } from './eval.mjs';

const argv = process.argv.slice(2);
const flag = (k, d) => { const i = argv.indexOf(k); return i >= 0 ? argv[i + 1] : d; };
const clip = loadClip(path.join(repo, CLIPS[flag('--clip', 'busy')]));
const P = PROFILES[flag('--profile', 'lora')];
const level = Number(flag('--level', 4));
const per = new Map();
const prev = new Map();
replay(clip, P, { seed: 1, blackouts: [], duration: Math.ceil(clip.lastS + 5), uplink: P.up, uplinkLoss: P.loss, focus: '', level,
  edgeSampler: (es, t) => {
    for (const c of es.contacts) {
      if (c.parent != null) continue;
      const p = prev.get(c.id);
      const k = per.get(c.id) || { id: c.id, revs: 0, counts: new Set(), motions: {}, rmax: 0, cnt: [], life: [t, t] };
      k.life[1] = t; k.counts.add(c.count); k.rmax = Math.max(k.rmax, c.radius); k.cnt.push(c.count);
      if (p != null && p.rev !== c.rev) { k.revs++; const why = p.count !== c.count ? 'count' : p.motion !== c.motion ? 'motion' : p.lost !== c.lost ? 'lost' : 'pos/other'; k.motions[why] = (k.motions[why] || 0) + 1; }
      k.spd = (k.spd || 0) + c.speed; k.n = (k.n || 0) + 1; k.ce = (k.ce || 0) + c.ce; k.mix = c.mix;
      per.set(c.id, k); prev.set(c.id, { rev: c.rev, count: c.count, motion: c.motion, lost: c.lost });
    }
  } });
const rows = [...per.values()].sort((a, b) => b.revs - a.revs).slice(0, 15);
for (const k of rows) console.log(`id ${k.id} revs ${k.revs} life ${k.life[0].toFixed(0)}-${k.life[1].toFixed(0)} s counts ${[...k.counts].slice(0, 12).join(',')} rmax ${k.rmax.toFixed(0)} m why ${JSON.stringify(k.motions)} mean speed ${(k.spd / k.n).toFixed(1)} ce ${(k.ce / k.n).toFixed(1)} mix ${k.mix}`);
console.log('total revs', [...per.values()].reduce((s, k) => s + k.revs, 0), 'contacts', per.size);
const by = {}; for (const k of per.values()) for (const [w, n] of Object.entries(k.motions)) { const key = (k.counts.size > 1 || [...k.counts][0] > 1 ? 'group ' : 'single ') + w; by[key] = (by[key] || 0) + n; }
console.log(JSON.stringify(by));
