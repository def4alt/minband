# MinBand design v2: feature extraction on the edge, contact reports over a trickle link

Status: 2026-10-10, decided in the pivot interview. Supersedes `DESIGN.md` for the direction;
v1 code stays as the fallback demo. Wire format: `proto/PROTOCOL_V2.md`. Hardware survey:
`EDGE_HARDWARE.md`.

## 1. What changed and why

v1 sends a state delta whenever the receiver's dead reckoning of an entity would be off by a few
centimetres. That is the right idea for a lab scene and the wrong scale for a drone: the sensor's
own geolocation error is 5-10 m, the link is 100 B/s to 1 kbit/s, the operator cannot fly after the
cut-off (the drone is on autopilot), and a jammed link drops out for seconds to minutes with no
guarantee the uplink ever comes back. What a commander needs from that link is not where each
object is to the centimetre but **what is there, is it new, is it moving, where to, how sure are
we, and what is the drone itself doing**.

Decisions from the interview (user's words paraphrased):

| topic | decision |
|---|---|
| platform | one sensor set common to drones and phones, so it is pitched for drones and tested on phones: camera, IMU, barometer, heading, GNSS when present; no rangefinder or gimbal assumed |
| consumer | observability for a commander (Delta/ATAK), with an operator-driven *focus* on individual contacts at a higher rate; never targeting |
| link | continuous trickle, ~100 B/s to 1 kbit/s, downlink first; uplink optional |
| what is sent | egomotion, new objects, object motion; the edge decides *when*: static things rarely, focused things first |
| aggregation | groups by default, individuals only under focus |
| evidence | image chips only on operator request |
| durability | researched below; stream must be reconstructable under heavy loss |
| receiver | current Three.js twin, top-down, geodetic grid, CoT to ATAK |
| demo | replay the real drone footage tracks with a simulated drone pose |
| scope | full redesign now, judging shows whatever works; protocol and event model first |

## 2. The common sensor set and what each sensor buys

| sensor | drone source | phone source | used for |
|---|---|---|---|
| camera (RGB or thermal) | video feed or SDK frames | AVFoundation / ARKit | detection, tracking, visual odometry |
| IMU | flight controller attitude | CoreMotion attitude | camera ray direction (pitch, roll, yaw) |
| barometer | FC altitude above take-off | CMAltimeter relative altitude | height above ground for the ray-to-ground intersection |
| heading | FC yaw (mag + GNSS course) | CoreMotion heading | course of the ray, course of movers |
| GNSS | FC fix, quality flags | CoreLocation, accuracy | own position; session origin; UTC |
| camera intrinsics | known model (FOV) | ARKit intrinsics | pixel -> ray |
| optional, declared in `Session.caps` | rangefinder, gimbal angles, RTK, thermal | LiDAR (indoor only) | tighter `ce`; the protocol does not depend on them |

When GNSS goes (jamming or spoofing), own position comes from visual odometry (ARKit on the phone;
the homography chain in `tools/footage/track.py` on footage; a VIO on a companion computer) and
every `ce` grows with the drift. The receiver sees it in `Ego.nav.gnss` and in the `ce` bytes, and
says so.

## 3. Edge pipeline (what runs on the drone or phone)

```
frames ──► detector (YOLO, 5-15 Hz) ──► 2D tracks (KF, re-acquisition)
   │                                          │
   └──► registration / odometry               ▼
           (homography, VIO)        ray-to-ground geolocation ─► tracks in metres ENU + ce
                                              │
                                              ▼
                                     contact manager
                                     • motion state machine per track
                                     • grouping into contacts (count, class mix, extent)
                                     • first_seen / since bookkeeping
                                     • change detection against the last *sent* record
                                              │
                                              ▼
                              scheduler (ladder, floor, focus, budget) ─► frame builder ─► link
                                              ▲
                                       uplink: Digest, Focus, Clock
```

Everything from "tracks in metres" down is the deterministic Rust core (uniffi for iOS, WASM for
Node and the browser, plain C ABI later for a flight controller). Detector and tracker stay
platform code (CoreML on the phone, ONNX Runtime on a companion computer, the Python pipeline on
footage).

### 3.1 Geolocation and the honest error radius

Bottom-centre of the box (feet, tyres) -> ray in the camera frame via intrinsics -> rotate by the
camera attitude -> intersect the ground plane at `-h_agl` -> add own position. Per contact at
ground range `R` and depression angle `θ`:

```
ce² = σ_own²  +  (R · σ_att)²  +  (σ_h / tan θ)²  +  (σ_px · R / f)²
```

`σ_own` from the GNSS accuracy (or the odometry drift estimate), `σ_att` from the IMU/mag
(1-2° consumer, 0.3° a good FC), `σ_h` from the barometer (1-3 m), `σ_px` the box-foot error in
pixels. At 100 m AGL, 45°, consumer parts this is 5-8 m; at 20° depression 15 m; with a rangefinder
the `σ_h / tan θ` term disappears. The edge sends `ce` as an `m8`, rounded up. **`ce` is the unit
of "surprise":** a contact is revised when it has moved more than its own `ce`, never for less.

### 3.2 Motion state machine (per track, then per contact)

```
            first look              >= 3 looks or 2 s
  (none) ───────────► new ─────────────────────────► confirmed
                                                         │
     speed > 0.7 m/s for 2 s ─────► moving ◄──┐          │
     speed < 0.3 m/s for 5 s ─────► stopped ──┘ (30 s) ─► static
                                                         │
     no look for 5 s ───────────► lost ──(60 s)──► departed (tombstone, 2 x T_floor)
     a look again ◄───────────────┘  (re-acquired: same id, `since` restarts)
```

Thresholds are class-aware (a vehicle "moving" at 0.7 m/s is creeping; a dismount at 0.3 m/s is
standing). `since` is the start of the current state, `first_seen` never changes, so the receiver
derives "started moving at 14:02:10" even if it only hears the record at 14:03.

### 3.3 Grouping

Single-linkage clustering on ground positions every tick:

- link distance `d = max(15 m, 2 · ce)`; two moving tracks also need speeds within 1.5 m/s and
  courses within 30°; static tracks cluster by distance only;
- hysteresis: a group holds while members stay within `1.5 d`; a member outside for 3 s splits off;
- id: the group keeps the id of its oldest member group; a split child that was never a group gets
  a new id with `parent` set; ids are never reused in a session;
- fields: centroid, extent `radius` (max member distance from centroid), class mix counts (dismount
  / vehicle / armour / other), `count` = sum, `conf` = mean of members, `first_seen` = min,
  motion state = majority by count, course/speed = mean of moving members;
- a single object is a contact of count 1; there is one record type.

Under `Focus(split)` the edge also emits the members as child records (`ext.bit5`, `parent`),
on their own ladders; without focus a 12-vehicle convoy is one 25-byte record.

### 3.4 Egomotion

`Ego` carries own position, altitude, heading, speed, climb, nav mode, GNSS state, link state,
battery, own `ce`, the camera footprint (centre and radius on the ground) and the scene counts.
The footprint is the negative information: a commander who sees the drone looked at a tree line
for 40 s and reported nothing knows something. Nav mode and link state are what the pilot cannot
see once video is gone and are sent at once when they change.

## 4. Durability: research and decision

The question the user asked me to settle: how should the stream be reconstructable under jamming,
complexity no object. The loss model is bursty (jamming windows of seconds to minutes), with a few
percent random loss underneath, and the uplink may be dead for the whole flight.

| approach | what it buys | what it costs | fit |
|---|---|---|---|
| **Repetition of self-contained state** (ADS-B squitters every 0.5 s with ±200 ms jitter, AIS SOTDMA every 2-10 s by dynamics, DIS heartbeat 5 s) | survives *any* loss pattern and needs no uplink; newer copies supersede older; receivers can join mid-stream | bandwidth: a copy per interval whether anything changed or not | the base layer, but the interval must adapt |
| **Trickle (RFC 6206)**: interval doubles while consistent, resets on inconsistency, transmissions suppressed when peers are heard to agree | "a few packets per hour when nothing changes, milliseconds when something does"; 50-200 lines of C | needs a definition of *consistent* | gives the adaptive interval: per record, doubling after a change, suppressed by a digest |
| **State-based / delta-state CRDT** (Almeida, Shoker, Baquero 2014) | idempotent, commutative, order-free merge; converges as long as every delta eventually arrives; a Chalmers field study on mine vehicles found delta-state viable over low bandwidth and plain state-based not | delta-state alone is "more vulnerable to message loss" and needs an anti-entropy round | the merge rule: LWW per contact by `(rev, tick)`; the anti-entropy round is the ladder plus the optional digest |
| **Acks/nacks + state repair** (v1) | cheapest when loss is low | needs the uplink to recover; useless simplex | demoted to suppression (`Digest`) |
| **Erasure coding**: block-interleaved MDS or fountain codes (RaptorQ RFC 6330: decode failure < 1 % at K symbols, < 1e-6 at K+2) | bandwidth-efficient against *random* loss; recovers a block without feedback | a burst longer than the coding block is not recovered; latency and memory grow with the block; interleaving spreads losses but does not replace redundancy | wrong tool for *state* (a newer state supersedes the lost one anyway); right tool for *bulk* objects: image chips |
| **High-latency telemetry** (MAVLink `HIGH_LATENCY2`: one ~100 B heartbeat per 5 s, Iridium SBD: one 340 B message per ~10 s, APRS/ADS-B position reports) | proves that one self-contained message per few seconds is what every low-rate link ends up doing | nothing beyond the obvious | the shape of `Ego` and `Session` |

Decision, in layers (each is independently testable):

1. **Self-contained idempotent records** with the history inside (`first_seen`, `since`) and a
   per-record revision. LWW merge. This alone makes reordering, duplication and any loss pattern
   harmless to *correctness*; loss only costs *latency*.
2. **Per-record Trickle**: a changed record repeats on a doubling ladder (0, 2, 6, 14, 30 s × f),
   then at a floor (60 s × f). Three copies in the first six seconds beat 70 % random loss; the
   spread covers jamming bursts; the floor bounds convergence after any blackout. Static records
   cost the floor only.
3. **Suppression by digest** when an uplink exists: records the receiver confirms jump to the
   floor at once (Trickle's "heard a consistent message"). Simplex links just pay the ladder.
4. **Tombstones** ride the ladder and live 2 × floor, so appear-and-depart during a blackout
   survives up to ~3 minutes at 100 B/s; beyond that the edge's on-device log is the record.
5. **Fountain code for chips** (systematic RaptorQ, `raptorq` crate, K + 2 for 1e-6 failure): the
   only bulk object, and the only place block coding beats repetition.
6. **Frame size follows the link**: about one second of link time per frame, so a lost frame costs
   a second and a serial carrier's CRC rejects a corrupted one without losing more.

What this does *not* do, and why: no FEC across contact records (bursts defeat it and state
supersession makes it redundant); no retransmission queue (state repair only, as v1 already
decided); no encryption or authentication (out of scope for the hackathon; a real link encrypts
below us). Under a jammer that targets the frame timing, randomise the frame builder's slot by
±20 % like ADS-B; the design already tolerates it.

## 5. Bandwidth, measured against surprise

The v1 pitch line holds, at the right scale: bytes are spent only when a contact's state machine
moves or its centroid leaves its own error circle, plus a floor that is 16 % of a 100 B/s link for
a 20-contact static scene (`PROTOCOL_V2.md` §7). A convoy of twelve is one record. A focused
individual costs about 25 B/s. A chip costs a quarter of the link for half a minute and the
operator chose to pay it.

## 6. Receiver and viewer

The server ingests v2 frames through the WASM core (`Receiver2`: merge, derived events, dead
reckoning, liveness), fuses several edges' contacts as before (same class mix, within the sum of
their `ce`), and exports CoT exactly as `PROTOCOL_V2.md` §10. The viewer stays the Three.js
top-down twin: a drone glyph with heading and footprint circle, contacts as counted blobs sized by
`radius` and ringed by `ce_shown`, children under focus, colour by motion state, grey when
unheard, dashed when lost, "k of n known" when incomplete; an event strip (new / moving / lost /
departed with event times); click a contact -> `Focus`. Lat/lon and MGRS readout from `Session`.

## 7. The on-device log

The edge appends every contact revision and `Ego` to a local file (CSV now, SQLite later). It is
the complete record for after the flight, the ground truth for evaluation, and the source for a
store-and-forward upload when a fat link appears (landing, LTE). The wire protocol never depends
on it.

## 8. Demo for 11 Oct (footage replay)

1. `server/src/sim.ts` gains a `--v2` path: `tracks.csv` (metres, camera-ground frame) from
   `runs/footage/<clip>/` plus a simulated drone: the fitted camera height and pitch from
   `summary.json`, a chosen origin lat/lon and heading (any field; the MEVA site is Muscatatuck,
   39.35 N, 85.70 W), a slow orbit or hover, battery draining, GNSS flag togglable from the API.
2. The WASM `Edge2` turns tracks into contacts and frames; the existing shaper or the Pi link box
   applies `hf` / `lora` / `telemetry` / `contested` / `blackout`.
3. Viewer shows groups and events; pull the cable: contacts grey out, circles grow, "k of n known";
   plug it back: the ladder refills the picture within one floor, the event strip back-fills with
   the right times. Click a group: it splits into vehicles and (if frames are available for the
   clip) a chip arrives over ~25 s.
4. Numbers on the slide: bytes/s floor, bytes per event, time-to-complete-picture after a 60 s
   blackout, at each profile. Measured by `tools/eval` from the sim's log against the receiver's log.

MEVA (CC-BY-4.0) frames may be shown; battlefield clips: numbers only, as before.

## 9. Metrics (replace the twin-error curve)

| metric | definition |
|---|---|
| picture completeness | fraction of the edge's live contacts the receiver holds at their current revision, over time |
| event latency | time from a state-machine transition on the edge to its derivation on the receiver, per link profile and loss rate |
| recovery time | after a blackout of B seconds, time until completeness returns to 1 |
| honesty | fraction of contacts whose true position lies within `ce_shown` (target >= 0.68 for 1 sigma) |
| bytes | B/s on the wire by record type; bytes per event |
| focus cost | B/s added per focused contact; chip delivery time |

## 10. Work split (team builds against the spec)

| part | where | owner |
|---|---|---|
| spec, event model, this doc | `proto/PROTOCOL_V2.md`, `docs/DESIGN_V2.md` | Claude (done) |
| core: `wire2.rs` codec + peek/describe, golden vectors | `core/src/` | |
| core: `contacts.rs` (state machine, grouping, change detection), `scheduler.rs` (ladder, floor, focus, budget, frame builder), `receiver2.rs` (merge, derived events, liveness, dead reckoning), `geo.rs` (ray-to-ground, ce) | `core/src/` | |
| core: chips (`raptorq`) behind a feature flag | `core/src/chips.rs` | stretch |
| server: v2 ingest, fusion on contacts, CoT mapping, `/api/focus`, event log | `server/src/` | |
| server: footage replay with simulated drone (`--v2`) | `server/src/sim.ts`, `scenes.ts` | |
| viewer: drone glyph, footprint, group blobs, ce rings, event strip, focus click, k-of-n | `viewer/src/` | |
| eval: completeness, event latency, recovery, honesty | `tools/eval` | |
| iOS: `Ego` from CoreMotion/CoreLocation/CMAltimeter, ray-to-ground, v2 edge | `ios/` | after judging |

Order for tomorrow: codec and golden vectors -> contacts + scheduler -> receiver2 -> sim replay ->
viewer -> eval. Chips and iOS after judging.

## 11. Risks

- **Grouping flicker** on noisy tracks (ids churn, groups split and merge every second). Mitigation:
  hysteresis in §3.3, and `rev` is not bumped by membership noise below the `ce` scale.
- **Static scenes look empty on the wire** and a judge may read silence as failure. The viewer
  shows the floor heartbeat and the "k of n known" readout; `Ego` every 5 s is the pulse.
- **The 120 Hz tick in a u32** wraps after 414 days: fine. `secs` saturates at 18 h: a flight is
  under 3 h.
- **`m8` rounding up** makes small `ce` values look worse than they are at 0.25 m steps; irrelevant
  at drone scale, visible in an indoor phone test (set `pos_res` = 1 cm there; `ce` still 0.25 m
  floor; acceptable).
- **Footage replay has no uplink story** unless the sim listens: it does (the WASM edge takes
  `Digest`/`Focus` like the phone), so the focus click is real.

## Sources for §4

- RFC 6206 The Trickle Algorithm: https://www.rfc-editor.org/rfc/rfc6206
- Almeida, Shoker, Baquero, Efficient State-based CRDTs by Delta-Mutation: https://arxiv.org/abs/1410.2803 ; Chalmers thesis on CRDTs over limited bandwidth: https://odr.chalmers.se/handle/20.500.12380/302292
- RaptorQ RFC 6330 and the `raptorq` crate: https://docs.rs/crate/raptorq/latest ; Raptor codes: https://en.wikipedia.org/wiki/Raptor_code
- Burst erasure channels and interleaved MDS codes: https://arxiv.org/pdf/1911.03265 , https://worldwidescience.org/topicpages/c/channels+mixing+bursts.html
- ADS-B squitter scheduling (EUROCONTROL), AIS SOTDMA (USCG NAVCEN): https://www.eurocontrol.int/archive_download/all/node/10119 , https://navcen.uscg.gov/node/534
- MAVLink High Latency protocol: https://mavlink.io/en/services/high_latency.html , https://ardupilot.org/copter/docs/common-MAVLink-high-latency.html
- Iridium SBD developer guide (340 B MO, ~10 s): https://docs.rs/crate/sbd/0.3.0/source/doc/Iridium%20Short%20Burst%20Data%20Service%20Developers%20Guide%20v3_0.pdf
- LoRaWAN fragmented data block transport (TS004) and SCHC FEC draft: https://lora-alliance.org/wp-content/uploads/2020/11/fragmented_data_block_transport_v1.0.0.pdf , https://datatracker.ietf.org/doc/html/draft-pelov-schc-fragmentation-fec-rule-format-00
- STANAG 4607 field layout (Wireshark): https://www.wireshark.org/docs/dfref/s/s4607.html
