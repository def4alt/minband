#!/bin/bash
# Replay a run through the real edge/link/receiver (driver DUMP mode) and score it against the eye audit:
#   tools/recon3d/eval/run_eval.sh <run dir> <tag> [driver env, e.g. PROFILE=lora]
# Writes <run>/eval/dump_<tag>.jsonl, score_<tag>.json, misses_<tag>.json and prints the table.
set -e
here=$(cd "$(dirname "$0")" && pwd); repo=$(cd "$here/../../.." && pwd)
run=$(realpath "$1"); tag=$2; shift 2
mkdir -p "$run/eval"
(cd "$repo/tools/recon3d" && env RUN_DIR="$run" PROFILE=${PROFILE:-clean} SEED=${SEED:-1} "$@" DUMP="$run/eval/dump_$tag.jsonl" npx tsx src/driver.ts | tail -1)
"${PY:-$HOME/miniconda3/envs/YOLO-CUDA-13/bin/python}" "$here/score_e2e.py" "$run" "$run/eval/dump_$tag.jsonl" "$here/gt-labrador.json" \
  --movers "$here/movers-labrador.json" --json "$run/eval/score_$tag.json" --misses "$run/eval/misses_$tag.json"
