#!/usr/bin/env node
// Usage: node tools/sidebyside/scripts/drift.mjs [out.json]   (fits for core belief.rs: docs/PROTOCOL_EVAL.md 10)
// Prediction error on real footage: at each revision of an edge contact take its state as "the
// record" (the receiver's prediction = dead reckoning along course at speed when moving, else the
// position), then measure |edge estimate(t + lag) - prediction(lag)| for later lags while the
// contact lives, ignoring later revisions (what a receiver that missed them would be off by).
import path from 'node:path';
const ev = await import('./eval.mjs');
const LAGS = [0.5, 1, 2, 3, 5, 8, 12, 20, 30, 45];
const out = {};
for (const name of ['busy', 'thermal', 'best2', 'convoy1', 'convoy2']) {
  const clip = ev.loadClip(path.join(ev.repo, ev.CLIPS[name]));
  const recs = []; // {id, t, e, n, ve, vn, key}
  const hist = new Map(); // id -> Map(tick10 -> {e,n})
  ev.replay(clip, ev.PROFILES.clean, { seed: 1, blackouts: [], duration: Math.ceil(clip.lastS), uplink: true, uplinkLoss: 0, focus: '', edgeSampler: (es, t) => {
    const k10 = Math.round(t * 10);
    for (const c of es.contacts) {
      if (c.parent != null || c.departed || c.lost) continue;
      let h = hist.get(c.id); if (!h) hist.set(c.id, (h = { last: null, pos: new Map() }));
      h.pos.set(k10, [c.e, c.n]);
      if (h.last !== c.rev) {
        h.last = c.rev;
        const moving = c.motion === 'moving' && c.speed > 0;
        const a = (c.course * Math.PI) / 180;
        const cls = c.mix[1] + c.mix[2] >= c.mix[0] ? 'vehicle' : 'dismount';
        recs.push({ id: c.id, k10, e: c.e, n: c.n, ve: moving ? Math.sin(a) * c.speed : 0, vn: moving ? Math.cos(a) * c.speed : 0, key: `${c.motion}/${cls}` });
      }
    }
  } });
  for (const r of recs) {
    const h = hist.get(r.id);
    for (const L of LAGS) {
      const p = h.pos.get(r.k10 + Math.round(L * 10)); if (!p) continue;
      const d = Math.hypot(p[0] - (r.e + r.ve * L), p[1] - (r.n + r.vn * L));
      ((out[r.key] ||= {})[L] ||= []).push(d);
    }
  }
}
const q = (a, p) => { const s = [...a].sort((x, y) => x - y); return s[Math.min(s.length - 1, Math.floor(p * s.length))]; };
console.log('state/class'.padEnd(18), LAGS.map((l) => `${l}s`.padStart(11)).join(''));
for (const [k, by] of Object.entries(out).sort()) {
  console.log(k.padEnd(18), LAGS.map((l) => (by[l] ? `${q(by[l], 0.5).toFixed(1)}/${q(by[l], 0.95).toFixed(1)}(${by[l].length})` : '-').padStart(11)).join(''));
}
console.log("cells: median/p95 metres (samples)"); (await import("node:fs")).writeFileSync(process.argv[2] || "/dev/null", JSON.stringify(out));
