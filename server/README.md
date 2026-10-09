# minband-server

UDP ingest from edges, per-device WASM `Receiver`, acks/nacks, world model + fusion, in-process
link shaper with named link profiles and time on air, packet events, metrics, WebSocket fan-out
and an HTTP API for tools.

```bash
npm install
npm run start          # UDP :7777, HTTP + WebSocket :8080
npm run dev            # same, restart on change
npm run sim            # synthetic edge(s) -> UDP :7777 (DEVICES=2 for the fusion demo)
SCENE=spread DEVICES=8 npm run sim   # eight independent one-walker feeds (drones per link)
npm test               # unit tests + WASM golden test
npm run typecheck
```

| env | default | |
|---|---|---|
| `MINBAND_UDP_PORT` | 7777 | edge datagrams in, acks out (same socket) |
| `MINBAND_WS_PORT` | 8080 | HTTP API and WebSocket on one server |
| `MINBAND_BASELINE_A` | `$MINBAND_RUNS/baseline_a.json`, else `<repo>/runs/baseline_a.json` | measured H.264 table (see `/api/baseline-a`) |

Sim only: `DEVICES` (1), `SCENE` (`shared`: every device sees the same walkers, the fusion demo;
`spread`: device d sees its own walkers in its own 10 m area, so N devices are N independent
feeds), `WALKERS` (1, spread only; each follows the eval's `one_walker` tour, ~120-130 B/s per
walker on the wire), `MINBAND_HOST` (127.0.0.1), `VERBOSE`, `GT_POST_MS` (5000; 0 disables
ground-truth upload), `GT_WINDOW_S` (10), `MINBAND_API` (`http://$MINBAND_HOST:8080`). Device ids
are 100 + d. Every 5 s the sim prints B/s on the wire and the edge stats per device, plus the total.
The sim uploads the last `GT_WINDOW_S` of the scene it fed the edge every `GT_POST_MS`, so the
twin-error metric and the viewer readout work without a phone.

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
    "airtimeShare": 0.078,           // channel time its delivered uplink used over the 2 s window, current model (0 = no model)
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
    "config": { "bps": 0, "delayMs": 0, "loss": 0, "enabled": false, "burstSec": 0.5, "queue": 0 },
    "counters": { "offered": 0, "offeredBytes": 0, "passed": 0, "passedBytes": 0, "delivered": 0, "deliveredBytes": 0,
                  "dropped": 0, "droppedBytes": 0, "droppedLoss": 0, "droppedCap": 0, "droppedQueue": 0, "inFlight": 0 },
    "revertInMs": null,              // ms until a timed override reverts
    "capacityBytes": null            // token bucket depth, null when uncapped
  },
  "budgetBps": 0,                    // link budget
  "edgeBudgetBps": 0,                // what each edge is told: budgetBps / live devices (see /api/budget)
  "link": { /* LinkView, as GET /api/link */ },
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
| `queue` | >= 0 | max datagrams held in the delay line, like netem `limit`; 0 = unbounded |
| `revertAfterMs` | 1..3600000 | apply for this long, then restore the previous config |

```bash
curl 'localhost:8080/api/shaper?enabled=1&bps=2000&loss=0.3'          # jammed-ish
curl 'localhost:8080/api/shaper?enabled=1&loss=1&revertAfterMs=10000'  # 10 s blackout
curl 'localhost:8080/api/shaper?enabled=0'                             # clean
```

Order per datagram: loss, queue limit, token bucket, delay. A datagram is admitted while the
bucket is positive and may leave it in debt (long-run rate is exactly `bps`; admission does not
depend on size, so keyframes do not starve behind small deltas). The queue limit counts datagrams
still in the delay line, as netem's `limit` does, so it caps the link at `queue / delay`
datagrams/s (`hf`: 8 / 0.5 s = 16/s) whatever their size. Acks are only generated for datagrams
that got through. Any explicit change ends a pending timed override first.

