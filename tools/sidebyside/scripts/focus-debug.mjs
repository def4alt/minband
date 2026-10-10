// Debug one operator-focus replay: node scripts/focus-debug.mjs <track> <at> <profile> <0|1> [log]
import path from 'node:path';
import { loadClip, replay, PROFILES, CLIPS, repo } from './eval.mjs';
const [track, at, prof, sel, log] = process.argv.slice(2);
const clip = loadClip(path.join(repo, CLIPS.best2));
const r = replay(clip, PROFILES[prof], { seed: 1, blackouts: [], duration: 41, uplink: true, uplinkLoss: PROFILES[prof].loss, focus: '', watch: { track: +track, at: +at, select: sel === '1', log: [] } });
if (log) for (const l of r.watch.log.filter((_, i) => i % 5 === 0)) console.log(JSON.stringify(l));
console.log('ackT', r.watch.ackT, 'focus sends', r.watch.sends, 'target ids', r.watch.ids.join(','));
console.log('contact bytes by id', JSON.stringify(r.watch.byId));
console.log('bytes/s by record', JSON.stringify(Object.fromEntries(Object.entries(r.bytes.perS).map(([k, v]) => [k, +v.toFixed(1)]))), 'app', r.bytes.appPerS.toFixed(1));
console.log('others', JSON.stringify(r.others));
