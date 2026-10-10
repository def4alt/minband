// P5a: thin Node process that drives the real WASM Edge from edge.py's live track datagrams.
// Closes C4 (the Python edge must not reimplement the MinBand delta/postcard codec) -- this is
// the "thin Node process" named in server/src/sim.ts's own docstring and in the plan. The only
// difference from sim.ts's SCENE=log replay is that the Scene here comes from a live UDP socket
// (edge.py, real time) instead of a scripted function or a replayed CSV file. Pure logic lives in
// edge-feed.ts (unit tested); this file is just the socket/timer wiring, same split as
// scenes.ts/sim.ts.
//
//   TRACK_PORT=7701 DEST=127.0.0.1:7788 npm run edge-bridge
//     # consume edge.py's track datagrams on :7701, drive WasmEdge, send MinBand datagrams into
//     # bridge A's --udp-listen ingress (the real link path).
//   TRACK_PORT=7701 DEST=127.0.0.1:7777 npm run edge-bridge
//     # same, but straight to the MinBand server's UDP ingest -- no bridge/boards, for dev.
import dgram from 'node:dgram';
import { WasmEdge } from 'minband-core';
import { TICK_HZ } from './types.js';
import { EdgeFeed, unpack } from './edge-feed.js';

const TRACK_PORT = Number(process.env.TRACK_PORT ?? 7701);
const [DEST_HOST, destPortStr] = (process.env.DEST ?? '127.0.0.1:7788').split(':');
const DEST_PORT = Number(destPortStr);
const DEVICE_ID = Number(process.env.DEVICE_ID ?? 1);
// Bridge A's own HTTP telemetry, polled locally, as a BACKUP budget source only -- the real,
// authoritative budget comes from the server's own `Ack.budget_bps` (core/src/edge.rs:202, "the
// server's budget is authoritative once it acks"), received on `outSock` below. Confirmed the hard
// way: without listening for the Ack reply at all, `WasmEdge` never leaves its "awaiting first Ack"
// state (core/src/edge.rs:403, "the edge sends nothing but Hello until then") -- an integration
// check against a loopback bridge + real server showed 264 Hello-only datagrams and 0
// deltas/keyframes until this was fixed. So the Ack round trip is NOT optional telemetry, it is
// the protocol handshake. For real board mode, that Ack still has to travel board B -> board A,
// the BLE-credit-starved direction (nucode/README.md) -- small and infrequent (session start +
// budget changes), so acceptable for now, but worth remeasuring once boards are in the loop (P7).
const TELEMETRY_URL = process.env.TELEMETRY_URL; // e.g. http://127.0.0.1:28765/telemetry
const TELEMETRY_POLL_MS = 1000;

// Camera Pose comes from edge.py's own track datagram (its `pose` field, edge/edge.py's
// _PoseTracker). Offered at 2 Hz like the phone does; the core itself decides what actually goes
// on the wire, at the budget's pose interval. Without it the viewer has no frustum for this
// device -- scene.ts only shows one for a device whose snapshot carries a pose.
const POSE_EVERY_TICKS = TICK_HZ / 2;

if (!Number.isFinite(DEST_PORT)) { console.error(`edge-bridge: bad DEST "${process.env.DEST}", want host:port`); process.exit(2); }

const feed = new EdgeFeed();
const inSock = dgram.createSocket('udp4');
inSock.on('message', msg => feed.ingest(msg.toString('utf8'), Date.now()));
inSock.on('error', e => { console.error(`edge-bridge: track socket: ${e.message}`); process.exit(1); });
inSock.bind(TRACK_PORT, () => console.log(`edge-bridge: listening for edge.py track datagrams on :${TRACK_PORT}`));

const outSock = dgram.createSocket('udp4');
const edge = new WasmEdge(DEVICE_ID, Math.floor(Math.random() * 2 ** 31));
// The reply (Ack) to a datagram sent from this socket comes back to this same socket's ephemeral
// port -- bridge.py's Bridge A forwards serial->UDP replies to "the last peer seen" on its
// --udp-listen socket, i.e. back to whichever address last sent it a datagram, same as sim.ts's
// single send+receive socket.
outSock.on('message', buf => edge.on_datagram(new Uint8Array(buf)));

const t0 = Date.now();
let tick = 0, sentBytes = 0;
setInterval(() => {
  const now = Date.now();
  const target = Math.floor((now - t0) / 1000 * TICK_HZ);
  while (tick <= target) {
    const out = unpack(edge.tick(JSON.stringify(feed.currentTracks(now)), tick));
    if (tick % POSE_EVERY_TICKS === 0 && feed.pose) {
      const { pos, quat } = feed.pose;
      const dg = edge.pose(pos[0], pos[1], pos[2], quat[0], quat[1], quat[2], quat[3], true, tick);
      if (dg.length) out.push(dg);
    }
    for (const dg of out) { outSock.send(dg, DEST_PORT, DEST_HOST); sentBytes += dg.length; }
    tick++;
  }
}, 1000 / 30);

if (TELEMETRY_URL) setInterval(() => {
  fetch(TELEMETRY_URL)
    .then(r => r.json())
    .then((j: any) => { const bps = j?.scheduler?.budget_bps; if (Number.isFinite(bps)) edge.set_budget(Math.round(bps)); })
    .catch(() => { /* bridge not up yet, or no scheduler (not a rate-limited run) */ });
}, TELEMETRY_POLL_MS);

setInterval(() => {
  console.log(`edge-bridge: ${feed.size} live track(s), tick=${tick}, ${sentBytes} B sent last 5s`);
  sentBytes = 0;
}, 5000);

console.log(`edge-bridge: device ${DEVICE_ID}, track datagrams on :${TRACK_PORT} -> MinBand datagrams to ${DEST_HOST}:${DEST_PORT}` +
  (TELEMETRY_URL ? ` (budget via ${TELEMETRY_URL})` : ' (no --telemetry-url: budget unlimited)'));
