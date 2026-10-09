import dgram from 'node:dgram';
import { WebSocketServer, WebSocket } from 'ws';
import { World } from './world.js';
import type { ControlMessage } from './types.js';

const UDP_PORT = Number(process.env.MINBAND_UDP_PORT ?? 7777);
const WS_PORT = Number(process.env.MINBAND_WS_PORT ?? 8080);

const world = new World();
const udp = dgram.createSocket('udp4');
udp.on('message', (msg, rinfo) => {
  const addr = `${rinfo.address}:${rinfo.port}`;
  const ack = world.ingest(addr, new Uint8Array(msg), Date.now());
  if (ack) udp.send(ack, rinfo.port, rinfo.address);
});
udp.bind(UDP_PORT, () => console.log(`udp ingest on :${UDP_PORT}`));

const wss = new WebSocketServer({ port: WS_PORT });
wss.on('listening', () => console.log(`ws on :${WS_PORT}`));
wss.on('connection', ws => {
  ws.on('message', raw => {
    let m: ControlMessage; try { m = JSON.parse(raw.toString()); } catch { return; }
    if (m.type === 'shaper') world.shaper.set(m.config);
    else if (m.type === 'budget') world.budgetBps = m.bps;
    else if (m.type === 'fusion') world.fusion.enabled = m.enabled;
  });
});

setInterval(() => {
  const snap = world.snapshot(Date.now());
  const payload = JSON.stringify({ type: 'snapshot', snap });
  for (const c of wss.clients) if (c.readyState === WebSocket.OPEN) c.send(payload);
}, 1000 / 30);
setInterval(() => {
  const payload = JSON.stringify({ type: 'log', lines: world.log.splice(0) , shaper: { dropped: world.shaper.dropped, passed: world.shaper.passed } });
  for (const c of wss.clients) if (c.readyState === WebSocket.OPEN) c.send(payload);
}, 500);
