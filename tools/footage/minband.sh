#!/usr/bin/env bash
# MinBand replays of a tracks.csv: θ 0.15 and 0.5 m on a clean link, and the telemetry, lora and hf
# link profiles. Writes OUTDIR/minband.jsonl, one {"run", "result"} object per line.
#
#   tools/footage/minband.sh TRACKS.csv OUTDIR
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
csv=$(cd "$(dirname "$1")" && pwd)/$(basename "$1"); out=$2
mkdir -p "$out"; out=$(cd "$out" && pwd)
cd "$here/../eval"
: > "$out/minband.jsonl"
run() {  # a failed replay stops here with its error, instead of writing an empty result
  local name=$1 r; shift
  r=$(node src/replay.ts "$csv" "$@" --json | tail -1)
  [ -n "$r" ] || { echo "replay $name: no result" >&2; return 1; }
  echo "{\"run\": \"$name\", \"result\": $r}" >> "$out/minband.jsonl"
}
run theta0.15 --theta 0.15
run theta0.5 --theta 0.5
run telemetry --budget 450 --loss 0.05 --delay 6
run lora --budget 1500 --loss 0.1 --delay 36
run hf --budget 8000 --loss 0.01 --delay 60
echo "wrote $out/minband.jsonl"
