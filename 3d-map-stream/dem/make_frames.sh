#!/usr/bin/env bash
# Frames for the DEM experiment: MEVA uav1 clip (3840x2160, 29.97 fps) -> 960x540 JPEG at 10 fps, TUM-style rgb.txt.
#   clip30 = 170-200 s (most camera motion), full = whole 204 s flight.
# usage: dem/make_frames.sh [VIDEO] [OUT_ROOT]   (defaults: tools/footage/clips/...uav1.mp4, runs/dem)
# Fetch the video first: cd tools/footage && python meva.py fetch 2018-03-13.16-00-14 clips/
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
V=${1:-$ROOT/tools/footage/clips/2018-03-13.16-00-14.16-03-38.uav1.mp4}
OUT=${2:-$ROOT/runs/dem}
for spec in clip30:170:30 full:0:205; do
  IFS=: read -r name ss dur <<< "$spec"
  d=$OUT/frames_$name
  mkdir -p "$d/rgb"
  ffmpeg -v error -y -ss "$ss" -t "$dur" -i "$V" -vf "fps=10,scale=960:540" -q:v 2 "$d/rgb/%05d.jpg"
  (cd "$d" && ls rgb | awk -v ss="$ss" '{printf "%.6f rgb/%s\n", ss + (NR-1)/10.0, $1}' > rgb.txt)
  echo "$name: $(wc -l < "$d/rgb.txt") frames -> $d"
done
