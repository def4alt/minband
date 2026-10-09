# minband-server

UDP ingest from edges, per-device WASM `Receiver`, acks/nacks, world model + fusion, in-process
link shaper, metrics, WebSocket fan-out and an HTTP API for tools.

```bash
npm install
npm run start          # UDP :7777, HTTP + WebSocket :8080
npm run dev            # same, restart on change
npm run sim            # synthetic edge(s) -> UDP :7777 (DEVICES=2 for the fusion demo)
npm test               # unit tests + WASM golden test
npm run typecheck
```

| env | default | |
|---|---|---|
| `MINBAND_UDP_PORT` | 7777 | edge datagrams in, acks out (same socket) |
| `MINBAND_WS_PORT` | 8080 | HTTP API and WebSocket on one server |

Sim only: `DEVICES` (1), `MINBAND_HOST` (127.0.0.1), `VERBOSE`, `GT_POST_MS` (5000; 0 disables
ground-truth upload), `GT_WINDOW_S` (10), `MINBAND_API` (`http://$MINBAND_HOST:8080`). The sim
uploads the last `GT_WINDOW_S` of the scene it fed the edge every `GT_POST_MS`, so the twin-error
metric and the viewer readout work without a phone.

## HTTP API (port 8080)

All responses are JSON with `access-control-allow-origin: *`. Errors: `{"error": "..."}` with
400 (bad parameter), 404 (unknown path or device), 405, 413 (body over 64 MB).

### `GET /api/metrics`

```jsonc
{
  "t": 1791527489007,               // server ms
  "uptimeS": 12.3,
  "entityCount": 4,                  // fused (global) entities in the last 30 Hz snapshot
  "deviceEntityCount": 8,            // sum over devices
  "devices": [{
    "key": "id:100",                 // "addr:<ip:port>" while provisional (no Hello seen)
    "deviceId": 100,                 // null while provisional
    "provisional": false,
    "addr": "127.0.0.1:64165",       // where acks go
    "aliases": [],                   // earlier addresses still routed to this device
    "bps": 1888,                     // bits/s delivered to the receiver (after the shaper), 2 s window, incl. 28 B UDP/IP
    "offeredBps": 1888,              // bits/s arriving at the socket (before the shaper)
    "msgsPerSec": 3.0,
    "entities": 4, "staleEntities": 0,
    "stats": { "datagrams": 37, "bytes": 1420, "deltas": 33, "keyframes": 2, "poses": 0,
               "gapsDetected": 0, "nacksSent": 0, "outOfOrderDropped": 0 },   // core Receiver stats, passed through
    "lastSeenMs": 1791527489001, "silentMs": 6, "silent": false,              // silent: > 5 s without a datagram
    "edgeTick": 3162,                // edge tick the twin currently extrapolates to
    "lastEdgeTick": 3160,            // newest tick received
    "clock": { "offsetMs": 1791527483988, "windowMinMs": 1791527483988, "samples": 37, "steps": 0 },
    "addrChanges": 0, "sessions": 1,
    "twinError": { "meanM": 0.056, "p95M": 0.11, "samples": 2392, "rows": 2392, "skipped": 0, "missing": 16, "updatedMs": 1791527489007 }
  }],
  "unattributedBps": 0,              // datagrams from addresses no device owns yet
  "shaper": {
    "config": { "bps": 0, "delayMs": 0, "loss": 0, "enabled": false, "burstSec": 0.5 },
    "counters": { "offered": 0, "offeredBytes": 0, "passed": 0, "passedBytes": 0, "delivered": 0, "deliveredBytes": 0,
                  "dropped": 0, "droppedBytes": 0, "droppedLoss": 0, "droppedCap": 0, "inFlight": 0 },
    "revertInMs": null,              // ms until a timed override reverts
    "capacityBytes": null            // token bucket depth, null when uncapped
  },
  "budgetBps": 0,
  "fusion": true,
  "twinError": { "meanM": 0.056, "p95M": 0.11, "samples": 4784 }   // over all devices' latest uploads; nulls when none
}
```

### `GET /api/shaper?enabled=&bps=&delayMs=&loss=&burstSec=&revertAfterMs=`

Changes only the given fields and returns `{config, counters, revertInMs}`. No parameters: read
only. Invalid or unknown parameters return 400 and change nothing.

| param | range | |
|---|---|---|
| `enabled` | `0/1/true/false/on/off` | off = everything passes untouched |
| `bps` | >= 0 | link capacity incl. 28 B UDP/IP per datagram; 0 = uncapped |
| `delayMs` | 0..60000 | fixed one-way delay, FIFO (never reorders) |
| `loss` | 0..1 | Bernoulli drop probability (a fraction, not percent) |
| `burstSec` | 0.01..10 | token bucket depth in seconds of `bps` (default 0.5) |
| `revertAfterMs` | 1..3600000 | apply for this long, then restore the previous config |

