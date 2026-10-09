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
  per-entity staleness, estimated twin error (when the edge later uploads its ground-truth log),
  time on air per device under the link profile's radio model (LoRa or serial; payload plus radio
  framing, without the UDP/IP header), and one event per datagram (up, including shaper drops,
  and acks down) in every snapshot for the packet waterfall.
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
  offset to its own clock from datagram arrival times and extrapolates to its own now
  (`server/src/clock.ts`): each datagram gives `local - edge = offset + transit delay`; a sample
  below the estimate lowers it at once; the estimate never exceeds the min over a sliding 10 s
  window and, while every sample in that window is above it (edge clock running slow, or added
  latency), rises toward that min by at most 1 ms/s. If every sample for 2 s is more than 1 s
  above it, the edge clock stepped (phone slept, ARKit timestamps stalled) and it re-syncs at
  once. A new session (new `session_nonce`) starts a fresh estimate.
- **Device identity**: the server keys a device by `device_id` once its `Hello` has been seen and
  by UDP address before that. A known `device_id` saying `Hello` from a new address keeps its
  state and moves there; acks go to the latest address, the old one stays routed as an alias.
  Because an acked edge never repeats `Hello`, a stream from an unknown address with no `Hello`
  is also adopted by an identified device when that device went quiet (>= 500 ms) as the new
  address appeared and the new stream's ticks continue its tick stream (within 1 s); ambiguous
  matches stay separate.
- **Sequence numbers** per device, `u32`, one per datagram.
- **Delta** = list of entity updates (`Spawn | Update | Despawn`). An `Update` carries the full
  `EntityState` of that entity (not a diff of fields): idempotent, loss-tolerant, 31 bytes with
  `postcard` varints (a one-update `Delta` is 40 B, 68 B on the wire; measured, see
  `proto/PROTOCOL.md`). Field-level diffs are a measured optimisation for later, not the baseline.
