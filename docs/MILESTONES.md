# Milestones

Order matters: M1 unblocks everything, M3 makes the project demoable without a phone, M4 is the
longest pole. Suggested ownership for a large team in brackets.

## M0 Scaffold (done 2026-10-09)
Repo layout, design, protocol, toolchain via `mise install`.

## M1 Core crate [Rust owner] (done 2026-10-09)
- `EntityState`, `Predictor` (per-class kinematics), `Edge`, `Receiver`, `codec`.
- Golden tests: fixed input scripts -> expected datagram bytes and expected extrapolations.
- WASM build (`wasm-pack build --target nodejs` and `--target web`), uniffi build for iOS
  (`tools/build-ios.sh` producing an XCFramework).
- Done when: `cargo test` green; the same golden JSON passes under WASM in Node.

## M2 Server [TS owner] (done 2026-10-09)
- UDP ingest, WASM `Receiver` per device, acks/nacks, world model, metrics, WebSocket fan-out.
- `npm run sim`: synthetic edge through WASM `Edge`.
- In-process shaper with WS control messages.
- Done when: sim scene shows in the viewer with bytes graph, and dropping the shaper to 2 kbps
  degrades gracefully.

## M3 Viewer [frontend owner] (done 2026-10-09)
- Three.js twin, entity rendering, staleness, ghosts, trails, frustums, panels, link slider,
  side-by-side layout.
- Done when: a non-team member can read the demo without explanation.

## M4 iOS app [iOS owner] (done 2026-10-09; tested on device)
- ARKit session, marker origin, depth, CoreML detector, 3D lift, KF tracker.
- Core FFI integration, UDP client, acks, budget.
- Ground-truth logger and export.
- Done when: walking around a room produces a coherent twin on the laptop with < 1 kbps for a
  static scene and < 10 kbps with two walkers.

## M5 Loss and resync hardening [Rust + TS] (done 2026-10-09; Wi-Fi dummynet runs pending)
- Gap detection, state repair, keyframe splitting, device timeouts, clock offset.
- dummynet scripts and recorded runs at 0/5/20/50% loss. The replayed sweep (`tools/eval`) and the
  live in-process e2e runs (`e2e/`, 20 % loss and blackouts) exist; the Wi-Fi runs with the phone
  do not yet.

## M6 Multi-phone fusion [TS + iOS] (server side done, e2e with two sim devices; needs two phones)
- Second phone, shared marker origin, fusion with hysteresis, fusion toggle.

## M7 Evaluation [whoever owns `tools/`] (tooling done; H.264 measurement code in the app, on-device run pending)
- Baseline A: H.264 bitrate from recorded frames (VideoToolbox).
- Baseline B: naive 30 Hz metadata.
- Baseline C: a 150 B AI thumbnail every N s at the same bytes or link rate (S23).
- Fidelity-vs-bytes sweep from ground-truth logs; resilience chart.
- Recorded fallback run.

## M8 Presentation (draft deck and fallback recorder done 2026-10-09)
- Live: link slider on stage, side-by-side, bytes graph.
- Slides: problem (DARPA framing), idea (surprise-proportional bandwidth), architecture, charts,
  what's next (real drone radio, ROI thumbnails, field-level diffs).

## Remaining work (as of 2026-10-09, after the P0/P1 build)

Done in code and covered by tests (unit, `e2e/`): cadence from budget (S19), header accounting,
keyframe pacing (S16), threshold byte (S14), coasting and error radius (S15), link profiles and
time on air (S2), packet waterfall (V3), video on this link with the thumbnail competitor (V1,
S23), error rings (V2), CoT export with a geodetic anchor and MGRS (S3), Pi link box script,
golden vectors on aarch64 (qemu), drones-per-link sim with Pose, dismount label, one Update size
(31 B) in every doc, `peek_json`.

Needs hardware or people:
- Record a phone ground-truth log and rerun `tools/eval` on it; retune θ_vel (EVAL_FINDINGS.md #1).
- Build the app and run the H.264 measurement (`ios/README.md`, "H.264 baseline"); copy the json to
  `runs/baseline_a.json` and fill the two placeholders on the numbers slide.
- On the Pi: `sudo tools/test/pi-link-kernel.test.sh` (real netem), `cd core && cargo test`, then
  phone -> Pi -> laptop through each profile.
- dummynet or Pi runs at 0/5/20/50 % loss on Wi-Fi with the phone.
- Two-phone fusion rehearsal with the printed marker.
- CoT on a real ATAK/WinTAK screen (`server/README.md`).
- Slide 1: the track's problem statement, the mentor's scenario, team names; re-record the fallback
  run on the final build (`cd e2e && npm run record`).
- Weekend priorities, the Pi 5 link box and stretch ideas: [HACKATHON_PLAN.md](HACKATHON_PLAN.md).
