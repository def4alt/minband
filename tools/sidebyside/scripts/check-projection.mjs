// Numerical check of web/camera.js against track.py's ground model:
//  1. round-trip: refToGround(u, v) -> groundToRef -> (u, v) for a grid of reference pixels
//  2. the reference frame's homography is identity, so projectEN at clip time 0 == groundToRef
//  3. writes runs/sidebyside/check-points.json: projected pixels of every track at a tick, for
//     draw-check.py to paint on a frame of the 720p file.
// usage: node scripts/check-projection.mjs [tick]   (default 2400 = clip time 20 s)
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { makeCamera } from '../web/camera.js';
import { loadMeta } from './meta.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, '../../..');
const runDir = process.env.RUN_DIR || path.join(repo, 'runs/footage/meva-2018-03-13.16-00-14-bf');
const tick = Number(process.argv[2] || 2400);

const meta = loadMeta(runDir);
const cam = makeCamera(meta);
console.log('origin (ground-model X,Y of the tracks-frame origin):', meta.origin);

// 1. round trip
let worst = 0, nOk = 0;
for (let v = 100; v < meta.height; v += 200) for (let u = 100; u < meta.width; u += 400) {
  const g = cam.refToGround(u, v);
  if (!g) continue;
  const p = cam.groundToRef(g.X, g.Y);
  const err = Math.hypot(p.u - u, p.v - v);
  worst = Math.max(worst, err); nOk++;
}
console.log(`round trip ref->ground->ref over ${nOk} pixels: worst ${worst.toExponential(2)} px`);
if (worst > 1e-6) { console.error('FAIL: round trip'); process.exit(1); }

// 2. reference frame identity
{
  const p0 = cam.projectEN(0, 0, 0), r0 = cam.groundToRef(meta.origin[0], meta.origin[1]);
  const d = Math.hypot(p0.u - r0.u, p0.v - r0.v);
  console.log(`ENU origin at clip t=0 -> ref pixel (${r0.u.toFixed(1)}, ${r0.v.toFixed(1)}); via projectEN diff ${d.toExponential(2)} px`);
  if (d > 1e-6) { console.error('FAIL: identity H'); process.exit(1); }
  // camera nadir (ground X=Y=0) should be below the image (pitch 83 deg < 90): v > height
  const nadir = cam.groundToRef(0, 0);
  console.log(`nadir (ground 0,0) -> ref pixel (${nadir.u.toFixed(0)}, ${nadir.v.toFixed(0)}) [pitch 83 deg: expected inside the frame, ~cy + f*tan(7deg) = ${(meta.height/2 + meta.ground.f_px * Math.tan((90 - meta.ground.pitch_deg) * Math.PI / 180)).toFixed(0)}]`);
  // pixel -> ENU -> pixel at a mid-clip time
  const t = 40;
  const en = cam.unprojectPixel(1800, 1200, t), back = cam.projectEN(en.e, en.n, t);
  console.log(`unproject/project at t=${t}: (1800,1200) -> e=${en.e.toFixed(2)} n=${en.n.toFixed(2)} -> (${back.u.toFixed(3)}, ${back.v.toFixed(3)})`);
}

// 3. tracks at `tick`
const rows = fs.readFileSync(path.join(runDir, 'tracks.csv'), 'utf8').split('\n').slice(1);
const pts = [];
let best = null;
for (const line of rows) {
  if (!line) continue;
  const c = line.split(',');
  const tk = Number(c[0]);
  if (tk <= tick && (best === null || tk > best)) best = tk;
}
for (const line of rows) {
  if (!line) continue;
  const c = line.split(',');
  if (Number(c[0]) !== best) continue;
  const id = Number(c[1]), cls = Number(c[2]), x = Number(c[3]), z = Number(c[5]);
  const e = x, n = -z;
  const p = cam.projectEN(e, n, best / 120);
  if (p) pts.push({ id, cls, e, n, u: p.u, v: p.v });
}
const out = { tick: best, clipT: best / 120, width: meta.width, height: meta.height, points: pts };
const outPath = path.join(repo, 'runs/sidebyside/check-points.json');
fs.mkdirSync(path.dirname(outPath), { recursive: true });
fs.writeFileSync(outPath, JSON.stringify(out));
const inside = pts.filter((p) => p.u >= 0 && p.u < meta.width && p.v >= 0 && p.v < meta.height).length;
console.log(`tick ${best} (clip t ${(best / 120).toFixed(3)} s): ${pts.length} tracks, ${inside} inside the frame -> ${outPath}`);