```bash
curl 'localhost:8080/api/shaper?enabled=1&bps=2000&loss=0.3'          # jammed-ish
curl 'localhost:8080/api/shaper?enabled=1&loss=1&revertAfterMs=10000'  # 10 s blackout
curl 'localhost:8080/api/shaper?enabled=0'                             # clean
```

Order per datagram: loss, token bucket, delay. A datagram is admitted while the bucket is
positive and may leave it in debt (long-run rate is exactly `bps`; admission does not depend on
size, so keyframes do not starve behind small deltas). Acks are only generated for datagrams that
got through. Any explicit change ends a pending timed override first.

### `GET /api/budget?bps=`

Byte budget pushed to every edge in `Ack.budget_bps` (0 = unlimited). Returns `{budgetBps}`.

### `GET /api/fusion?enabled=0|1`

Multi-device fusion on/off. Returns `{fusion}`.

### `POST /api/ground-truth?deviceId=<u32>`

Body: CSV `tick,id,class,x,y,z,vx,vy,vz,conf` (header optional, `#` comments ignored), ticks of
1/120 s on that device's edge clock, ids as sent by the edge. This is the phone's ground-truth
log format (`ios/MinBand/GroundTruthLog.swift`).

The server keeps, per device, the last 60 s of what the twin served at 30 Hz (edge tick ->
extrapolated entity positions). Each row is compared with the snapshot closest in tick: 3D
distance, or 2.0 m if the twin had no entity with that id. Rows more than 8 ticks from any
snapshot (outside the recording) are skipped. The result replaces that device's previous
`twinError` and is returned:

```json
{ "deviceId": 100, "malformed": 0, "meanM": 0.067, "p95M": 0.203, "samples": 273,
  "rows": 273, "skipped": 0, "missing": 0, "window": [5, 3162] }
```

404 if the device has never been seen. Upload before restarting the edge: a new session clears
the device's recording (ticks restart at 0).

## WebSocket (port 8080, same server)

Server -> viewer:

- `{"type":"snapshot","snap":Snapshot}` at 30 Hz (`src/types.ts`). Added in M5:
  `DeviceView.key / provisional / offeredBps / edgeTick / silent / addrChanges / clockOffsetMs`,
  `ShaperConfig.burstSec`, `Snapshot.budgetBps / shaperRevertMs`.
- `{"type":"log","lines":[...],"shaper":ShaperCounters}` at 2 Hz (`shaper.dropped/passed` kept;
  the other counters were added).

Viewer -> server:

- `{"type":"shaper","config":Partial<ShaperConfig>,"revertAfterMs"?:number}` (partial merge;
  `revertAfterMs` added in M5)
- `{"type":"budget","bps":number}`
- `{"type":"fusion","enabled":boolean}`

## Behaviour notes

- **Device identity**: keyed by `device_id` after `Hello`, by address before. A known id saying
  `Hello` from a new address moves there with its state; acks go to the newest address. An edge
  that is already acked never repeats `Hello`, so an unknown address with no `Hello` is adopted
  by an identified device that went quiet (>= 500 ms) when the new address appeared and whose
  tick stream it continues (within 1 s); ambiguous cases stay separate (`src/world.ts`). After a
  server restart, edges that were already acked show up as provisional (`dev ?`) until they
  restart their session.
- **New session** (`Hello` with a new nonce): fresh receiver and clock estimate. The core
  `Receiver` keeps its old `last_edge_tick` across a new `Hello`, so reusing it would mix tick
  bases.
- **Clock**: `src/clock.ts`, see DESIGN.md section 4.
- **Staleness**: core marks entities stale after 6 s without refresh; the server also marks every
  entity of a device stale after 5 s without any datagram, and removes the device after 30 s.

## Layout

| file | |
|---|---|
| `src/main.ts` | sockets, HTTP + WS server, 30 Hz snapshot / 2 Hz log loops |
| `src/world.ts` | devices, identity, acks, rates, snapshots, metrics |
| `src/clock.ts` | edge-clock offset estimator |
| `src/shaper.ts` | in-process link impairment |
| `src/fusion.ts` | multi-device merge/split with hysteresis |
| `src/groundtruth.ts` | snapshot ring, CSV parsing, twin error |
| `src/http.ts` | `/api/*` |
| `src/peek.ts` | datagram kind/id/tick via core's `describe()` (no byte parsing in TS) |
| `src/sim.ts` | synthetic edge(s) |
