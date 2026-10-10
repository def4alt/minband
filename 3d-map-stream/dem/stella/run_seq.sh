#!/usr/bin/env bash
# stella_vslam (maintained OpenVSLAM fork) on a make_frames.sh sequence, paced in real time like ../orbslam3,
# then the map in ORB-SLAM3 export format for fuse.py.
# usage: STELLA=~/src/github.com/stella-cv dem/stella/run_seq.sh SEQ_DIR OUT_DIR [CONFIG.yaml]   (PY: python with msgpack)
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ST=${STELLA:-$HOME/src/github.com/stella-cv}
SEQ=$1; OUT=$2; CFG=${3:-$HERE/camera_960.yaml}; PY=${PY:-python3}
mkdir -p "$OUT"; cp "$CFG" "$OUT/settings.yaml"
T0=$(awk '!/^#/{print $1; exit}' "$SEQ/rgb.txt")  # timestamps then match rgb.txt (fps from the config)
t0=$(date +%s)
DYLD_LIBRARY_PATH=$HOME/.local/stella/lib:$HOME/.local/orbslam3/lib "$ST/stella_vslam_examples/build/run_image_slam" \
  -v "$ST/FBoW_orb_vocab/orb_vocab.fbow" -d "$SEQ/rgb" -c "$OUT/settings.yaml" -t "$T0" --viewer none --auto-term \
  --eval-log-dir "$OUT" -o "$OUT/map.msg" > "$OUT/run.log" 2>&1
echo "wall_s_process $(( $(date +%s) - t0 ))" > "$OUT/timing.txt"
$PY "$HERE/export_map.py" "$OUT"
$PY - "$OUT" <<'EOF' >> "$OUT/timing.txt"
import sys, numpy as np
t = np.loadtxt(sys.argv[1] + "/track_times.txt") * 1000
print(f"frames {len(t)}\ntrack_ms_median {np.median(t):.2f}\ntrack_ms_p90 {np.percentile(t, 90):.2f}")
EOF
n_ok=$(wc -l < "$OUT/frame_trajectory.txt"); echo "frames_with_pose $n_ok" >> "$OUT/timing.txt"
cat "$OUT/timing.txt"
