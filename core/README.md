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
