import WebSocket from "ws";
const ws = new WebSocket("ws://localhost:8090");
let n = 0;
setTimeout(() => { console.log("timeout"); process.exit(1); }, 70000);
ws.on("open", () => { ws.send(JSON.stringify({cmd:"seek", t: 10})); ws.send(JSON.stringify({cmd:"rate", x: 2})); });
ws.on("message", (d) => {
  const m = JSON.parse(d); n++;
  if (n === 5) ws.send(JSON.stringify({cmd:"link", profile:"lora"}));
  if (n === 150) ws.send(JSON.stringify({cmd:"link", profile:"blackout"}));
  if (n === 250) ws.send(JSON.stringify({cmd:"link", profile:"lora"}));
  if (n === 30 && m.rx.contacts.length) { const g = m.rx.contacts.find(c=>c.count>1) || m.rx.contacts[0]; console.log("focus split on", g.id, g.count); ws.send(JSON.stringify({cmd:"focus", id: g.id, mode:"split"})); }
  if ([3, 60, 140, 240, 330].includes(n)) {
    const ev = m.rx.events.slice(0,3).map(e=>`${e.t} ${e.kind} ${e.text}`);
    console.log(`#${n} t=${m.t} prof=${m.wire.profile} up=${m.wire.up} regime=${m.wire.regime} edge: ${m.edge.tracks.length} tracks ${m.edge.contacts.length} contacts (${m.edge.contacts.filter(c=>c.count>1).length} groups) | rx: ${m.rx.contacts.length} held, known ${m.rx.known} of ${m.rx.of}, ${m.rx.contacts.filter(c=>c.liveness==="fresh").length} fresh ${m.rx.contacts.filter(c=>c.liveness==="unheard").length} unheard | B/s ${m.wire.bytesPerS} (wire ${m.wire.wireBytesPerS}) dropped ${m.wire.dropped} | frames this step ${m.wire.frames.length}`);
    if (m.wire.frames[0]) console.log("   frame:", m.wire.frames[0].lines.slice(0,3).join(" | ").slice(0,400));
    if (ev.length) console.log("   events:", ev.join(" || ").slice(0,500));
  }
  if (n >= 340) {
    const c = m.rx.contacts.find(c=>c.child); console.log("children held:", m.rx.contacts.filter(c=>c.child).length, c ? JSON.stringify(c).slice(0,300): "");
    console.log("edge stats", JSON.stringify(m.wire.stats)); console.log("rx stats", JSON.stringify(m.rx.stats));
    ws.close(); process.exit(0); }
});
