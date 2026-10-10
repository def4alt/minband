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
run() { local name=$1; shift; echo "{\"run\": \"$name\", \"result\": $(node src/replay.ts "$csv" "$@" --json 2>/dev/null | tail -1)}" >> "$out/minband.jsonl"; }
run theta0.15 --theta 0.15
run theta0.5 --theta 0.5
run telemetry --budget 450 --loss 0.05 --delay 6
run lora --budget 1500 --loss 0.1 --delay 36
run hf --budget 8000 --loss 0.01 --delay 60
echo "wrote $out/minband.jsonl"
