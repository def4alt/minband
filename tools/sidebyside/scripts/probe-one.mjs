import WebSocket from "ws";
const ws = new WebSocket("ws://localhost:8090");
let n = 0; let target = null;
setTimeout(() => process.exit(1), 60000);
ws.on("open", () => { ws.send(JSON.stringify({cmd:"seek", t: 10})); ws.send(JSON.stringify({cmd:"rate", x: 2})); ws.send(JSON.stringify({cmd:"link", profile:"lora"})); });
ws.on("message", (d) => {
  const m = JSON.parse(d); n++;
  if (n === 40) { target = m.edge.contacts.find(c => c.lost && c.rev_dirty) || m.edge.contacts[0]; console.log("watching", target.id); }
  if (!target) return;
  const c = m.edge.contacts.find(x => x.id === target.id);
  const inFrames = m.wire.frames.filter(f => !f.up).map(f => f.lines.filter(l => l.includes(`Contact id=${target.id} `)).length).reduce((a,b)=>a+b,0);
  const ranks = m.wire.frames.filter(f => !f.up).map(f => f.lines.slice(1).map(l => l.split(' ')[0] + (l.match(/id=(\d+) rev=(\d+)/)?.slice(1,3).join('r') ?? '')).join(',')).join(' || ');
  if (n % 10 === 0 || inFrames) console.log(`#${n} t=${m.t} ${c ? `rev${c.rev} lost=${c.lost} dirty=${c.rev_dirty} step=${c.step} dueIn=${c.dueIn} sends=${c.sends}` : "gone"} inFrames=${inFrames} | ${ranks.slice(0, 260)}`);
  if (n >= 200) process.exit(0);
});
