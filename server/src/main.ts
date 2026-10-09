import dgram from 'node:dgram';
import http from 'node:http';
import { WebSocketServer, WebSocket } from 'ws';
import { World } from './world.js';
import { createApi, shaperState } from './http.js';
import { parseAnchor } from './geo.js';
import { cotConfigFromEnv, startCot } from './cot.js';
import type { ControlMessage } from './types.js';

const UDP_PORT = Number(process.env.MINBAND_UDP_PORT ?? 7777);
const WS_PORT = Number(process.env.MINBAND_WS_PORT ?? 8080);

const world = new World();
// Geodetic anchor and CoT export (MINBAND_GEO, MINBAND_COT, MINBAND_COT_HZ; see README).
let cotCfg: ReturnType<typeof cotConfigFromEnv>;
try {
  if (process.env.MINBAND_GEO) world.geo = parseAnchor(process.env.MINBAND_GEO);
  cotCfg = cotConfigFromEnv(process.env);
} catch (e) { console.error(`config: ${(e as Error).message}`); process.exit(1); }
console.log(world.geo ? `geo anchor ${JSON.stringify(world.geo)}` : 'geo anchor: none (MINBAND_GEO="lat,lon,headingDeg[,altM]" or /api/geo)');
const cot = startCot(world, cotCfg);
const udp = dgram.createSocket('udp4');
world.onAck = (addr, ack) => {
  const i = addr.lastIndexOf(':');
  udp.send(ack, Number(addr.slice(i + 1)), addr.slice(0, i));
};
udp.on('message', (msg, rinfo) => world.ingest(`${rinfo.address}:${rinfo.port}`, new Uint8Array(msg)));
udp.on('error', e => { console.error(`udp: ${e.message}`); process.exit(1); });
udp.bind(UDP_PORT, () => console.log(`udp ingest on :${UDP_PORT}`));

// One HTTP server for the API and the WebSocket upgrade.
const server = http.createServer(createApi(world, { cot: cotCfg.opts }));
const wss = new WebSocketServer({ server });
server.on('error', e => { console.error(`http/ws: ${e.message}`); process.exit(1); });
server.listen(WS_PORT, () => console.log(`ws + http api on :${WS_PORT}`));
wss.on('connection', ws => {
  ws.on('message', raw => {
    let m: ControlMessage; try { m = JSON.parse(raw.toString()); } catch { return; }
    if (m.type === 'shaper') world.link.manual(m.config ?? {}, m.revertAfterMs); // by hand: profile 'custom' unless timed
    else if (m.type === 'link') world.link.apply(m.profile, m.as); // invalid names: logged, nothing changes
    else if (m.type === 'budget') { const b = Number(m.bps); if (Number.isFinite(b) && b >= 0) world.budgetBps = Math.round(b); }
    else if (m.type === 'fusion') world.fusion.enabled = !!m.enabled;
  });
});

const broadcast = (payload: string) => { for (const c of wss.clients) if (c.readyState === WebSocket.OPEN) c.send(payload); };
const timers = [
  setInterval(() => broadcast(JSON.stringify({ type: 'snapshot', snap: world.snapshot() })), 1000 / 30),
  setInterval(() => {
    const s = shaperState(world);
    broadcast(JSON.stringify({ type: 'log', lines: world.log.splice(0), shaper: s.counters }));
  }, 500),
];

const shutdown = () => {
  timers.forEach(clearInterval); world.link.stop();
  cot?.stop();
  for (const c of wss.clients) c.terminate();
  wss.close(); server.close(); udp.close();
  setTimeout(() => process.exit(0), 200).unref();
};
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
