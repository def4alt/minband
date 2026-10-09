# MinBand design

Status: v0.1, decided 2026-10-09 after the kickoff interview. Everything here is a decision unless
marked *open*.

## 1. Goal

A hackathon project that must be fully operational at the end: an iPhone acting as a drone's
camera tracks objects in 3D and streams **only changes** to a laptop, which keeps a live world
twin. Measured against H.264 video and naive per-frame detections, with a fidelity-vs-bytes curve,
and demoed live with a link that degrades on stage while the twin stays coherent.

Non-goals for v1: security/auth, flight control, cloud, persistence beyond a session.

## 2. Key idea in one paragraph

This is dead reckoning (IEEE 1278.1 DIS) applied to a perception pipeline. The edge keeps, for
every tracked entity, a **ghost**: the state the receiver currently believes, computed by running
the *same deterministic predictor* the receiver runs, from the last update the edge sent. Each tick
the edge compares the real track against the ghost. If they diverge beyond a threshold (position,
velocity, class, confidence, or an age cap), it sends a delta and resets the ghost. The receiver
applies deltas and predicts forward to its own wall clock. Bandwidth therefore scales with
*surprise*, not with frame rate, and a better predictor directly buys bytes.

Three things make this more than DIS:

1. The entities are *perceived*, not owned. They appear, get lost, merge, and have confidence.
2. The link is lossy and tiny. Repair is state-based (resend latest state), never log-based
   (replay old deltas). Deltas are idempotent per entity.
3. A **budget controller** raises thresholds when the link is over budget, trading fidelity for
   bytes continuously instead of failing (the DARPA Semantically-Aware ISR framing).

## 3. System

```
 iPhone (edge)                                 Laptop
 ┌──────────────────────────────┐   UDP       ┌───────────────────────────────┐
 │ ARKit: pose, depth, marker   │  deltas     │ server (Node)                 │
 │ Vision/CoreML: detections    │ ─────────►  │  ingest, acks/nacks, shaper   │
 │ Tracker: 3D tracks           │  ◄─────     │  world model + fusion         │
 │ core (Rust via uniffi):      │  acks       │  metrics                      │
 │   ghosts, thresholds, budget │             │  core (Rust via WASM):        │
 │   codec                      │             │    predictor, codec           │
 │ UDP client                   │             └──────────────┬────────────────┘
 └──────────────────────────────┘                            │ WebSocket
                                                  ┌──────────▼────────────────┐
                                                  │ viewer (Three.js)         │
                                                  │ twin, ghosts, staleness,  │
                                                  │ bytes graph, link slider  │
                                                  └───────────────────────────┘
```

### 3.1 Edge (iOS)

- **ARKit** `ARWorldTrackingConfiguration` with `ARReferenceImage` detection. The printed marker
  defines the world origin for every phone (Y up, origin at marker center, X along marker width).
  Until the marker is seen, the phone streams nothing but `Hello` and pose-less heartbeats.
  Scene depth (`frameSemantics = .sceneDepth`) on LiDAR devices; raycast against detected planes
  otherwise.
- **Detection** at 10-15 Hz on the ARKit frame: YOLOv8n/v11n CoreML (COCO classes, filtered to
  person, chair, backpack, handbag, cup, bottle, laptop, cell phone, tv; configurable). Vision's
  `VNRecognizedObjectObservation` gives 2D boxes.
- **3D lift**: bbox center -> ray in camera frame -> depth sample (median of a small patch at the
  bbox center, or plane raycast) -> point in world frame via `ARCamera.transform`. Boxes with no
  depth are dropped.
- **Tracker**: 3D nearest-neighbour association with per-class gating, constant-velocity Kalman
  (position + velocity, 6 states), birth after 3 hits, death after 1 s without hits. Velocity is
  what the predictor needs, so the KF is where velocity quality is decided.
- **core FFI**: tracks go into the Rust `Edge` object every tick (30 Hz); it returns zero or more
  encoded datagrams to send. All sync logic lives in Rust.
- **UDP** via `Network.framework` (`NWConnection`, `.udp`). Also receives acks/nacks.
- **Local ground truth log**: every track at every tick, written to a file for offline evaluation.
- **Optional, budget permitting**: a JPEG thumbnail (64x64) on entity spawn, low priority.

