#!/bin/bash
# Replay a recorded live run into the GUI viewer on this laptop (no network: sender timing only).
# Usage: [VIEW=first|third|top] bash replay_view.sh RUN_DIR [SPEED]   (RUN_DIR has stream_record.bin from run_live_test.sh)
set -u; RUN=${1:?run dir}; SPEED=${2:-1}; R=$(cd "$(dirname "$0")" && pwd)
PY=${PY:-/tmp/claude-1000/-home-matteo-Documents-3d/ec416e8c-37fe-40e2-9d59-d38cd41f70c1/scratchpad/venv_geo/bin/python}  # laptop python with open3d + constriction + numba
PORT=$(( 6100 + RANDOM % 300 )); OUT="$RUN/replay"; mkdir -p "$OUT"
STREAMER=$(grep -o "streamer=[^ ]*" "$RUN/sender_pkt_log.txt" 2>/dev/null | head -1 | cut -d= -f2); STREAMER=${STREAMER:-stream_rt.py}
RATE=$(grep -o "rate=[0-9.]*" "$RUN/sender_pkt_log.txt" 2>/dev/null | head -1 | cut -d= -f2 | cut -d. -f1); RATE=${RATE:-6250}
echo "replay $RUN/stream_record.bin -> GUI on :$PORT (decoder $STREAMER, speed $SPEED)"
( cd "$R" && exec env -u WAYLAND_DISPLAY XDG_SESSION_TYPE=x11 PKT=500 "$PY" -u map_receiver_gui.py --view ${VIEW:-third} --listen 127.0.0.1:$PORT --out "$OUT" --streamer "$STREAMER" --rate-kbit $(( RATE*8/1000 )) --accept-timeout 60 --idle-timeout 30 --hold 15 --snap-every 0.5 --screenshot "$OUT/gui_screenshot.png" ) > "$OUT/receiver_stdout.txt" 2>&1 &
RP=$!
"$PY" "$R/map_replay.py" "$RUN/stream_record.bin" --connect 127.0.0.1:$PORT --speed "$SPEED"
wait $RP; echo "receiver done: $(tail -1 "$OUT/receiver_stdout.txt")"
if ls "$OUT"/recv_frames/shot_*.png >/dev/null 2>&1; then
  ffmpeg -y -loglevel error -framerate 2 -pattern_type glob -i "$OUT/recv_frames/shot_*.png" -vf "scale=trunc(iw/2)*2:trunc(ih/2)*2" -c:v libx264 -pix_fmt yuv420p "$OUT/replay.mp4" && echo "video: $OUT/replay.mp4"
fi
