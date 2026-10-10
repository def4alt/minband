# MinBand

Bandwidth-minimal perception streaming: an edge device with a camera (iPhone now, drone later)
tracks objects in 3D and sends only **state deltas** to a laptop, which keeps a live **world twin**
that dead-reckons every entity between updates. Both ends run the *same* deterministic predictor,
so the edge sends a packet only when the twin would otherwise be wrong.

Target use case: drones over jammed or single-digit-kbps links. Demo: iPhone + laptop over Wi-Fi
with emulated impairment.

## Layout

| Path | What | Language |
|---|---|---|
| `core/` | Entity model, predictor, divergence thresholds, delta codec, budget controller. Compiled to iOS (uniffi) and WASM (server/viewer). | Rust |
| `server/` | UDP ingest, reliability/resync, world model, multi-device fusion, metrics, WebSocket fan-out, in-process link shaper and link profiles (time on air), CoT export to TAK, geodetic anchor (WGS84/MGRS). | TypeScript (Node) |
| `viewer/` | Three.js twin, error rings and coasting/staleness, link profiles and airtime, link activity strip, video-on-this-link panel, bytes/sec graph, mock snapshot server. | TypeScript (Vite) |
| `ios/` | ARKit (marker origin, pose, depth) + Vision/CoreML detection + tracker + core FFI + UDP client; opt-in H.264 baseline measurement (VideoToolbox). | Swift |
| `tools/` | Pi 5 link box (`pi-link.sh`, tc netem profiles), golden vectors on aarch64 under qemu, dummynet (macOS), evaluation, baselines and charts. | Shell / TS |
| `e2e/` | End-to-end tests: real server + WASM core + sim edges over UDP, viewer in headless Chromium, kernel shaping on `lo`, CoT listener; fallback-run recorder. | TypeScript (Node) |
| `proto/` | Wire protocol spec. | Markdown |
| `docs/` | Design, prior art, milestones. | Markdown |

## Read first

1. [docs/DESIGN.md](docs/DESIGN.md) - contact reports over a trickle link, the 3D handoff, durability; [docs/EDGE_HARDWARE.md](docs/EDGE_HARDWARE.md) - drone/phone sensor survey
2. [proto/PROTOCOL.md](proto/PROTOCOL.md) - wire format
3. [docs/MILESTONES.md](docs/MILESTONES.md) - build order and ownership
4. [docs/PRIOR_ART.md](docs/PRIOR_ART.md) - what exists and where this sits
5. [docs/HACKATHON_PLAN.md](docs/HACKATHON_PLAN.md) - D4D x EDTH weekend plan, Pi 5 link box, stretch ideas

## Toolchain

```bash
# Everything (rust + iOS/WASM targets, wasm-pack, xcodegen, pnpm) is pinned in mise.toml:
mise install
# Node 20+ (server, viewer) and Xcode 16+ with the iOS 17 SDK are assumed.
# iOS: tools/build-ios.sh builds the core XCFramework + Swift bindings (gitignored) before Xcode can build.
```

## Quick start (laptop side)

```bash
cd server && npm install && npm run dev      # UDP :7777, WS :8080
cd viewer && npm install && npm run dev      # http://localhost:5173
```

Without a phone, `npm run sim` in `server/` replays a synthetic scene through the full
edge pipeline (core predictor + thresholds + codec) so the twin, graphs and link slider work
end to end (`DEVICES=8 SCENE=spread npm run sim` for the drones-per-link run).

## Tests

```bash
cd core && cargo test                 # unit + golden vectors (tools/golden-aarch64.sh: the same on aarch64 under qemu)
cd server && npm test                 # node:test, fake clock
cd tools/eval && npm test             # replay; `npm run eval` regenerates runs/eval/
bash tools/test/pi-link.test.sh       # link box dry run (sudo tools/test/pi-link-kernel.test.sh: real tc on lo)
cd viewer && npm run smoke            # mock snapshot server + headless Chromium screenshots
cd e2e && npm install && npm test     # end to end: server + sim + viewer + CoT + kernel-shaped link (root for tc)
cd e2e && npm run record              # fallback run video through the link profiles -> runs/fallback/
```

The WASM packages (`core/pkg-node`, `core/pkg-web`) must be built first (core/README.md); use
binaryen 117's `wasm-opt` (wasm-pack downloads it; older distro builds break wasm-bindgen).

### Viewer without the server: mock and smoke test

`viewer/dev/mock-server.ts` speaks the server's WebSocket protocol on :8080 with every field of
the snapshot contract (`server/src/types.ts`), including the hackathon ones the server does not
produce yet. It is a small deterministic simulation (two edges running core's predictor, theta,
budget controller and heartbeat; shaper; receiver; fusion; MGRS) looping through
clean, a 10 s blackout, recovery, hf, lora and telemetry.

```bash
cd viewer && npm run mock                            # terminal 1
cd viewer && npm run dev                             # terminal 2, http://localhost:5173
MOCK_PHASE=blackout@8 MOCK_HOLD=1 npm run mock       # start and stay in a phase (stale here)
curl 'localhost:8080/mock?phase=lora&at=4&hold=1'    # jump at runtime; also freeze=1, geo=0, measured=1, legacy=1
npm run smoke                                        # mock + Vite + headless Chromium: screenshots, fails on console errors
```

In the viewer, `STAGE` (in DETAILS, or `?stage=1`) is the presenter view: the link activity strip
beside the twin and the video-on-this-link panel; the operator view keeps the restraint rules in
[docs/STYLE.md](docs/STYLE.md).
