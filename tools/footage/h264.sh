#!/usr/bin/env bash
# Measure what H.264 needs for a piece of footage (Baseline A on the same pixels MinBand tracked).
#
#   tools/footage/h264.sh VIDEO START END OUTDIR [CRF...]
#
# Encodes VIDEO from START to END seconds with libx264 the way a live downlink would (veryfast,
# tune zerolatency, keyframe every 2 s of the clip's own frame rate, no B-frames), at native
# resolution and at 720p, 480p and 360p below it (never upscaled), for each CRF (default 23 and 28:
# good, and acceptable for overwatch). The bitrate is the raw
# Annex-B bitstream size * 8 / duration, so no container overhead is counted. Writes
#   OUTDIR/h264.csv          one row per resolution x CRF
#   OUTDIR/baseline_a.json   the CRF 23 rows in the format tools/eval and the server read
#                            (tools/eval/src/baselines.ts loadBaselineA): ids h264_720p/480p/360p
#                            and h264_native.
# This is x264 on a laptop, not the phone's VideoToolbox, and quality-targeted (CRF) rather than
# bitrate-targeted: it answers "how many bits does video of this scene need to look right".
set -euo pipefail
[[ $# -ge 4 ]] || { sed -n '2,15p' "$0"; exit 2; }
video=$1 start=$2 end=$3 out=$4; shift 4
if [[ $# -gt 0 ]]; then crfs=("$@"); else crfs=(23 28); fi
mkdir -p "$out"
dur=$(awk -v a="$start" -v b="$end" 'BEGIN { print b - a }')
src_w=$(ffprobe -v error -select_streams v:0 -show_entries stream=width -of csv=p=0 "$video")
src_h=$(ffprobe -v error -select_streams v:0 -show_entries stream=height -of csv=p=0 "$video")
# The clip's own frame rate (drone footage comes at 7.5, 25, 29.97 fps); keyframes every 2 s of it.
fps=$(ffprobe -v error -select_streams v:0 -show_entries stream=r_frame_rate -of csv=p=0 "$video" | awk -F/ '{ printf "%.3f", ($2 ? $1 / $2 : $1) }')
gop=$(awk -v f="$fps" 'BEGIN { printf "%d", f * 2 + 0.5 }')
echo "resolution,width,height,crf,bytes,duration_s,bps" > "$out/h264.csv"
for crf in "${crfs[@]}"; do
  for res in native 720p 480p 360p; do
    case $res in
      native) w=$src_w h=$src_h ;;
      720p) w=1280 h=720 ;;
      480p) w=854 h=480 ;;
      360p) w=640 h=360 ;;
    esac
    # No upscaling: a 596x336 source has a native row and nothing above it.
    if [[ $res != native && $h -ge $src_h ]]; then continue; fi
    bs="$out/$res-crf$crf.h264"
    ffmpeg -nostdin -loglevel error -y -ss "$start" -t "$dur" -i "$video" -an \
      -vf "scale=$w:$h:flags=area" -c:v libx264 -preset veryfast -tune zerolatency \
      -crf "$crf" -g "$gop" -keyint_min "$gop" -bf 0 -pix_fmt yuv420p -f h264 "$bs"
    bytes=$(stat -c %s "$bs")
    bps=$(awk -v b="$bytes" -v d="$dur" 'BEGIN { printf "%d", b * 8 / d }')
    echo "$res,$w,$h,$crf,$bytes,$dur,$bps" >> "$out/h264.csv"
    echo "$res ${w}x$h crf $crf: $(awk -v b="$bps" 'BEGIN { printf "%.0f kbit/s", b / 1000 }')"
    rm -f "$bs"
  done
done
name=$(basename "$video")
awk -F, -v src="$name" -v s="$start" -v e="$end" -v fps="$fps" -v gop="$gop" 'NR > 1 && $4 == 23 {
    id = ($1 == "native") ? "h264_native" : "h264_" $1
    label = ($1 == "native") ? "H.264 " $3 "p (native)" : "H.264 " $1
    rows = rows (rows ? ",\n" : "") sprintf("  { \"id\": \"%s\", \"label\": \"%s\", \"resolution\": \"%sx%s\", \"bps\": %d, \"fps\": %s, \"source\": \"measured: libx264 veryfast zerolatency CRF 23, keyint %s (2 s), no B-frames, on %s %s-%s s\" }", id, label, $2, $3, $7, fps, gop, src, s, e)
  } END { print "{ \"entries\": [\n" rows "\n] }" }' "$out/h264.csv" > "$out/baseline_a.json"
echo "wrote $out/h264.csv and $out/baseline_a.json"
