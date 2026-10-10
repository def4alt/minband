# minband-core

```bash
cargo test                                   # unit + golden tests
UPDATE_GOLDEN=1 cargo test --test golden     # after an intentional protocol/predictor change
wasm-pack build --target nodejs --out-dir pkg-node --release -- --features wasm   # for server/
wasm-pack build --target web    --out-dir pkg-web  --release -- --features wasm   # for viewer/
../tools/build-ios.sh   # for ios/: staticlib for device + simulator, uniffi Swift bindings, XCFramework
```

Determinism rules (enforced by review, tested by `tests/golden`): f32 only, integer ticks,
no trig/exp/pow, no HashMap iteration order on the wire, no platform `libm`.

Wire changes bump `wire::PROTOCOL_VERSION` (now 1, see `proto/PROTOCOL.md`); a peer on another
version fails with `BadVersion`. Hosts that hard-code an `Ack` (`[1, 4, 0, 0, 0]` in the server
tests and `tools/eval` `PRE_ACK`) or read the version byte must follow. The keyframe/Hello/pose cadence and the receiver's
coast/stale/drop thresholds come from `cadence(budget_bps)` (`src/cadence.rs`).
