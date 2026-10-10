#!/usr/bin/env bash
# End-to-end DEM experiment on prepared frames (make_frames.sh): ORB-SLAM3 mono -> Depth Anything V2 Small fusion,
# optionally MoGe2-Aerial, then metrics and the comparison sheet.
# usage: ORB_SLAM3=~/src/github.com/UZ-SLAMLab/ORB_SLAM3 dem/run_pipeline.sh [OUT_ROOT] [--moge] [--mast3r MAP.ply]
#   OUT_ROOT defaults to runs/dem; PY defaults to python3; ORB-SLAM3 must be built with orbslam3/minband_export.patch.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
OUT=$ROOT/runs/dem
MOGE=0; MAST3R=""
while [ $# -gt 0 ]; do
  case $1 in
    --moge) MOGE=1 ;;
    --mast3r) MAST3R=$2; shift ;;
    *) OUT=$1 ;;
  esac
  shift
done
PY=${PY:-python3}
OUT=$(mkdir -p "$OUT" && cd "$OUT" && pwd)
[ -n "$MAST3R" ] && MAST3R=$(cd "$(dirname "$MAST3R")" && pwd)/$(basename "$MAST3R")
ORB=${ORB_SLAM3:?set ORB_SLAM3 to the patched ORB_SLAM3 checkout}
cd "$HERE"

for seq in clip30 full; do
  echo "== ORB-SLAM3 $seq"
  "$ORB/run_seq.sh" "$OUT/frames_$seq" "$OUT/orbslam3/$seq" "$HERE/orbslam3/camera_960.yaml"
  grep -E "track_ms_median|frames_ok|frames_lost|first_ok_frame|big_map_changes" "$OUT/orbslam3/$seq/timing.txt" | tr '\n' ' '; echo
  echo "== fuse Depth Anything V2 Small $seq"
  $PY fuse.py --frames "$OUT/frames_$seq" --slam "$OUT/orbslam3/$seq" --out "$OUT/vo_da_${seq}_uv" --fit affine_uv
done

ROWS=("$OUT/vo_da_full_uv=ORB-SLAM3 + Depth Anything V2 Small (full flight)")
WARP=("$OUT/vo_da_full_uv")
if [ -n "$MAST3R" ]; then
  echo "== MASt3R reference DEM"
  $PY dem_from_cloud.py "$MAST3R" --out "$OUT/mast3r_full"
  ROWS=("$OUT/mast3r_full=MASt3R-SLAM (full flight)" "${ROWS[@]}"); WARP=("$OUT/mast3r_full" "${WARP[@]}")
fi
ALT=72.7
if [ "$MOGE" = 1 ]; then
  echo "== fuse MoGe2-Aerial clip30 + single frame"
  $PY fuse.py --frames "$OUT/frames_clip30" --slam "$OUT/orbslam3/clip30" --out "$OUT/vo_moge_clip30_uv" --fit affine_uv --model moge2aerial
  F=$(awk '{print $2}' "$OUT/frames_clip30/rgb.txt" | sed -n 150p)
  $PY single_frame.py "$OUT/frames_clip30/$F" --out "$OUT/moge_single_frame"
  ALT=$($PY -c "import json;print(json.load(open('$OUT/vo_moge_clip30_uv/meta.json'))['camera_altitude_m'])")
  ROWS+=("$OUT/vo_moge_clip30_uv=ORB-SLAM3 + MoGe2-Aerial (30 s clip)" "$OUT/moge_single_frame=MoGe2-Aerial, one frame, no SLAM (metres)")
  WARP+=("$OUT/vo_da_clip30_uv" "$OUT/vo_moge_clip30_uv")
fi

echo "== metrics (heights in camera altitudes; objects in metres assuming altitude $ALT m)"
$PY warp.py "${WARP[@]}"
OBJ=(); for d in "${WARP[@]}"; do OBJ+=("$d:$ALT"); done
[ "$MOGE" = 1 ] && OBJ+=("$OUT/moge_single_frame:1")
$PY objects.py "${OBJ[@]}"
$PY compose.py "$OUT/cmp_all.png" "${ROWS[@]}"
