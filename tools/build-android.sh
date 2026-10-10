#!/usr/bin/env bash
# Build minband-core for Android, generate the Kotlin bindings with uniffi, and drop both where
# the Gradle project (android/) picks them up. The Android twin of build-ios.sh.
#
#   tools/build-android.sh                 # release (default), arm64-v8a
#   PROFILE=debug tools/build-android.sh
#   ABIS="arm64-v8a armeabi-v7a" tools/build-android.sh
#
# Outputs (both gitignored):
#   android/app/src/main/jniLibs/<abi>/libminband_core.so
#   android/app/src/main/java/dev/minband/core/minband_core.kt   (package dev.minband.core,
#                                                                 see core/uniffi.toml)
#
# Needs: rustup targets aarch64-linux-android (and armv7-linux-androideabi for armeabi-v7a) and
# an NDK (ANDROID_NDK_HOME, else the newest under $ANDROID_HOME/ndk). No cargo-ndk: the core has
# no C dependencies, so pointing cargo at the NDK's clang wrapper as the linker is all it takes.
# The bindgen is the crate's own `uniffi-bindgen` bin, so its version always matches the
# scaffolding. The uniffi feature is still called `ios` in core/Cargo.toml.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CORE="$ROOT/core"
APP="$ROOT/android/app/src/main"
JNI="$APP/jniLibs"
GEN="$APP/java"
PROFILE="${PROFILE:-release}"
API="${ANDROID_API:-26}"      # minSdk in android/app/build.gradle.kts
read -r -a ABIS <<< "${ABIS:-arm64-v8a}"

if command -v mise >/dev/null 2>&1; then
  run() { mise exec -- "$@"; }
else
  run() { "$@"; }
fi

case "$PROFILE" in
  release) PROFILE_FLAG=--release; PROFILE_DIR=release ;;
  debug) PROFILE_FLAG=; PROFILE_DIR=debug ;;
  *) echo "PROFILE must be release or debug" >&2; exit 2 ;;
esac

if [ -z "${ANDROID_NDK_HOME:-}" ]; then
  SDK="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-${LOCALAPPDATA:-$HOME/AppData/Local}/Android/Sdk}}"
  ANDROID_NDK_HOME="$(ls -d "$SDK"/ndk/* 2>/dev/null | sort -V | tail -1 || true)"
fi
[ -n "$ANDROID_NDK_HOME" ] || { echo "no NDK: set ANDROID_NDK_HOME" >&2; exit 2; }
PREBUILT="$(ls -d "$ANDROID_NDK_HOME"/toolchains/llvm/prebuilt/* | head -1)"
BIN="$PREBUILT/bin"
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) EXT=.cmd; winpath() { cygpath -m "$1"; } ;;
  *) EXT=; winpath() { printf '%s' "$1"; } ;;
esac
echo "NDK: $ANDROID_NDK_HOME"

# Windows GNU host: the proc-macros (uniffi_macros -> tempfile -> windows-sys) and the bindgen
# compile for the host, and windows-sys' raw-dylib imports need a `dlltool` that rustc finds on
# PATH. rustup's self-contained MinGW one cannot run (it shells out to an `as` that is not
# shipped); the NDK's llvm-dlltool takes the same arguments, so expose it under that name.
if [ -n "$EXT" ] && ! command -v dlltool >/dev/null 2>&1; then
  SHIM="$CORE/target/dlltool-shim"
  mkdir -p "$SHIM"
  cp -f "$BIN/llvm-dlltool.exe" "$SHIM/dlltool.exe"
  export PATH="$SHIM:$PATH"
fi

# 16 KB page alignment, required on Android 15+ devices with 16 KB pages. Target-specific so the
# host-side proc-macro and bindgen links are untouched.
page_flags="-C link-arg=-Wl,-z,max-page-size=16384"

triple_of() {
  case "$1" in
    arm64-v8a) echo aarch64-linux-android ;;
    armeabi-v7a) echo armv7-linux-androideabi ;;
    x86_64) echo x86_64-linux-android ;;
    x86) echo i686-linux-android ;;
    *) echo "unknown ABI $1" >&2; exit 2 ;;
  esac
}
# The clang wrapper is named after the target, with armv7 spelled as the NDK does.
clang_of() {
  case "$1" in
    armv7-linux-androideabi) echo "armv7a-linux-androideabi$API-clang$EXT" ;;
    *) echo "$1$API-clang$EXT" ;;
  esac
}

cd "$CORE"
for abi in "${ABIS[@]}"; do
  triple="$(triple_of "$abi")"
  linker="$(winpath "$BIN/$(clang_of "$triple")")"
  upper="$(echo "$triple" | tr 'a-z-' 'A-Z_')"
  echo "$abi: $triple, linker $linker"
  (
    export "CARGO_TARGET_${upper}_LINKER=$linker" "CARGO_TARGET_${upper}_RUSTFLAGS=$page_flags" \
      "AR_${triple//-/_}=$(winpath "$BIN/llvm-ar")"
    run cargo build --target "$triple" $PROFILE_FLAG --features ios
  )
  mkdir -p "$JNI/$abi"
  cp "$CORE/target/$triple/$PROFILE_DIR/libminband_core.so" "$JNI/$abi/"
done

# Bindings from the first ABI's library: the uniffi metadata is identical across ABIs.
LIB="$CORE/target/$(triple_of "${ABIS[0]}")/$PROFILE_DIR/libminband_core.so"
rm -rf "$GEN/dev/minband/core"
run cargo run --quiet --features ios --bin uniffi-bindgen -- \
  generate --library "$LIB" --language kotlin --out-dir "$GEN" --no-format

echo "ok: $JNI/{${ABIS[*]}}/libminband_core.so and $GEN/dev/minband/core/minband_core.kt"
