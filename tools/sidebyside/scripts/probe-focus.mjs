// Click-to-focus on the live page (driver on :8090): click the group holding footage track TRACK,
// then, once its individuals are on the receiver, click the one holding TRACK (drill). Prints the
// focused individual's sends and information age.  node scripts/probe-focus.mjs [track] [profile]
import WebSocket from 'ws';
const TRACK = Number(process.argv[2] || 21), PROFILE = process.argv[3] || 'lora';
const ws = new WebSocket(`ws://localhost:${process.env.PORT || 8090}`);
let n = 0, stage = 'wait', group = null, child = null, sends = 0, ages = [], t0 = null;
setTimeout(() => { console.log('timeout', stage); process.exit(1); }, 90000);
ws.on('open', () => { ws.send(JSON.stringify({ cmd: 'link', profile: PROFILE })); ws.send(JSON.stringify({ cmd: 'seek', t: Number(process.env.SEEK || 13) })); ws.send(JSON.stringify({ cmd: 'play' })); ws.send(JSON.stringify({ cmd: 'rate', x: 1 })); });
ws.on('message', (d) => {
  const m = JSON.parse(d); n++;
  if (n < 10) return;
  const holders = m.edge.contacts.filter((c) => c.members.includes(TRACK));
  const top = holders.find((c) => c.parent == null), kid = holders.find((c) => c.parent != null);
  if (stage === 'wait' && top && m.rx.contacts.some((c) => c.id === top.id)) {
    group = top.id; stage = 'split'; t0 = m.t;
    console.log(`t=${m.t} click group #${group} (${top.count} members)`); ws.send(JSON.stringify({ cmd: 'focus', id: group, mode: 'auto' }));
  } else if (stage === 'split' && kid && m.rx.contacts.some((c) => c.id === kid.id && c.child)) {
    child = kid.id; stage = 'drill';
    console.log(`t=${m.t} individuals on the map after ${(m.t - t0).toFixed(1)} s; click #${child}`); ws.send(JSON.stringify({ cmd: 'focus', id: child, mode: 'auto' }));
  } else if (stage === 'drill') {
    const rc = m.rx.contacts.find((c) => c.id === child), g = m.rx.contacts.find((c) => c.id === group);
    sends += m.wire.frames.filter((f) => !f.up).reduce((s, f) => s + f.lines.filter((l) => l.startsWith(`Contact id=${child} `)).length, 0);
    if (rc) ages.push(rc.ageS);
    const siblings = m.rx.contacts.filter((c) => c.child && c.parent === group && c.id !== child && !c.departed).length;
    if (n % 20 === 0) console.log(`t=${m.t} #${child} focused=${rc?.focused} age=${rc?.ageS}s err-circle=${rc?.ceShown} m | group #${group} focused=${g?.focused} | live siblings ${siblings} | edge focus ${JSON.stringify(m.edge.focus ?? null)}`);
    if (ages.length >= Number(process.env.N || 150)) { ages.sort((a, b) => a - b); console.log(`${ages.length / 10} s after drilling: ${sends} sends of #${child} (${(sends / (ages.length / 10)).toFixed(2)}/s), info age median ${ages[ages.length >> 1]} s, p90 ${ages[Math.floor(ages.length * 0.9)]} s`); process.exit(0); }
  }
});
