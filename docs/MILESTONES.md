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

## M4 iOS app [iOS owner] (implemented 2026-10-09; needs on-device verification)
- ARKit session, marker origin, depth, CoreML detector, 3D lift, KF tracker.
- Core FFI integration, UDP client, acks, budget.
- Ground-truth logger and export.
- Done when: walking around a room produces a coherent twin on the laptop with < 1 kbps for a
  static scene and < 10 kbps with two walkers.

## M5 Loss and resync hardening [Rust + TS] (done 2026-10-09; dummynet runs pending)
- Gap detection, state repair, keyframe splitting, device timeouts, clock offset.
- dummynet scripts and recorded runs at 0/5/20/50% loss.

## M6 Multi-phone fusion [TS + iOS] (server side done; needs two phones)
- Second phone, shared marker origin, fusion with hysteresis, fusion toggle.

## M7 Evaluation [whoever owns `tools/`] (tooling done; H.264 measurement pending)
- Baseline A: H.264 bitrate from recorded frames (VideoToolbox).
- Baseline B: naive 30 Hz metadata.
- Fidelity-vs-bytes sweep from ground-truth logs; resilience chart.
- Recorded fallback run.

## M8 Presentation
- Live: link slider on stage, side-by-side, bytes graph.
- Slides: problem (DARPA framing), idea (surprise-proportional bandwidth), architecture, charts,
  what's next (real drone radio, ROI thumbnails, field-level diffs).

## Remaining work (as of 2026-10-09)
- On a real iPhone: marker lock, depth lift, overlay alignment, detector rate, bytes/s end to end.
- Record a phone ground-truth log and rerun `tools/eval` on it; retune θ_vel (EVAL_FINDINGS.md #1).
- Measure the H.264 baseline on the phone (VideoToolbox) and drop it into `runs/baseline_a.json`.
- dummynet runs at 0/5/20/50% loss on Wi-Fi (`tools/link.sh`).
- Two-phone fusion rehearsal with the printed marker.
- Presentation: recorded fallback run, slides from `runs/eval/*.svg` and `summary.md`.
