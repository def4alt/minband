# MinBand design: feature extraction on the edge, contact reports over a trickle link

Status: 2026-10-10, branch `feature/protocol-v2`, decided in the pivot interview. Wire format:
`proto/PROTOCOL.md`. Hardware survey: `EDGE_HARDWARE.md`.

## 1. The problem, restated

A recon drone's video dies the moment jamming starts. The pilot cannot fly it after that (it is on
autopilot), the control link's back-channel carries 100 B/s to 1 kbit/s, and whatever the drone
still sees has to reach a commander through that trickle, in a form a situational-awareness system
(Delta, ATAK) can use: what is there, is it new, is it moving and where to, how sure are we, and
what is the drone itself doing. The sensor's own geolocation error is 5-10 m, so centimetres are
noise and bytes must be spent on *events* at the error scale.

A second consumer sits next to the commander: a server that, while the video is up, reconstructs
the scene in 3D. When the cut comes it must keep placing the drone's reports into the model it
already has. So the protocol has two regimes on one stream: fat, with camera poses and image
geometry for the model; thin, with contacts only, placed by rays.

Decisions from the interview:

| topic | decision |
|---|---|
| platform | one sensor set common to drones and phones (camera, IMU, barometer, heading, GNSS when present); no rangefinder or gimbal assumed; pitched for drones, tested on phones |
| consumer | observability for a commander (Delta/ATAK) and a 3D-reconstruction server; operator-driven *focus* on individual contacts; never targeting |
| link | continuous trickle, ~100 B/s to 1 kbit/s, downlink first; uplink optional |
| what is sent | egomotion, new objects, object motion; the edge decides *when*: static things rarely, focused things first |
| aggregation | groups by default, individuals only under focus |
| evidence | image chips only on operator request |
| durability | researched in §4; the stream must be reconstructable under heavy loss |
| receiver | the Three.js top-down twin, geodetic grid, CoT to ATAK; a side-by-side page on real footage |
| demo | replay the real drone footage with a simulated drone pose |

## 2. The common sensor set and what each sensor buys

| sensor | drone source | phone source | used for |
|---|---|---|---|
| camera (RGB or thermal) | video feed or SDK frames | AVFoundation / ARKit | detection, tracking, visual odometry |
| IMU | flight controller attitude | CoreMotion attitude | camera ray direction (pitch, roll, yaw) |
| barometer | FC altitude above take-off | CMAltimeter relative altitude | height above ground for the ray-to-ground intersection |
| heading | FC yaw (mag + GNSS course) | CoreMotion heading | course of the ray, course of movers |
| GNSS | FC fix, quality flags | CoreLocation, accuracy | own position; session origin; UTC |
| camera intrinsics | known model (FOV) | ARKit intrinsics | pixel -> ray; `Session.hfov`, `img_w/h` |
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
           (homography, VIO)        ray-to-ground geolocation ─► tracks in metres ENU + ce + ray + bbox
                                              │
                                              ▼
                                     contact manager
                                     • motion state machine per track
                                     • grouping into contacts (count, class mix, extent)
                                     • first_seen / since bookkeeping
                                     • change detection against the last *sent* record
                                              │
                                              ▼
                              scheduler (regime, ladder, floor, focus, budget) ─► frame builder ─► link
                                              ▲
                                       uplink: Digest, Focus, Clock, ChipAck
