import WebSocket from "ws";
const ws = new WebSocket("ws://localhost:8090");
let n = 0;
setTimeout(() => { console.log("timeout"); process.exit(1); }, 60000);
ws.on("open", () => { ws.send(JSON.stringify({cmd:"seek", t: 10})); ws.send(JSON.stringify({cmd:"rate", x: 2})); ws.send(JSON.stringify({cmd:"link", profile:"lora"})); });
ws.on("message", (d) => {
  const m = JSON.parse(d); n++;
  if (n !== 200) return;
  const edgeLive = new Map(m.edge.contacts.filter(c => !c.lost).map(c => [c.id, c]));
  const edgeAll = new Map(m.edge.contacts.map(c => [c.id, c]));
  const rxKnown = m.rx.contacts.filter(c => !c.lost && !c.departed && !c.child);
  console.log(`t=${m.t} edge contacts ${m.edge.contacts.length} live(non-lost) ${edgeLive.size}; rx held ${m.rx.contacts.length}, known ${m.rx.known} of ${m.rx.of}; rxKnown computed ${rxKnown.length}`);
  const stale = rxKnown.filter(c => !edgeLive.has(c.id));
  console.log(`rx known but not live at edge: ${stale.length}`);
  for (const c of stale.slice(0, 12)) {
    const e = edgeAll.get(c.id);
    console.log(`  rx#${c.id} rev${c.rev} ${c.motion} ${c.liveness} age ${c.ageS}s copies ${c.copies} | edge: ${e ? `rev${e.rev} lost=${e.lost} dirty=${e.rev_dirty} step=${e.step} dueIn=${e.dueIn} sends=${e.sends} count=${e.count}` : "GONE (not in edge snapshot)"}`);
  }
  const edgeLost = m.edge.contacts.filter(c => c.lost);
  console.log(`edge lost contacts: ${edgeLost.length}; dirty among them ${edgeLost.filter(c=>c.rev_dirty).length}; sends==0 ${edgeLost.filter(c=>c.sends===0).length}`);
  const live = [...edgeLive.values()];
  console.log(`edge live: dirty ${live.filter(c=>c.rev_dirty).length}, never sent ${live.filter(c=>c.sends===0).length}, step>=5 ${live.filter(c=>c.step>=5).length}`);
  console.log("edge stats", JSON.stringify(m.wire.stats));
  ws.close(); process.exit(0);
});
