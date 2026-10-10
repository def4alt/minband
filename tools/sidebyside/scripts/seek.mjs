// usage: node scripts/seek.mjs <t> [pause|play] [profile]  — drive the running server, then exit
import WebSocket from "ws";
const [t, mode, profile] = [Number(process.argv[2] ?? 20), process.argv[3] ?? 'pause', process.argv[4]];
const ws = new WebSocket("ws://localhost:8090");
ws.on("open", () => {
  if (profile) ws.send(JSON.stringify({ cmd: 'link', profile }));
  ws.send(JSON.stringify({ cmd: 'seek', t }));
  setTimeout(() => { ws.send(JSON.stringify({ cmd: mode })); setTimeout(() => process.exit(0), 300); }, 12000);
});
