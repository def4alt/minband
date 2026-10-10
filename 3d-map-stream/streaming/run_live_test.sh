#!/bin/bash
# Live end-to-end test: phone stream (simulated or real) -> MASt3R-SLAM (home, --live-export) -> map_sender_slot (home)
# -> ssh -R tunnel -> receiver (laptop; headless frames or GPU window).
# Env: RATE (B/s), SLOT (s), PKT (B), PORT, STREAMER (sender), RECV_STREAMER (receiver decoder module), GUI=1, OUT,
#      SRC (sim | http://PHONE_IP:8080/video | rtsp://...), CLIP (clip for the simulation)
set -u
RATE=${RATE:-2500}; SLOT=${SLOT:-1}; PKT=${PKT:-500}; PORT=${PORT:-5561}; STREAMER=${STREAMER:-stream_rt.py}
RECV_STREAMER=${RECV_STREAMER:-decoder_rt.py}; GUI=${GUI:-0}; SRC=${SRC:-sim}; CLIP=${CLIP:-datasets/test/houses_30fps_640.mp4}
CONFIG=${CONFIG:-config/base.yaml}; OUT=${OUT:-/home/matteo/Documents/3d/slam_results/00_LIVE_E2E/test_rt}; SIMPORT=${SIMPORT:-$(( 18100 + RANDOM % 400 ))}
PY=/tmp/claude-1000/-home-matteo-Documents-3d/ec416e8c-37fe-40e2-9d59-d38cd41f70c1/scratchpad/venv_geo/bin/python
R=/home/matteo/Documents/3d/slam_results/research
CONDA='source ~/miniconda3/etc/profile.d/conda.sh && conda activate mast3r-slam'
rm -rf "$OUT"; mkdir -p "$OUT"; T0=$(date +%s)
log(){ echo "[$(( $(date +%s)-T0 ))s] $*" | tee -a "$OUT/timeline.txt"; }
LEFT=$(ssh home 'rm -f /tmp/slam_ready.flag; ps -eo pid,cmd | grep -E "map_sender_slo[t]|main\.py --datase[t]|sim_phone_serve[r]" | cut -c1-90'); LEFTL=$(ps -eo pid,cmd | grep -E "map_receive[r]" | cut -c1-90)
if [ -n "$LEFT$LEFTL" ]; then echo "ABORT: leftover test processes (kill them by PID first):"; echo "home: $LEFT"; echo "laptop: $LEFTL"; exit 2; fi
scp -q "$R/sim_phone_server.py" home:~/MASt3R-SLAM/ 2>/dev/null
log "receiver start (laptop :$PORT, decoder $RECV_STREAMER, gui=$GUI)"
if [ "$GUI" = "1" ]; then
  (cd "$R" && env -u WAYLAND_DISPLAY XDG_SESSION_TYPE=x11 PKT=$PKT "$PY" map_receiver_gui.py --listen 0.0.0.0:$PORT --out "$OUT" --streamer "$RECV_STREAMER" --rate-kbit $(( RATE*8/1000 )) --accept-timeout 900 --idle-timeout 15 --hold 20 --snap-every 0.5 --screenshot "$OUT/gui_screenshot.png" > "$OUT/receiver_stdout.txt" 2>&1 &)
else
  (cd "$R" && PKT=$PKT "$PY" map_receiver.py --listen 0.0.0.0:$PORT --out "$OUT" --streamer "$RECV_STREAMER" --rate-kbit $(( RATE*8/1000 )) --accept-timeout 900 --idle-timeout 15 > "$OUT/receiver_stdout.txt" 2>&1 &)
fi
sleep 2
if [ "$SRC" = "sim" ]; then
  log "simulated phone (port $SIMPORT) + SLAM start on home (frozen frame until SLAM is ready)"
  ssh home "$CONDA && cd ~/MASt3R-SLAM && rm -rf live_out_pkt && mkdir live_out_pkt && (setsid nohup python -u sim_phone_server.py $CLIP $SIMPORT /tmp/slam_ready.flag > sim_phone.log 2>&1 &); sleep 1; (OMP_NUM_THREADS=8 MKL_NUM_THREADS=8 PHONE_STOP_ON_EOF=1 setsid nohup python -u main.py --dataset http://127.0.0.1:$SIMPORT/video --config $CONFIG --no-viz --live-export live_out_pkt > slam_pkt.log 2>&1 &); echo slam launched"
  for i in $(seq 1 300); do n=$(ssh home 'grep -a -c "FPS:" ~/MASt3R-SLAM/slam_pkt.log 2>/dev/null; pgrep -c -f "main\.py --datase[t]"'); set -- $n; [ "${1:-0}" -ge 1 ] && break; if [ "${2:-0}" -eq 0 ]; then log "SLAM died before becoming ready:"; ssh home 'grep -a -E "Error|rror:|Traceback" ~/MASt3R-SLAM/slam_pkt.log ~/MASt3R-SLAM/sim_phone.log | tail -3'; exit 1; fi; sleep 2; done
  ssh home 'touch /tmp/slam_ready.flag'; log "SLAM ready -> clip starts playing"