### 3.2 Core (Rust)

Single crate `minband-core`, deterministic:

- `EntityState { id, class, pos: [f32;3], vel: [f32;3], conf: u8, flags }`
- `Predictor::step(&EntityState, dt_ticks) -> EntityState`: per-class kinematics.
  Constant velocity with exponential damping per class, ground clamp (y >= 0 for ground
  classes), per-class max speed. Fixed-point time (ticks of 1/120 s), f32 state, only +,-,*,/,
  sqrt and comparisons (all IEEE-754 correctly rounded, so identical across ARM64 and WASM).
  No trig, no exp, no platform `libm`.
- `Edge`: owns ghosts, the budget controller and the outgoing sequence. `tick(tracks, now) ->
  Vec<Datagram>`; `on_ack(ack)`.
- `Receiver`: owns received state per device, detects gaps, produces `Ack` payloads, and
  `extrapolate(now) -> Vec<EntityState>`.
- `codec`: `postcard` + `serde` (compact, deterministic, no schema registry). Everything on the
  wire is encoded and decoded in Rust; Swift and TS never parse bytes.
- Bindings: `uniffi` for Swift, `wasm-bindgen` for Node/browser. Behind feature flags so the pure
  crate builds and tests anywhere.

### 3.3 Server (Node, TypeScript)

- `dgram` UDP socket on :7777. Each datagram goes through the **shaper** first (see 6), then into
  the WASM `Receiver` for its device.
- **World model**: per-device entity sets, plus a **fusion** layer producing global entities:
  two tracks from different devices merge when same class, distance < 0.5 m and velocity
  difference < 0.5 m/s for 1 s; split when apart > 1.0 m for 1 s. Global entity position =
  confidence-weighted mean of its sources' extrapolated positions.
- **Acks** every 100 ms per device or immediately on a detected gap: `{last_seq, missing[]}`.
- **Metrics**: bytes in per device (payload + 28 B UDP/IP header), msgs/s, deltas vs keyframes,
  per-entity staleness, estimated twin error (when the edge later uploads its ground-truth log).
- **WebSocket** on :8080 fans out world snapshots at 30 Hz plus metrics at 2 Hz to the viewer.
- `npm run sim`: synthetic scene (random walkers, a bouncing object) driven through the WASM
  `Edge` so the full pipeline runs with no phone.

### 3.4 Viewer (Three.js, Vite)

- Top-down and free orbit camera. Marker drawn at origin; each phone drawn as a frustum.
- Entities as class-coloured capsules with velocity arrows. Staleness fades opacity; a
  "ghost" wireframe shows the receiver's extrapolation when `debug` is on; a trail shows the last
  5 s.
- Panels: bytes/sec per device (live), deltas vs keyframes, link slider (bandwidth, latency,
  loss), fusion toggle, baseline overlay ("H.264 would be at X kbps").
- Side-by-side mode: the twin on the left, and on the right either the phone's screen mirror
  (QuickTime/AirPlay) or a WebRTC preview if we add one later.

## 4. Sync protocol (behaviour; wire format in `proto/PROTOCOL.md`)

- **Ticks** are 1/120 s of the *edge* clock, `u32` since session start. The receiver estimates
  offset to its own clock from datagram arrival times (min-filter over the last 50) and
  extrapolates to its own now.
- **Sequence numbers** per device, `u32`, one per datagram.
- **Delta** = list of entity updates (`Spawn | Update | Despawn`). An `Update` carries the full
  `EntityState` of that entity (not a diff of fields): idempotent, loss-tolerant, ~22 bytes with
  `postcard` varints. Field-level diffs are a measured optimisation for later, not the baseline.
- **Keyframe** = all live entities, sent every 2 s, after a `Nack`, and when a new device joins.
  Also sent when the budget has been under-used for >1 s (free refresh).