- **Self-declared threshold** (S14): every `Delta` and `Keyframe` carries `theta_q`, the position
  threshold in use (θ_pos x the budget controller's scale) in one byte, rounded up. Every mature
  state broadcast carries its own error class (ADS-B NIC/NACp, MAVLink `HIGH_LATENCY2` eph); the
  receiver keeps it per entity, so a consumer knows how far the twin may be from the edge's track.
- **Keyframe** = all live entities, sent every keyframe period (2 s at budget 0 and >= 8 kbit/s,
  see Cadence), after a `Nack` with more than 8 missing seqs. It is the heartbeat, so it is sent
  even when nothing is tracked. At a budget it is paced (S16): parts of at most 2 s of link time,
  one per link time of the previous part, each carrying the current state of its entities when it
  goes out, like a video intra-refresh; deltas keep flowing between parts.
- **Pose** = camera pose, only for drawing the frustum (not required for correctness): 0.5 s at
  budget 0, 10 s below 4 kbit/s. `Edge::pose` gates it, callers call it at their own rate.
- **Cadence from the budget** (S19, `core/src/cadence.rs`): the keyframe, Hello and pose periods
  and the receiver's coast/stale/drop thresholds are one integer function of the budget, computed
  by the edge from the budget it runs and by the receiver from the budget it advertises (the
  `Ack`'s budget is authoritative, 0 included). Keyframe period = 1 s + the time the budget needs
  for 1 kB, 2..15 s (14.3 s at 600 bit/s); Hello = 2 keyframes (5..30 s); coast = one keyframe +
  max(25 %, 0.5 s); stale = max(6 s, 3 keyframes); drop = max(10 s, 5 keyframes). The heartbeat
  period is a link-class parameter everywhere else too (DIS 5 s, Iridium SBD 10-15 s). Table in
  `proto/PROTOCOL.md`.
- **Trigger** (per entity, every tick, in `core::Edge`):
  - `|pos_real - pos_ghost| > θ_pos` (default 0.15 m)
  - `|vel_real - vel_ghost| > θ_vel` (default 0.3 m/s)
  - class or confidence bucket changed
  - entity age since last send > `T_max` (3 s at budget 0; 1.5 keyframe periods at a budget, so
    it never pre-empts a slow keyframe)
  - spawn / despawn
- **Budget controller**: target bits/s set by operator or link estimate. Every 500 ms compare
  sent bytes, payload plus the 28 B UDP/IP header per datagram (what the link carries), to the
  budget; scale `θ_pos` and `θ_vel` by `1.25` when over, `0.9` when under 70 %, scale clamped to
  `[0.33, 13]` (0.05..1.95 m at the default θ_pos). Despawns and spawns are never suppressed.
  Reported in metrics so the viewer can show "fidelity knob at 0.4 m".
- **Loss handling**: receiver tracks a window of seqs; a gap older than 200 ms becomes a `Nack`.
  The edge responds with the *current* state of every entity touched in the missing seqs (state
  repair), not the lost packets.
- **Hello refresh**: after being acked, the edge re-sends `Hello` every 5 s (Cadence) so a restarted
  server re-identifies the device. A receiver that adopted a device without a Hello records the
  nonce on the first one it sees; only a *different* nonce resets state.
- **Nack pacing**: a gap is nacked at most once per 500 ms (one round trip plus margin), and the
  edge ignores a repeat nack for an id it repaired within the last 500 ms. Without this, repairs
  under loss roughly doubled the uplink (see EVAL_FINDINGS.md).
- **Despawn repair**: the edge remembers despawned ids for 10 s; a nacked seq that carried a
  Despawn is repaired by resending it. Independently, once every part of a keyframe has arrived
  the receiver removes entities the keyframe did not list (and that were not observed after it).
- **Entity drop rule**: while the device is alive, removal happens only through Despawn and
  keyframe reconciliation (which also works when paced parts arrive over time). Age-based dropping
  (`drop`, 10 s at budget 0) applies once the device has been silent for `stale` (6 s); a hard
  limit of max(30 s, 3 x `drop`) applies regardless. All from the cadence of the advertised budget,
  so a static scene at 600 bit/s (keyframes every ~14 s) neither goes stale nor is dropped between
  keyframes. When the advertised budget rises, the old limits hold for one old coast period while
  the edge learns the new budget.
- **Coasting and honest error radius** (S15): under a prediction-error trigger silence means
  "within threshold" only while the heartbeat arrives. Once the device has been silent for
  `coast` (one keyframe period plus margin, 2.5 s at budget 0) its entities are *coasting*, and
  each entity's error radius `ce` grows from its declared θ: `ce = θ + max_speed(class) x silence`
  (silence since the device's last datagram, so a missed keyframe is a visible jump; capped at
  1000 m). Never extrapolate silently: coast, mark, then drop (FAA AD 2017-22-14). Trust returns
  per entity, not with the device: after a blackout the first datagram vouches only for what it
  carries, the rest keep coasting (`ce` still growing from the silence start) until a datagram
  sent after the resume refreshes them, or a whole keyframe taken after it arrives (one keyframe
  period at most while the link holds). Updates lost in the blackout would otherwise sit behind a
  tight θ ring until the nack round trip.
- **Staleness**: entity not refreshed for `stale` (6 s at budget 0, 3 keyframe periods) is drawn
  as stale; after `drop` it is dropped once its device is silent. Server-side, a device silent for
  5 s has all its entities stale and is removed after 30 s.

## 5. Predictor and why not a physics engine

Per-class kinematic models: constant velocity, damping, ground clamp, max speed. Rationale:
entity motion here is intent-driven (people, vehicles), where physics adds little prediction and
costs determinism. The predictor is an interface in `core`; a physics-backed implementation can
replace it later as long as it is deterministic across platforms. Determinism is tested with
golden vectors (`core/tests/golden/*.json`) that must match on native, iOS and WASM builds.

## 6. Link impairment

Two mechanisms, used for different purposes:

1. **In-process shaper** in the server (Bernoulli loss, then token bucket, then fixed FIFO delay)
   controlled from the viewer sliders and scenario presets, or from `GET /api/shaper` (see
   `server/README.md`). Reliable on stage, shows cause and effect instantly. It drops datagrams
   *before* they reach the receiver so resync is exercised for real, and acks are only generated
   for datagrams that got through, so a blackout silences the downlink too. Token bucket depth is
   `burstSec` (default 0.5 s) of `bps`; a datagram is admitted while the bucket is positive and
   may leave it in debt, so admission does not depend on size (a strict "tokens >= size" rule
   starves 160 B keyframes behind small deltas on a 2 kbps link). Delay never reorders. A timed
   override (`revertAfterMs`, used by the "blackout 10 s" preset) restores the previous link on
   the server, so it survives a viewer reload; any explicit change ends it early. A queue limit
   (like netem's `limit`, counting datagrams in the delay line) drops bursts the way the Pi link
   box does. Named **link profiles** (`/api/link`, the same table as the Pi link box in
   `HACKATHON_PLAN.md` 3.3) set the shaper, the edge budget and an airtime model in one step;
   `contested` alternates `lora` with random blackouts on a server timer, and `external` leaves
   shaping to the box and keeps only its budget and airtime model. The budget is the link's: each
   ack carries it split over the devices heard in the last 5 s.
2. **dummynet** (`tools/link.sh`, macOS `dnctl`/`pfctl`) shaping UDP :7777 at the OS level for
   honest measurements and for the recorded evaluation runs.

## 7. Evaluation

Tooling: `tools/eval` (see its README). Logs are replayed offline and in-process through the WASM
`Edge` and `Receiver` over a simulated link; no server. Until phone logs exist the same pipeline
runs on synthetic ground truth (perfect-tracker velocities, optional gaussian noise).

- **Bytes/s per device** under scenarios: static room, one walker, three walkers, crowd (8
  people with churn), 2 phones (live only). Bytes are counted at the sender, every datagram
  including those the link drops, as payload + 28 B UDP/IP header.
- **Baseline A**: H.264 720p at 1.5 Mbps (and 480p at 500 kbps, 360p at 250 kbps) - the "what
  drones send today". Encoded from the recorded ARKit frames with `AVAssetWriter` H.264
  (VideoToolbox) at those target bitrates over the same session as the ground-truth log; the
  bitrate is measured from the encoded track, not quoted. Until then the numbers are shown as
  "configured, to be replaced by measured VideoToolbox numbers". The server (`/api/baseline-a`,
  `Snapshot.baselineA`) and `tools/eval` read the same `runs/baseline_a.json`.
- **Baseline B**: naive metadata, full state of every entity every frame at 30 Hz (31 B is one
  `Update`, 40 B a message header plus the 28 B UDP/IP header; measured, `proto/PROTOCOL.md`):
  `entities * 31 B * 30 Hz + 30 Hz * 40 B`, entities time-averaged from the log.
- **Twin error**: at every logged frame, for each ground-truth row, the distance between the
  logged position and the receiver's extrapolation to that tick. The ground truth is the edge's
  own tracker output, so this measures sync fidelity, not perception accuracy. An entity absent
  from the twin is reported as availability (and charged 2.0 m in the penalised mean).
- **Fidelity vs bytes**: replay the edge's ground-truth log through `core::Edge` offline with
  `θ_pos` swept over `[0.02 .. 2.0]` (log-spaced, `θ_vel = 2·θ_pos`), lossless zero-delay link,
  measure mean and p95 twin position error, plot error vs bytes/s. One chart, log-x, with the
  baselines as reference lines.
- **Resilience**: twin error and availability vs packet loss rate (0, 5, 20, 50 %, Bernoulli,
  both directions, 50 ms one-way delay) at `θ_pos` 0.15, mean of 10 seeds, with state repair
  (an ack listing the open gaps whenever a gap turns 200 ms old, checked every 100 ms) and
  without (no acks; keyframes only). The live server's cadence (an ack after any datagram once
  100 ms have passed) is measured too, as a bytes comparison.

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