```

Everything from "tracks in metres" down is the deterministic Rust core (uniffi for iOS, WASM for
Node and the browser, plain C ABI later for a flight controller). Detector and tracker stay
platform code (CoreML on the phone, ONNX Runtime on a companion computer, the Python pipeline on
footage).

### 3.0 Precision before anything is sent

A false object costs the same bytes as a real one and more trust. The edge therefore runs two
independently trained detectors and tracks only what both put a box on (IoU >= 0.3, any class), or
what one of them scores very high (>= 0.7); a track is born at 0.5 confidence, continued at 0.3,
and reported only after 2 s. On the MEVA 1080p clip this removed every roof vent and roof segment
the single VisDrone model had tracked as persons, cars and trucks (none scored above 0.52) and kept
every parked car, the museum tank and the pedestrians (`tools/sidebyside/README.md`,
`tools/footage/consensus.py`). Two nano models cost less than one medium model on a companion
computer and fail independently. Motion-only detection (MTI) is not used for reported contacts.

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

The ray itself (`az`, `el`) is sent too: a server with real terrain intersects it with that
terrain and removes the flat-ground term, which the edge cannot.

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
  motion state = majority by count, course/speed = mean of moving members; ray and bbox of the
  centroid's nearest member (bbox = the union box in the video regime);
- a single object is a contact of count 1; there is one record type.

Under `Focus(split)` the edge also emits the members as child records (`ext.bit5`, `parent`),
on their own ladders; without focus a 12-vehicle convoy is one 27-byte record.

The distances and thresholds above are detail level 1 of five (`proto/PROTOCOL.md` §6.4). The edge
picks the level from the backlog it measures, in seconds of link time, not from the budget it was
told: on a busy scene and a thin link it groups at 30, 60 or 120 m, revises only past a floor of a
third of that, tolerates small count changes, and holds back contacts seen for less than 3-10 s;
on a fat, quiet link it groups at 8 m. Focused contacts are exempt. Grouping alone buys little:
a big group's centre moves whenever a member joins or leaves, and moving singles never group, so
each coarse level is a spatial resolution as much as a link distance (`docs/PROTOCOL_EVAL.md`
§11).

### 3.4 Egomotion

`Ego` carries own position, altitude, heading, speed, climb, nav mode, GNSS state, link state,
video state, battery, own `ce`, the camera footprint (centre and radius on the ground) and the
scene counts. The footprint is the negative information: a commander who sees the drone looked at
a tree line for 40 s and reported nothing knows something. Nav mode and link state are what the
pilot cannot see once video is gone and are sent at once when they change.

## 4. Durability: research and decision

The question: how should the stream be reconstructable under jamming, complexity no object. The
loss model is bursty (jamming windows of seconds to minutes), with a few percent random loss
underneath, and the uplink may be dead for the whole flight.

| approach | what it buys | what it costs | fit |
|---|---|---|---|
| **Repetition of self-contained state** (ADS-B squitters every 0.5 s with ±200 ms jitter, AIS SOTDMA every 2-10 s by dynamics, DIS heartbeat 5 s) | survives *any* loss pattern and needs no uplink; newer copies supersede older; receivers can join mid-stream | bandwidth: a copy per interval whether anything changed or not | the base layer, but the interval must adapt |
| **Trickle (RFC 6206)**: interval doubles while consistent, resets on inconsistency, transmissions suppressed when peers are heard to agree | "a few packets per hour when nothing changes, milliseconds when something does"; 50-200 lines of C | needs a definition of *consistent* | gives the adaptive interval: per record, doubling after a change, suppressed by a digest |
| **State-based / delta-state CRDT** (Almeida, Shoker, Baquero 2014) | idempotent, commutative, order-free merge; converges as long as every delta eventually arrives; a Chalmers field study on mine vehicles found delta-state viable over low bandwidth and plain state-based not | delta-state alone is "more vulnerable to message loss" and needs an anti-entropy round | the merge rule: LWW per contact by `(rev, tick)`; the anti-entropy round is the ladder plus the optional digest |
| **Acks/nacks + state repair** | cheapest when loss is low | needs the uplink to recover; useless simplex | demoted to suppression (`Digest`) |
| **Erasure coding**: block-interleaved MDS or fountain codes (RaptorQ RFC 6330: decode failure < 1 % at K symbols, < 1e-6 at K+2) | bandwidth-efficient against *random* loss; recovers a block without feedback | a burst longer than the coding block is not recovered; latency and memory grow with the block; interleaving spreads losses but does not replace redundancy | wrong tool for *state* (a newer state supersedes the lost one anyway); right tool for *bulk* objects: image chips |
| **High-latency telemetry** (MAVLink `HIGH_LATENCY2`: one ~100 B heartbeat per 5 s, Iridium SBD: one 340 B message per ~10 s, APRS/ADS-B position reports) | proves that one self-contained message per few seconds is what every low-rate link ends up doing | nothing beyond the obvious | the shape of `Ego` and `Session` |

Decision, in layers (each independently testable):

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
supersession makes it redundant); no retransmission queue; no encryption or authentication (a real
link encrypts below us). Under a jammer that targets the frame timing, randomise the frame
builder's slot by ±20 % like ADS-B; the design already tolerates it.

## 5. The 3D handoff

The server's reconstruction (not in this repo) runs on the video while it lasts. The protocol's job
is to make the cut survivable for it:

1. **Shared clock.** Ticks are the camera frame clock (`Session.video_frame0`, `fps_x100`), so a
   decoded video frame and a `Pose` record name the same instant without any other clock.
2. **Alignment while it is cheap.** In the video regime the edge streams `Pose` (camera centre in
   ENU cm, yaw/pitch/roll in 0.01°) at 10 Hz: 240 B/s next to megabits of video. The server solves
   the similarity transform (scale, rotation, translation; Umeyama or any trajectory alignment)
   between its reconstruction's camera trajectory and the edge's ENU, continuously, and keeps the
   last good solution.
3. **Geometry on contacts.** Each contact carries its observation ray (`az`, `el`, 2 B) and, in
   the video regime, its image box (4 B). The server attaches detections to the model while video
   is up and, after the cut, intersects rays from the `Ego` camera position with its own terrain:
   a better position than the edge's flat ground, in the model's frame, with no further pose
   stream needed.
4. **After the cut.** `Ego` every 5 s carries the camera position; contacts keep their rays; the
   server keeps placing them. The viewer shows the model where it has one and the twin's ENU
   where it does not.

**Edge-side variant (`tools/recon3d`, 2026-10-10).** When the edge itself runs the reconstruction
(MASt3R-SLAM on a companion computer; the map goes out on 3d-map-stream's channel), it does step 3
on board: a detection's mask selects its pixels in the frame's pointmap for the direction, the ray
meets the reconstructed terrain for the range, and the contact goes out with its height (`dz`,
ext.bit1) and a ray aimed at the object instead of the flat ground below it. The tracker's own
error radius (in the reconstruction's frame) replaces the flat-ground `ce` model. The receiver
places the contact on its copy of the map; the object's appearance travels once as a chip.

## 6. Bandwidth, measured against surprise

Bytes are spent only when a contact's state machine moves or its centroid leaves its own error
circle, plus a floor that is 17 % of a 100 B/s link for a 20-contact static scene
(`proto/PROTOCOL.md` §7). A convoy of twelve is one record. A focused individual costs about
30 B/s. A chip costs a quarter of the link for half a minute and the operator chose to pay it. In
the video regime the stream is about 300 B/s beside the video.

## 7. The on-device log

The edge appends every contact revision, `Ego` and `Pose` to a local file (CSV now, SQLite later).
It is the complete record for after the flight, the ground truth for evaluation, and the source for
a store-and-forward upload when a fat link appears (landing, LTE). The wire protocol never depends
on it.

## 8. Receiver, viewer and the side-by-side test

The receiver (Rust `Receiver`, WASM for Node and the browser) merges records, derives events,
dead-reckons, tracks liveness and the "k of n known" integrity readout, and exports CoT per
`proto/PROTOCOL.md` §10.

`tools/sidebyside/` is the test on real footage: the MEVA clip on the left with the edge's tracks
and contacts drawn on it from the per-frame homographies; on the right the receiver's top-down
world, the decoded frames and the derived event stream, bytes/s, and the link controls (profiles,
blackout, focus). The replay driver runs the real WASM edge over the footage tracks with a simulated
drone (camera height and pitch from the footage fit, an origin lat/lon, a hover), a shaper (rate,
delay, loss, blackout windows) and the real WASM receiver.

## 9. Metrics

| metric | definition |
|---|---|
| picture completeness | fraction of the edge's live contacts the receiver holds at their current revision, over time |
| event latency | time from a state-machine transition on the edge to its derivation on the receiver, per link profile and loss rate |
| recovery time | after a blackout of B seconds, time until completeness returns to 1 |
| honesty | fraction of contacts whose true position lies within `ce_shown` (target >= 0.68 for 1 sigma) |
| bytes | B/s on the wire by record type; bytes per event |
| focus cost | B/s added per focused contact; chip delivery time |
| alignment | residual of the `Pose` trajectory against the reconstruction's after the similarity solve (server side) |

## 10. Build order on this branch

| part | where |
|---|---|
| spec, this doc, hardware survey | `proto/PROTOCOL.md`, `docs/DESIGN.md`, `docs/EDGE_HARDWARE.md` |
| core: `wire.rs` codec + peek/describe, `contacts.rs` (state machine, grouping, change detection), `scheduler.rs` (regime, ladder, floor, focus, budget, frame builder), `receiver.rs` (merge, derived events, liveness, dead reckoning), `geo.rs` (ENU, rays, ce); golden tests | `core/src/` |
| core: chips (`raptorq`) behind a feature flag | `core/src/chips.rs` (later) |
| side-by-side page and replay driver on the MEVA footage | `tools/sidebyside/` |
| server ingest, fusion on contacts, CoT; viewer groups and events | `server/`, `viewer/` (after the side-by-side works) |
| iOS: `Ego`/`Pose` from CoreMotion/CoreLocation/CMAltimeter, ray-to-ground, edge | `ios/` (later) |

## 11. Risks

- **Grouping flicker** on noisy tracks (ids churn, groups split and merge every second). Mitigation:
  hysteresis in §3.3, and `rev` is not bumped by membership noise below the `ce` scale.
- **Static scenes look empty on the wire** and a judge may read silence as failure. The viewer
  shows the floor heartbeat and the "k of n known" readout; `Ego` every 5 s is the pulse.
- **`m8` rounding up** makes small `ce` values look worse than they are at 0.25 m steps; irrelevant
  at drone scale.
- **Alignment quality** depends on `Pose` accuracy; consumer attitude at 1-2° gives a few metres
  of placement error at 100 m. The server's solve averages over hundreds of poses; the residual is
  a reported metric, not a hidden assumption.

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
