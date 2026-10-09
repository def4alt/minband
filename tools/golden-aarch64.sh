#!/usr/bin/env bash
# Golden vectors on Linux aarch64 without a Pi (docs/HACKATHON_PLAN.md section 4): cross-compile
# the core's tests (unit + tests/golden) for aarch64-unknown-linux-gnu and run them under
# qemu-user, so the predictor is checked bit for bit on the Raspberry Pi 5's architecture.
# On the Pi itself (or any aarch64 Linux) this is just `cd core && cargo test`, which the script
# then runs natively.
#
#   tools/golden-aarch64.sh                  # all core tests
#   tools/golden-aarch64.sh --test golden    # extra arguments go to `cargo test`
#
# Needs a Linux host with
#   rustup target add aarch64-unknown-linux-gnu
#   apt install gcc-aarch64-linux-gnu libc6-dev-arm64-cross qemu-user
# Linker and runner are set through cargo's per-target environment variables for this run only
# (no .cargo/config change); override them, or AARCH64_SYSROOT, from the environment.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARGET=aarch64-unknown-linux-gnu
SYSROOT="${AARCH64_SYSROOT:-/usr/aarch64-linux-gnu}"

die() { echo "golden-aarch64: $*" >&2; exit 1; }

cd "$ROOT/core"

if [[ "$(uname -s)" == Linux && "$(uname -m)" == aarch64 ]]; then
  echo "==> native aarch64 Linux: cargo test"
  exec cargo test "$@"
fi
[[ "$(uname -s)" == Linux ]] || die "needs Linux with qemu-user (on macOS use a Linux container; on the Pi run: cd core && cargo test)"

export CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER="${CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER:-aarch64-linux-gnu-gcc}"
export CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_RUNNER="${CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_RUNNER:-qemu-aarch64 -L $SYSROOT}"
LINKER="$CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER"
RUNNER="$CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_RUNNER"

if command -v rustup >/dev/null 2>&1; then
  rustup target list --installed | grep -qx "$TARGET" || die "missing Rust target: rustup target add $TARGET"
fi
command -v "$LINKER" >/dev/null 2>&1 || die "missing cross linker $LINKER: apt install gcc-aarch64-linux-gnu libc6-dev-arm64-cross"
command -v "${RUNNER%% *}" >/dev/null 2>&1 || die "missing ${RUNNER%% *}: apt install qemu-user"
[[ -d "$SYSROOT" ]] || die "missing aarch64 sysroot $SYSROOT (libc6-dev-arm64-cross), or set AARCH64_SYSROOT"

echo "==> cargo test --target $TARGET (linker: $LINKER, runner: $RUNNER)"
cargo test --target "$TARGET" "$@"