else
  log "SLAM start on home with phone source $SRC"
  ssh home "$CONDA && cd ~/MASt3R-SLAM && rm -rf live_out_pkt && mkdir live_out_pkt && (setsid nohup python -u main.py --dataset '$SRC' --config $CONFIG --no-viz --live-export live_out_pkt > slam_pkt.log 2>&1 &); echo slam launched"
fi
log "sender start through tunnel (rate $RATE B/s, slot ${SLOT}s, PKT $PKT, $STREAMER)"
timeout 900 ssh -R $PORT:127.0.0.1:$PORT home "$CONDA && cd ~/mapstream && PKT=$PKT python map_sender_slot.py --snapshots ~/MASt3R-SLAM/live_out_pkt --connect 127.0.0.1:$PORT --rate $RATE --slot $SLOT --streamer research/$STREAMER --timeout 15 --connect-timeout 400 --log ~/mapstream/sender_pkt_log.txt" > "$OUT/sender_stdout.txt" 2>&1
log "sender exited ($?)"
scp -q home:~/mapstream/sender_pkt_log.txt home:~/MASt3R-SLAM/slam_pkt.log home:~/MASt3R-SLAM/sim_phone.log "$OUT/" 2>/dev/null
ssh home 'ls -la --time-style=full-iso ~/MASt3R-SLAM/live_out_pkt' > "$OUT/snapshots.txt" 2>/dev/null
for i in $(seq 1 90); do pgrep -f "[m]ap_receiver.*--listen 0.0.0.0:$PORT" >/dev/null || break; sleep 2; done
log "receiver finished"
ssh home 'for p in $(pgrep -f "sim_phone_serve[r]"); do kill $p; done; true' >/dev/null 2>&1   # SLAM exits on its own at stream end
"$PY" - "$OUT" "$RATE" <<'PYEOF'
import sys, os, glob, subprocess, numpy as np
out, rate = sys.argv[1], float(sys.argv[2])
try: recs = [l.split() for l in open(f"{out}/recv_log.txt") if l and l[0].isdigit()]
except FileNotFoundError: print("no recv_log.txt"); sys.exit(0)
rec = [r for r in recs if len(r) > 3 and r[2] == "REC"]
if not rec: print("no records received"); sys.exit(0)
t = np.array([float(r[0]) for r in rec]); b = np.array([int(r[3]) for r in rec]); gaps = np.diff(t) if len(t) > 1 else np.array([0.0])
print(f"records {len(rec)}, bytes {b.sum()} over {t[-1]-t[0]:.1f} s = {8*b.sum()/max(t[-1]-t[0],1e-6)/1e3:.1f} kbit/s, "
      f"updates/s {len(rec)/max(t[-1]-t[0],1e-6):.2f}, gap mean {gaps.mean():.2f}s max {gaps.max():.2f}s, median packet {int(np.median(b))} B, final pts {rec[-1][-1]}")
frames = sorted(glob.glob(f"{out}/recv_frames/frame_*.png")) or sorted(glob.glob(f"{out}/recv_frames/*.png"))
n = min(len(frames), len(rec))
if n > 1:
    with open(f"{out}/concat.txt", "w") as f:
        for i in range(n):
            d = (t[i+1] - t[i]) if i + 1 < n else 2.0
            f.write(f"file '{frames[i]}'\nduration {max(d, 0.05):.3f}\n")
        f.write(f"file '{frames[n-1]}'\n")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", f"{out}/concat.txt", "-vf", "fps=10", "-c:v", "libx264", "-pix_fmt", "yuv420p", f"{out}/live_received_realtime.mp4"])
    print("video:", f"{out}/live_received_realtime.mp4")
PYEOF
log "done"