- **Pose** = camera pose at 2 Hz, only for drawing the frustum (not required for correctness).
- **Trigger** (per entity, every tick, in `core::Edge`):
  - `|pos_real - pos_ghost| > θ_pos` (default 0.15 m)
  - `|vel_real - vel_ghost| > θ_vel` (default 0.3 m/s)
  - class or confidence bucket changed
  - entity age since last send > `T_max` (default 3 s)
  - spawn / despawn
- **Budget controller**: target bits/s set by operator or link estimate. Every 500 ms compare
  sent bytes to the budget; scale `θ_pos` and `θ_vel` by `1.25` when over, `0.9` when under,
  clamped to `[0.05, 2.0]` m. Despawns and spawns are never suppressed. Reported in metrics so the
  viewer can show "fidelity knob at 0.4 m".
- **Loss handling**: receiver tracks a window of seqs; a gap older than 200 ms becomes a `Nack`.
  The edge responds with the *current* state of every entity touched in the missing seqs (state
  repair), not the lost packets.
- **Staleness**: entity not refreshed for 2 x `T_max` is drawn as stale; after 10 s it is dropped.
  If the device is silent for 5 s, all its entities are stale; after 30 s the device is removed.

## 5. Predictor and why not a physics engine

Per-class kinematic models: constant velocity, damping, ground clamp, max speed. Rationale:
entity motion here is intent-driven (people, vehicles), where physics adds little prediction and
costs determinism. The predictor is an interface in `core`; a physics-backed implementation can
replace it later as long as it is deterministic across platforms. Determinism is tested with
golden vectors (`core/tests/golden/*.json`) that must match on native, iOS and WASM builds.

## 6. Link impairment

Two mechanisms, used for different purposes:

1. **In-process shaper** in the server (token bucket + fixed delay + Bernoulli loss) controlled
   from the viewer slider. Reliable on stage, shows cause and effect instantly. It drops
   datagrams *before* they reach the receiver so resync is exercised for real.
2. **dummynet** (`tools/link.sh`, macOS `dnctl`/`pfctl`) shaping UDP :7777 at the OS level for
   honest measurements and for the recorded evaluation runs.

## 7. Evaluation

- **Bytes/s per device** under scenarios: static room, one walker, three walkers, 2 phones.
- **Baseline A**: H.264 720p at 1.5 Mbps (and 480p at 500 kbps) - the "what drones send today".
  Encoded from the recorded ARKit frames with VideoToolbox, bitrate measured not quoted.
- **Baseline B**: naive metadata, full state of every entity every frame at 30 Hz.
- **Fidelity vs bytes**: replay the edge's ground-truth log through `core::Edge` offline with
  `θ_pos` swept over `[0.02 .. 2.0]`, measure mean and p95 twin position error against the
  ground truth at the receiver's extrapolated time, plot error vs bytes/s. One chart, log-x.
- **Resilience**: twin error vs packet loss rate at fixed `θ`, with and without state repair.

## 8. Risks and mitigations

| Risk | Mitigation |
|---|---|
| CoreML detector too slow on the phone | Run at 10 Hz on a downscaled frame; tracker interpolates. YOLOv8n at 320 px is ~15 ms on A15+. |
| Depth noise makes velocity jittery, which triggers deltas | KF velocity with strong process noise prior; `θ_vel` floor; median depth patch. |
| Rust float determinism across ARM64/WASM | Restrict ops to IEEE basic ops; golden tests run in CI on both; avoid `f32::powf`, `sin`, etc. |
| Marker not seen on stage | Big (A3) high-contrast marker, rehearsed lighting; manual "set origin here" fallback button. |
| Live demo network misbehaves | In-process shaper does not depend on the venue network; recorded fallback run and charts. |
| Multi-phone fusion flickers | Hysteresis on merge/split; fusion toggle in UI so the single-phone demo never depends on it. |
| Rust toolchain for iOS cross-compile | Set up in week 1 (`tools/build-ios.sh`), not in week 4. |

## 9. Open questions

- Thumbnails on spawn: nice for the operator story, but adds a second traffic class and priority
  queue. Decide after M4 based on remaining budget.
- Field-level diffs vs full-state updates: measure after M5; only worth it if entity count is
  high and most changes are position-only.
- Edge-side detection-free "keep alive" when the camera sees nothing: currently just `Pose`.
