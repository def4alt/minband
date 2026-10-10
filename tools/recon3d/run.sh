#!/bin/bash
# The whole tools/recon3d pipeline on one image sequence or video:
#   tools/recon3d/run.sh <frames dir (*.jpg) | video.mp4> <run dir, e.g. runs/recon3d/vd263> [fps=30]
# Stages (README.md): clip -> SLAM export (mast3r-slam env) -> YOLO seg + ByteTrack (YOLO-CUDA-13 env) ->
# lift to 3D, tracks, chips, static map -> browser video. Env names and the MASt3R-SLAM checkout are
# overridable: SLAM_DIR, SLAM_ENV, YOLO_ENV.
set -eo pipefail   # no -u: conda activate scripts read unset variables
src=$1; run=$(realpath -m "$2"); fps=${3:-30}
here=$(cd "$(dirname "$0")" && pwd)
SLAM_DIR=${SLAM_DIR:-$HOME/MASt3R-SLAM}; SLAM_ENV=${SLAM_ENV:-mast3r-slam}; YOLO_ENV=${YOLO_ENV:-YOLO-CUDA-13}
source "$(conda info --base 2>/dev/null || echo "$HOME/miniconda3")/etc/profile.d/conda.sh"
mkdir -p "$run"
if [ -s "$run/clip.mp4" ]; then
  echo "clip exists: $run/clip.mp4"
elif [ -d "$src" ]; then
  first=$(ls "$src" | grep -E '\.jpg$' | sort | head -1); pat=${first%%[0-9]*.jpg}; digits=$(( ${#first} - ${#pat} - 4 ))
  ffmpeg -loglevel error -y -framerate "$fps" -i "$src/${pat}%0${digits}d.jpg" -vf "crop=trunc(iw/2)*2:trunc(ih/2)*2:0:0" -c:v libx264 -preset slow -crf 12 -pix_fmt yuv420p "$run/clip.mp4"
else
  ffmpeg -loglevel error -y -i "$src" -r "$fps" -vf "crop=trunc(iw/2)*2:trunc(ih/2)*2:0:0" -c:v libx264 -preset slow -crf 12 -pix_fmt yuv420p -an "$run/clip.mp4"
fi
echo "[1/4] SLAM $(date +%T)"
(cd "$SLAM_DIR" && conda activate "$SLAM_ENV" && python "$here/slam_export.py" --dataset "$run/clip.mp4" --out "$run/slam" > "$run/slam_run.txt" 2>&1) || { tail -20 "$run/slam_run.txt"; exit 1; }
tail -1 "$run/slam_run.txt"
echo "[2/4] detect $(date +%T)"
conda activate "$YOLO_ENV"
(cd "$here" && python detect.py "$run") | tail -1
echo "[3/4] lift $(date +%T)"
python "$here/lift.py" "$run" --fps "$fps" | tail -4
echo "[4/4] browser video $(date +%T)"
# A keyframe every 0.5 s: the page re-seeks the video to the replay clock.
ffmpeg -loglevel error -y -i "$run/clip.mp4" -vf scale=1280:-2 -c:v libx264 -preset veryfast -crf 23 -g $((fps / 2)) -an -movflags +faststart "$run/clip-720p.mp4"
echo "done $(date +%T): RUN_DIR=$run npm start (in tools/recon3d)"
