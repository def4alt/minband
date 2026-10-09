#!/usr/bin/env bash
# Build minband-core for iOS device + simulator, generate Swift bindings with uniffi, and
# package an XCFramework at ios/Frameworks/MinBandCore.xcframework.
# Prereq: `cargo install uniffi-bindgen-cli` is NOT needed; we use the bindgen binary built from
# the crate (see core/src/bin/uniffi-bindgen.rs, M1 task) once the `ios` feature is implemented.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/core"
cargo build --release --target aarch64-apple-ios --features ios
cargo build --release --target aarch64-apple-ios-sim --features ios
OUT="$ROOT/ios/Frameworks"
mkdir -p "$OUT" "$ROOT/ios/MinBand/Generated"
cargo run --release --features ios --bin uniffi-bindgen -- generate \
  --library target/aarch64-apple-ios/release/libminband_core.dylib \
  --language swift --out-dir "$ROOT/ios/MinBand/Generated" 2>/dev/null || \
  echo "uniffi bindgen not wired yet (M1). Static libs are built; see docs/MILESTONES.md"
rm -rf "$OUT/MinBandCore.xcframework"
xcodebuild -create-xcframework \
  -library target/aarch64-apple-ios/release/libminband_core.a \
  -library target/aarch64-apple-ios-sim/release/libminband_core.a \
  -output "$OUT/MinBandCore.xcframework"
echo "built $OUT/MinBandCore.xcframework"
