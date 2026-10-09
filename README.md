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
| `viewer/` | Three.js twin, staleness/ghost rendering, bytes/sec graph, link slider, side-by-side mode. | TypeScript (Vite) |
| `ios/` | ARKit (marker origin, pose, depth) + Vision/CoreML detection + tracker + core FFI + UDP client. | Swift |
| `tools/` | Link impairment (dummynet), baseline measurement, evaluation and charts. | Shell / TS |
| `proto/` | Wire protocol spec. | Markdown |
| `docs/` | Design, prior art, milestones. | Markdown |

## Read first

1. [docs/DESIGN.md](docs/DESIGN.md) - architecture and the sync protocol
2. [proto/PROTOCOL.md](proto/PROTOCOL.md) - wire format
3. [docs/MILESTONES.md](docs/MILESTONES.md) - build order and ownership
4. [docs/PRIOR_ART.md](docs/PRIOR_ART.md) - what exists and where this sits

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