A change with any field sets the link profile to `custom` (the airtime model is kept: the radio did
not change, its impairment did) and stops the `contested` loop. A timed override
(`revertAfterMs`, the viewer's blackout preset) keeps the profile, since the shaper restores the
profile's config afterwards; under `contested` the next phase switch ends it early.

### `GET /api/budget?bps=`

Link budget in bits/s (0 = unlimited). Every ack carries `budgetBps` split evenly over the devices
heard in the last 5 s (`edgeBudgetBps` in metrics): an edge under its budget tightens its
thresholds toward the floor (one walker told 8 kbit/s sends ~330 B/s instead of ~130), so N edges
each told the whole budget would oversubscribe a shared link N times. One device: the whole
budget, as before. Returns `{budgetBps}`. Applying a link profile sets it too.

### `GET /api/link?profile=<name>[&as=<name>]`

Applies a named link profile and returns the `LinkView`; no parameters: read only. 400 for an
unknown profile or parameter, or `as` without `profile=external`; nothing changes then. The table
is the Pi link box table (`docs/HACKATHON_PLAN.md` 3.3) and lives in `src/link.ts`:

| profile | rate | delay | loss | queue | edge budget | airtime model | stands for |
|---|---|---|---|---|---|---|---|
| `clean` | | | | | 0 | none | Wi-Fi reference (shaper off) |
| `degraded` | 64 kbit/s | 20 ms | 2 % | 20 | 0 | none | Busy mesh |
| `hf` | 9600 bit/s | 500 ms | 1 % | 8 | 8000 | serial 9600, 10 bit/B, +2 B | NATO HF ceiling |
| `lora` | 2 kbit/s | 300 ms | 10 % | 4 | 1500 | LoRa LongFast | Meshtastic-class LoRa |
| `telemetry` | 600 bit/s | 50 ms | 5 % | 4 | 450 | serial 600, 10 bit/B, +2 B | ELRS-class control-link telemetry |
| `contested` | `lora`, alternating with blackouts | | | | 1500 | LoRa LongFast | Intermittent jamming |
| `blackout` | | | 100 % | | 0 | none | Link cut |

- Applying a profile sets the shaper (`enabled`, `bps`, `delayMs`, `loss`, `queue`; `burstSec`
  back to 0.5), the link budget and the airtime model. `clean` turns the shaper off.
- `contested` runs a timer loop on the server: `lora` for 3-8 s, then 100 % loss for 1-5 s
  (uniform, `CONTESTED` in `src/link.ts`), until another profile or a hand-made shaper change.
- `external`: the Pi link box shapes. Shaper off; budget and airtime model from the profile named
  by `as` (default `clean`); no `contested` loop (the box runs it).
  `curl 'localhost:8080/api/link?profile=external&as=lora'`.
- `custom`: the shaper was changed by hand (`/api/shaper`, viewer sliders).

```jsonc
{
  "profile": "lora",                 // or "custom", "external"
  "as": "lora",                      // only with "external"
  "model": { "kind": "lora", "sf": 11, "bwHz": 250000, "cr": 5, "preamble": 16, "crc": true,
             "explicitHeader": true, "lowDataRateOptimize": false, "overheadBytes": 0 },
  "airtimeShare": 1.04,              // uplink channel time per second, all devices, 2 s window (can exceed 1)
  "msgsPerSec": 2.1,                 // uplink datagrams/s delivered, all devices
  "downAirtimeShare": 0.56,          // acks, same model (a half-duplex radio shares the channel)
  "downMsgsPerSec": 1.9,
  "contested": { "blackout": false, "switchInMs": 2700 },   // only while profile is contested
  "profiles": [ /* LinkProfile[], the table above */ ]
}
```

**Time on air.** Per delivered datagram, from its payload (the MinBand message) plus the model's
`overheadBytes` of radio framing. The 28 B UDP/IP header is not on air: a LoRa or serial radio
carries the payload in its own frame, the IP header only exists on the Wi-Fi/Ethernet hops of the
emulation (the shaper, the bytes graph and packet events still count it, since the emulated link
carries it). Counted from what arrives, so under loss it is a lower bound.

- `lora`: Semtech time-on-air formula (SX127x, SF7-12) with Meshtastic LongFast defaults: SF11,
  250 kHz, CR 4/5 (`cr` is the denominator, 5..8), 16-symbol preamble, explicit header, CRC on,
  LDRO off; raw LoRa PHY (`overheadBytes` 0; a Meshtastic transport would add its 16 B header).
  16 B = 354.3 ms, 34 B (a one-walker delta) = 518.1 ms, 100 B = 1009.7 ms.
- `serial` (`hf`, `telemetry`): `(payload + 2) x 10 bits / rate`: a UART at the link rate with
  8N1 framing and two SLIP-style delimiters per datagram (a synchronous HF modem would be 8 bits
  per byte).

Reference load, one walker through the edge (`SCENE=spread`, 120 s, acks on, budget 0): 2.1
datagrams/s, 129 B/s on the wire, mean payload 34 B. Uplink airtime: `hf` 7.8 %, `lora` /
`contested` 104 %, `telemetry` 124 % (acks add 1.5 %, 56 %, 24 %). On LoRa the binding limit is
airtime, not the 2 kbit/s rate (52 % of it).

### `GET /api/baseline-a`

Baseline A (H.264 720p/480p/360p) as served in `Snapshot.baselineA`:
`{ "baselineA": [{ "id": "h264_720p", "label": "H.264 720p", "bps": 1500000, "measured": false, "source": "configured, ..." }, ...], "file": "/.../runs/baseline_a.json", "error": null }`.
Configured numbers until `runs/baseline_a.json` exists (`MINBAND_BASELINE_A`); same format and
merge rule as `tools/eval` (`loadBaselineA`): `{"entries": [{"id": "h264_720p", "bps": 1234567,
"label"?, "source"?}]}`, entries replace the configured one with the same id (`measured: true`),
new ids are appended. The file is re-read when it changes (checked at most every 2 s; this
endpoint checks at once); a malformed file falls back to the configured table and reports
`error`. `Snapshot.baselines.h264_720p_bps / h264_480p_bps` (legacy, read by the current viewer)
come from the same table.

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
  `ShaperConfig.burstSec`, `Snapshot.budgetBps / shaperRevertMs`. Added for the hackathon:
  `Snapshot.link` (LinkView, as `/api/link`), `Snapshot.packets`, `Snapshot.baselineA`,
  `DeviceView.airtimeShare`, `ShaperConfig.queue`.
  `packets` is every datagram since the previous snapshot (V3): each one arriving at the socket
  (`dir: "up"`, including those the shaper drops, `dropped: true`; the reason is in the shaper
  counters) and each ack sent (`dir: "down"`), as
  `{t, dir, key, kind, bytes, seq?, ids?, dropped}`: `t` server ms at arrival/send, `key` the
  device's render key (`''` if unattributed), `bytes` incl. 28 B UDP/IP, `seq` for
  delta/keyframe/pose/bye, `ids` once core's `peek_json` provides them. At most 2000 are kept
  between snapshots (oldest dropped).
- `{"type":"log","lines":[...],"shaper":ShaperCounters}` at 2 Hz (`shaper.dropped/passed` kept;
  the other counters were added).

Viewer -> server:

- `{"type":"shaper","config":Partial<ShaperConfig>,"revertAfterMs"?:number}` (partial merge;
  `revertAfterMs` added in M5)
- `{"type":"budget","bps":number}`
- `{"type":"fusion","enabled":boolean}`
- `{"type":"link","profile":string,"as"?:string}` (as `/api/link`; invalid names are logged and
  change nothing). A `shaper` message with any field makes the profile `custom` unless it carries
  `revertAfterMs`.

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
| `src/link.ts` | link profiles, airtime models (LoRa, serial), `contested` loop |
| `src/baseline.ts` | Baseline A table from `runs/baseline_a.json` |
| `src/fusion.ts` | multi-device merge/split with hysteresis |
| `src/groundtruth.ts` | snapshot ring, CSV parsing, twin error |
| `src/http.ts` | `/api/*` |
| `src/peek.ts` | datagram kind/id/tick via core's `describe()` (no byte parsing in TS) |
| `src/sim.ts` | synthetic edge(s) |
| `src/scenes.ts` | sim scenes (`shared`, `spread`) |
