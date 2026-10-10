// Out-of-view check on the live page: seek, play, and every 2 s list the receiver's contacts that
// are not fresh, with their circle (ceShown) and silence.  PORT=8090 node scripts/probe-outofview.mjs [seek] [seconds]
import WebSocket from 'ws';
const SEEK = Number(process.argv[2] || 18), SECS = Number(process.argv[3] || 30);
const ws = new WebSocket(`ws://localhost:${process.env.PORT || 8090}`);
let n = 0;
setTimeout(() => process.exit(0), (SECS + 5) * 1000);
ws.on('open', () => { ws.send(JSON.stringify({ cmd: 'link', profile: 'lora' })); ws.send(JSON.stringify({ cmd: 'seek', t: SEEK })); ws.send(JSON.stringify({ cmd: 'play' })); ws.send(JSON.stringify({ cmd: 'rate', x: 1 })); });
ws.on('message', (d) => {
  const m = JSON.parse(d); n++;
  if (n % 20 !== 0) return;
  const cs = m.rx.contacts.filter((c) => !c.departed && !c.child);
  const by = {}; for (const c of cs) by[c.liveness] = (by[c.liveness] || 0) + 1;
  const odd = cs.filter((c) => c.liveness !== 'fresh').map((c) => `#${c.id} ${c.liveness} circle ${c.ceShown} m (ce ${c.ce}) silent ${c.ageS} s`);
  console.log(`t=${m.t.toFixed(1)} ${JSON.stringify(by)} | ${odd.slice(0, 5).join(' ; ')}`);
});
