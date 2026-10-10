#!/usr/bin/env bash
# The frozen battlefield pipeline on one clip, the old pipeline on the same frames, and MinBand.
#
#   tools/footage/run_clip.sh VIDEO OUTDIR
#
# Env: PY (python with onnxruntime, opencv, numpy, scipy; default tools/footage/.venv/bin/python),
#      MODELS (dir with aerial-guardian.onnx; default tools/footage/models).
# Writes, under OUTDIR:
#   tracks.csv summary.json detlog.npy   improved: VisDrone appearance + MTI, fusion, new tracker
#   old/                                 the original detect.py (olddet.py) + --legacy-tracker
#   det-legacy/  det-newtracker/         ablations: new detector with the old / new tracker, no MTI
#   h264.csv baseline_a.json eval/       x264 on the same pixels, tools/eval report
#   */minband.jsonl                      replays (minband.sh)
set -u
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
PY=${PY:-$here/.venv/bin/python}
MODELS=${MODELS:-$here/models}
v=$1; R=$2
mkdir -p "$R"
T="$PY -I $here/track.py"
echo "== detect";  $T detect "$v" --model "$MODELS/aerial-guardian.onnx" --out "$R" > "$R/detect.log" 2>&1 || echo "detect failed"
echo "== mti";     $T mti "$v" --out "$R" > "$R/mti.log" 2>&1 || echo "mti failed"
echo "== track";   $T track "$R" --sources det,mti > "$R/track.log" 2>&1 || echo "track failed"
$T track "$R" --sources det --legacy-tracker --no-overlay --out "$R/det-legacy" > "$R/track-det-legacy.log" 2>&1 || echo "det-legacy failed"
$T track "$R" --sources det --out "$R/det-newtracker" > "$R/track-det-newtracker.log" 2>&1 || echo "det-newtracker failed"
echo "== old pipeline"
$PY -I "$here/olddet.py" detect "$v" --model "$MODELS/aerial-guardian.onnx" --tag old --tile 640 --conf 0.15 --out "$R" > "$R/detect-old.log" 2>&1 || echo "old detect failed"
$T track "$R" --sources old --legacy-tracker --no-overlay --out "$R/old" > "$R/track-old.log" 2>&1 || echo "old track failed"
echo "== metrics"
$PY -I "$here/audit.py" metrics "$R" "$R/old" "$R/det-legacy" "$R/det-newtracker" > "$R/audit-metrics.log" 2>&1 || echo "metrics failed"
echo "== x264 and eval"
dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$v")
bash "$here/h264.sh" "$v" 0 "$dur" "$R" > "$R/h264.log" 2>&1 || echo "h264 failed"
(cd "$repo/tools/eval" && npm run -s eval -- --gt "$R/tracks.csv" --baseline-a "$R/baseline_a.json" --out "$R/eval" > "$R/eval.log" 2>&1) || echo "eval failed"
echo "== replays"
for d in "$R" "$R/old" "$R/det-legacy" "$R/det-newtracker"; do
  bash "$here/minband.sh" "$d/tracks.csv" "$d" > /dev/null 2>&1 || echo "replay failed for $d"
done
echo "== done $R"
