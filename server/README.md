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
| `MINBAND_GEO` | unset | geodetic anchor `lat,lon,headingDeg[,altM]` of the marker (see [Geodetic anchor and CoT export](#geodetic-anchor-and-cot-export-tak)) |
| `MINBAND_COT` | unset (off) | CoT endpoints, comma-separated: `udp://239.2.3.1:6969` (ATAK SA multicast), `udp://<device-ip>:<port>`, `tcp://<tak-server>:8087`; `udp://...?ttl=2&iface=<local-ip>` for multicast |
| `MINBAND_COT_HZ` | 1 | CoT send rate, (0, 30] |
| `MINBAND_COT_STALE_S` | max(5, 3 / Hz) | validity window of a live CoT event |

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

### `GET /api/geo?lat=&lon=&heading=&alt=` / `?mgrs=&heading=` / `?clear=1`

Reads or sets the geodetic anchor (same as `MINBAND_GEO`). Fields not given keep their current
value, so `?heading=93` alone corrects the heading; `alt=` (empty) makes the altitude unknown;
`mgrs=` takes the centre of the grid square instead of `lat`/`lon`. Invalid or unknown
parameters return 400 and change nothing.

```bash
curl 'localhost:8080/api/geo?lat=37.5665&lon=126.978&heading=90&alt=38'
curl 'localhost:8080/api/geo?mgrs=52SCG2142459640&heading=90'
```

```json
{ "anchor": { "lat": 37.5665, "lon": 126.978, "headingDeg": 90, "altM": 38, "mgrs": "52S CG 21424 59640" } }
```

`{"anchor": null}` when none is set.

### `GET /api/cot`

The CoT events one send round would carry now, as one XML document
(`<events count="n"><event .../>...</events>`; on the wire each event is its own document).
Works with or without `MINBAND_COT`; 409 without an anchor.

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
  `ShaperConfig.burstSec`, `Snapshot.budgetBps / shaperRevertMs`. With a geodetic anchor:
  `Snapshot.geo` = `{lat, lon, headingDeg, mgrs}` of the marker and `GlobalEntity.geo` =
  `{lat, lon, mgrs}` (1 m MGRS, e.g. `52S CG 21434 59641`); both null without one.
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

## Geodetic anchor and CoT export (TAK)

**Anchor.** `lat,lon` of the marker centre (WGS84 degrees) and `headingDeg`, the true bearing
of the marker frame's -Z axis: from the marker centre toward the **top edge of the printed
image** as you read it (+X, the image's right, is then heading + 90; for the manual "set origin
here" fallback, -Z is the direction the phone faced). Optional `altM` is the marker's height
above the WGS84 ellipsoid; without it CoT `hae` and `le` are "unknown" (9999999). To measure,
stand at the marker's bottom edge looking across it toward the top edge, read the bearing from a
compass set to true north (iPhone Compass: Settings -> Compass -> Use True North), and take
lat/lon from the phone at the marker. A few
metres of GPS error shift every entity by the same amount; CoT `ce` does not include it.
Positions go marker -> ENU -> ECEF -> WGS84 (exact for the Cartesian ARKit frame). UTM is the
Krueger series to n^6; MGRS uses the WGS84 lettering with the Norway/Svalbard exceptions and
truncates like GeographicLib. Polar UPS is not implemented (MGRS is then ''). `src/geo.ts` has
the conventions, `test/geo.test.ts` the reference vectors (NGA GEOTRANS, PROJ, GeographicLib).

**Events.** One per fused entity, at `MINBAND_COT_HZ`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<event version="2.0" uid="minband-g1" type="a-u-G" how="m-f" time="2026-10-10T09:00:00.000Z" start="2026-10-10T09:00:00.000Z" stale="2026-10-10T09:00:05.000Z">
  <point lat="37.5665136" lon="126.9779697" hae="38.00" ce="0.35" le="0.35"/>
  <detail><contact callsign="DISMOUNT g1"/><track course="15.8" speed="1.09"/>
    <remarks>MinBand g1: dismount (COCO person), 2 sources. ce 0.35 m is the twin's error bound (grows while the link is silent). Position dead-reckoned between edge updates; affiliation not assessed.</remarks></detail>
</event>
```

(Pretty-printed here; on the wire an event is one line.)

| field | value | why |
|---|---|---|
| `uid` | `minband-<gid>` | stable while fusion keeps the group (gids restart with the server) |
| `type` | `a-u-G` | affiliation unknown, ground; a person is a dismount, not an infantry unit, so no `G-U-C-I` |
| `how` | `m-p`, or `m-f` with >= 2 devices and not coasting | CoT Event.xsd: `p` "predicted - prediction of future (e.g. from a tracker)", `f` "fused - corroborated from multiple sources"; not `m-g` (GPS) |
| `stale` | now if the entity is stale, else now + max(5 s, 3 periods) | an entity that disappears gets one more event with `stale` = now, so TAK greys it out |
| `ce` | `GlobalEntity.ce` (m), 9999999 when unknown | the edge's declared threshold grown with silence; a bound, so conservative as CoT's 1-sigma |
| `le` | `ce` with a known `altM`, else 9999999 | the threshold bounds the 3D error |
| `track` | course (degrees true) and horizontal speed (m/s) | velocity rotated by the heading |

**Viewing it on the same Wi-Fi.**

- ATAK (Android) and WinTAK listen on the SA multicast group by default:
  `MINBAND_COT=udp://239.2.3.1:6969`. Add `?iface=<laptop Wi-Fi IP>` when the laptop has more
  than one interface (e.g. Ethernet to the Pi link box), `?ttl=2` across a router.
- Venue Wi-Fi often drops multicast or isolates clients. Then send unicast to the device,
  `udp://<tablet-ip>:4242` (ATAK's default UDP input; check Settings -> Network Preferences ->
  Manage Inputs, or add one), or to a subnet broadcast address `udp://192.168.1.255:<port>`.
- iTAK, or a shared team picture: send to a TAK Server or FreeTAKServer plain CoT input,
  `tcp://<server>:8087`, and connect the clients to the server. TLS inputs (8089) are not
  supported. The sender reconnects with backoff (1 s to 30 s) and skips rounds while down.
- Several sinks at once: `MINBAND_COT=udp://239.2.3.1:6969,tcp://tak.local:8087`.

```bash
MINBAND_GEO="37.5665,126.978,90,38" MINBAND_COT=udp://239.2.3.1:6969 npm run start
curl -s localhost:8080/api/cot      # what is being sent
```

## Layout

| file | |
|---|---|
| `src/main.ts` | sockets, HTTP + WS server, 30 Hz snapshot / 2 Hz log loops |
| `src/world.ts` | devices, identity, acks, rates, snapshots, metrics |
| `src/clock.ts` | edge-clock offset estimator |
| `src/shaper.ts` | in-process link impairment |
| `src/fusion.ts` | multi-device merge/split with hysteresis |
| `src/groundtruth.ts` | snapshot ring, CSV parsing, twin error |
| `src/geo.ts` | geodetic anchor: marker frame <-> ENU <-> WGS84, UTM, MGRS |
| `src/cot.ts` | CoT events, UDP/TCP senders |
| `src/http.ts` | `/api/*` |
| `src/peek.ts` | datagram kind/id/tick via core's `describe()` (no byte parsing in TS) |
| `src/sim.ts` | synthetic edge(s) |
