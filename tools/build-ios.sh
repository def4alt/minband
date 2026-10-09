#!/usr/bin/env bash
# Build minband-core for iOS device + simulator, generate the Swift bindings with uniffi, and
# package an XCFramework at ios/Frameworks/MinBandCore.xcframework.
#
#   tools/build-ios.sh            # release (default)
#   PROFILE=debug tools/build-ios.sh
#
# Outputs:
#   ios/Frameworks/MinBandCore.xcframework   static lib per slice + Headers/minband_coreFFI/
#                                            {minband_coreFFI.h, module.modulemap} (Clang module
#                                            `minband_coreFFI`, imported by the generated Swift)
#   ios/MinBand/Generated/minband_core.swift compiled into the app target (gitignored)
#
# The bindgen is the crate's own `uniffi-bindgen` bin (core/src/bin/uniffi-bindgen.rs), so its
# version always matches the scaffolding; no `cargo install uniffi-bindgen-cli` needed.
# Uses the mise toolchain from the repo root when mise is available.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CORE="$ROOT/core"
GEN="$ROOT/ios/MinBand/Generated"
OUT="$ROOT/ios/Frameworks"
XCF="$OUT/MinBandCore.xcframework"
PROFILE="${PROFILE:-release}"
TARGETS=(aarch64-apple-ios aarch64-apple-ios-sim)
# Match ios/project.yml so the linker does not warn about mismatched minimum versions.
export IPHONEOS_DEPLOYMENT_TARGET="${IPHONEOS_DEPLOYMENT_TARGET:-17.0}"

if command -v mise >/dev/null 2>&1; then
  run() { mise exec -- "$@"; }
else
  run() { "$@"; }
fi

case "$PROFILE" in
  release) PROFILE_FLAG=--release ;;
  debug) PROFILE_FLAG= ;;   # empty, expanded unquoted below (bash 3.2 + set -u safe)
  *) echo "PROFILE must be release or debug" >&2; exit 2 ;;
esac

cd "$CORE"

echo "==> cargo build ($PROFILE) for ${TARGETS[*]}"
# `cargo rustc --crate-type staticlib` builds only the static library. With the manifest's mixed
# crate types (lib + staticlib + cdylib) rustc cannot run fat LTO for the staticlib and the archive
# is ~3x larger, made of per-crate objects.
for t in "${TARGETS[@]}"; do
  run cargo rustc --lib $PROFILE_FLAG --features ios --target "$t" --crate-type staticlib
done
LIB_DEVICE="$CORE/target/aarch64-apple-ios/$PROFILE/libminband_core.a"
LIB_SIM="$CORE/target/aarch64-apple-ios-sim/$PROFILE/libminband_core.a"

echo "==> uniffi-bindgen -> $GEN"
# Generated/ is entirely ours and gitignored; start clean so stale bindings never linger.
rm -rf "$GEN"
mkdir -p "$GEN"
run cargo run --quiet --features ios --bin uniffi-bindgen -- generate \
  --library "$LIB_DEVICE" --language swift --no-format --out-dir "$GEN"
for f in minband_core.swift minband_coreFFI.h minband_coreFFI.modulemap; do
  [[ -f "$GEN/$f" ]] || { echo "uniffi-bindgen did not produce $f" >&2; exit 1; }
done

echo "==> xcframework"
# uniffi names the module map <ffi_module>.modulemap; Clang only discovers it as
# `module.modulemap`. It sits in a subdirectory named after the module so it cannot collide with
# other XCFrameworks' module maps in the shared build-products include dir.
HDR="$CORE/target/ios-headers"
rm -rf "$HDR"
mkdir -p "$HDR/minband_coreFFI"
cp "$GEN/minband_coreFFI.h" "$HDR/minband_coreFFI/"
cp "$GEN/minband_coreFFI.modulemap" "$HDR/minband_coreFFI/module.modulemap"
# Only the Swift file belongs in the app sources; the C module ships inside the XCFramework.
rm -f "$GEN/minband_coreFFI.h" "$GEN/minband_coreFFI.modulemap"

mkdir -p "$OUT"
rm -rf "$XCF"
xcodebuild -create-xcframework \
  -library "$LIB_DEVICE" -headers "$HDR" \
  -library "$LIB_SIM" -headers "$HDR" \
  -output "$XCF" >/dev/null

echo "built $XCF"
echo "bindings $GEN/minband_core.swift"
