// Synthetic edge: runs the real WASM Edge over a scripted scene and sends real UDP datagrams to
// the server, so the full pipeline (shaper, receiver, fusion, viewer) works with no phone.
//   npm run sim                      # one device
//   DEVICES=2 npm run sim            # two devices observing the same walkers (fusion demo)
import dgram from 'node:dgram';
import { WasmEdge, describe } from 'minband-core';
import { TICK_HZ } from './types.js';

const HOST = process.env.MINBAND_HOST ?? '127.0.0.1';
const PORT = Number(process.env.MINBAND_UDP_PORT ?? 7777);
const DEVICES = Number(process.env.DEVICES ?? 1);
const VERBOSE = !!process.env.VERBOSE;

function scene(t: number, deviceId: number) {
  // Two walkers on loops, one static chair, one object that appears periodically. Each device
  // sees the same world with small observation noise and a per-device id space.
  const n = (k: number) => (Math.sin(t * 7.3 + k * 13.1 + deviceId) * 0.02);
  const tracks = [
    { id: 1, class: 0, pos: [3 * Math.cos(t * 0.4), 0, 3 * Math.sin(t * 0.4)], vel: [-1.2 * Math.sin(t * 0.4), 0, 1.2 * Math.cos(t * 0.4)], conf: 230 },
    { id: 2, class: 0, pos: [((t * 0.8) % 8) - 4, 0, 2], vel: [0.8, 0, 0], conf: 200 },
    { id: 3, class: 56, pos: [-2, 0, -2], vel: [0, 0, 0], conf: 180 },
  ];
  if (Math.floor(t / 5) % 2 === 0) tracks.push({ id: 4, class: 41, pos: [1, 0.8, -1 + 0.3 * Math.sin(t)], vel: [0, 0, 0.3 * Math.cos(t)], conf: 150 });
  return tracks.map(tr => ({ ...tr, pos: tr.pos.map((v, i) => v + n(tr.id + i)) }));
}

function unpack(buf: Uint8Array): Uint8Array[] {
  const out: Uint8Array[] = []; let i = 0;
  while (i + 2 <= buf.length) { const n = buf[i] | (buf[i + 1] << 8); i += 2; out.push(buf.subarray(i, i + n)); i += n; }
  return out;
}

for (let d = 0; d < DEVICES; d++) {
  const deviceId = 100 + d;
  const edge = new WasmEdge(deviceId, Math.floor(Math.random() * 2 ** 31));
  const sock = dgram.createSocket('udp4');
  sock.on('message', m => edge.on_datagram(new Uint8Array(m)));
  const t0 = Date.now();
  let tick = 0;
  setInterval(() => {
    const now = Date.now();
    const target = Math.floor((now - t0) / 1000 * TICK_HZ);
    while (tick <= target) {
      const t = tick / TICK_HZ;
      const packed = edge.tick(JSON.stringify(scene(t, deviceId)), tick);
      for (const dg of unpack(packed)) {
        sock.send(dg, PORT, HOST);
        if (VERBOSE) console.log(`[${deviceId}] ${describe(dg)} (${dg.length} B)`);
      }
      tick++;
    }
  }, 1000 / 30);
  setInterval(() => console.log(`[${deviceId}] ${edge.stats_json()}`), 5000);
}
console.log(`sim: ${DEVICES} device(s) -> ${HOST}:${PORT}`);
