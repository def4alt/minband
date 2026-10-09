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
| `server/` | UDP ingest, reliability/resync, world model, multi-device fusion, metrics, WebSocket fan-out, in-process link shaper. | TypeScript (Node) |
| `viewer/` | Three.js twin, error rings and coasting/staleness, link profiles and airtime, link activity strip, video-on-this-link panel, bytes/sec graph, mock snapshot server. | TypeScript (Vite) |
| `ios/` | ARKit (marker origin, pose, depth) + Vision/CoreML detection + tracker + core FFI + UDP client. | Swift |
| `tools/` | Link impairment (dummynet), baseline measurement, evaluation and charts. | Shell / TS |
| `proto/` | Wire protocol spec. | Markdown |
| `docs/` | Design, prior art, milestones. | Markdown |

## Read first

1. [docs/DESIGN.md](docs/DESIGN.md) - architecture and the sync protocol
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
end to end.

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
