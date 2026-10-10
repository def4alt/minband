#!/bin/bash
# Live end-to-end test: phone stream (simulated or real) -> MASt3R-SLAM (home, --live-export) -> map_sender_slot (home)
# -> ssh -R tunnel -> receiver (laptop; headless frames or GPU window).
# Env: RATE (B/s), SLOT (s), PKT (B), PORT, STREAMER (sender), RECV_STREAMER (receiver decoder module, default = STREAMER),
#      GUI=1, OUT, SRC (sim | http://PHONE_IP:8080/video | rtsp://...), CLIP (clip for the simulation)
# Exit codes: 0 pass, 1 SLAM died, 2 leftover processes / ssh failure, 3 no free port, 4 SLAM never ready, 5 sender failed,
#             6 pass criteria not met.  Every abort path kills exactly the PIDs this run started (never by pattern).
set -u
RATE=${RATE:-3750}; SLOT=${SLOT:-0.5}; PKT=${PKT:-500}; PORT=${PORT:-$(( 5600 + RANDOM % 300 ))}; STREAMER=${STREAMER:-stream_rt.py}
RECV_STREAMER=${RECV_STREAMER:-$STREAMER}; GUI=${GUI:-0}; SRC=${SRC:-sim}; CLIP=${CLIP:-datasets/test/houses_30fps_640.mp4}
CONFIG=${CONFIG:-config/base.yaml}; OUT=${OUT:-/home/matteo/Documents/3d/slam_results/00_LIVE_E2E/test_rt}; SIMPORT=${SIMPORT:-$(( 18100 + RANDOM % 400 ))}
SENDER_TIMEOUT=${SENDER_TIMEOUT:-45}; FIRST_TIMEOUT=${FIRST_TIMEOUT:-240}; IDLE_TIMEOUT=${IDLE_TIMEOUT:-60}
PY=${PY:-/tmp/claude-1000/-home-matteo-Documents-3d/ec416e8c-37fe-40e2-9d59-d38cd41f70c1/scratchpad/venv_geo/bin/python}  # laptop python with open3d + constriction + numba
R=/home/matteo/Documents/3d/slam_results/research
CONDA='source ~/miniconda3/etc/profile.d/conda.sh && conda activate mast3r-slam'
SSH="ssh -o ConnectTimeout=15"
rm -rf "$OUT"; mkdir -p "$OUT"; T0=$(date +%s)
log(){ echo "[$(( $(date +%s)-T0 ))s] $*" | tee -a "$OUT/timeline.txt"; }
RECV_PID=""; SIM_PID=""; SLAM_PID=""; FINISHED=0
cleanup(){
  if [ "$FINISHED" != 1 ]; then
    log "cleanup: stopping the processes of this run (receiver '$RECV_PID', home sim '$SIM_PID' slam '$SLAM_PID')"
    [ -n "$RECV_PID" ] && kill -0 "$RECV_PID" 2>/dev/null && kill "$RECV_PID"
    [ -n "${SENDER_SSH:-}" ] && kill -0 "$SENDER_SSH" 2>/dev/null && kill "$SENDER_SSH"
    [ -n "$SIM_PID$SLAM_PID" ] && $SSH home "for p in $SIM_PID $SLAM_PID; do kill -0 \$p 2>/dev/null && kill \$p; done; true" >/dev/null 2>&1
  fi
  return 0
}
trap cleanup EXIT
# ---- leftover check (home: by working directory, the hackathon tree is a teammate's; laptop: any receiver) + ssh liveness
LEFT=$($SSH home 'rm -f /tmp/slam_ready.flag; for p in $(pgrep -f "map_sender_slo[t]|main\.py --datase[t]|sim_phone_serve[r]"); do d=$(readlink /proc/$p/cwd 2>/dev/null); c=$(tr "\0" " " < /proc/$p/cmdline 2>/dev/null); case "$d $c" in *edtm-d4d*) ;; *) [ -n "$c" ] && echo "$p [$d] $(echo "$c" | cut -c1-80)";; esac; done; echo __SSH_OK__')
case "$LEFT" in *__SSH_OK__*) LEFT=${LEFT//__SSH_OK__/};; *) echo "ABORT: ssh to home failed (try: ssh -O exit home)"; exit 2;; esac
LEFTL=$(ps -eo pid,cmd | grep -E "map_receive[r]" | cut -c1-90)
if [ -n "$(echo "$LEFT$LEFTL" | tr -d '[:space:]')" ]; then echo "ABORT: leftover test processes (kill them by PID first):"; echo "home: $LEFT"; echo "laptop: $LEFTL"; exit 2; fi
# ---- pick a port that is free on both ends (a multiplexed ssh -R forward can outlive earlier runs)
for try in 1 2 3 4 5 6; do
  busy=$($SSH home "ss -ltn 2>/dev/null | grep -q ':$PORT ' && echo BUSY || echo FREE"); ss -ltn 2>/dev/null | grep -q ":$PORT " && busy=BUSY
  [ "$busy" = FREE ] && break; log "port $PORT busy (stale tunnel?), trying another"; PORT=$(( 5600 + RANDOM % 300 ))
done
[ "$busy" = FREE ] || { log "ABORT: no free port found"; exit 3; }
scp -q "$R/sim_phone_server.py" home:~/MASt3R-SLAM/ 2>/dev/null
log "receiver start (laptop :$PORT, decoder $RECV_STREAMER, gui=$GUI)"
if [ "$GUI" = "1" ]; then
  ( cd "$R" && exec env -u WAYLAND_DISPLAY XDG_SESSION_TYPE=x11 PKT=$PKT "$PY" -u map_receiver_gui.py --view ${VIEW:-third} --listen 0.0.0.0:$PORT --out "$OUT" --streamer "$RECV_STREAMER" --rate-kbit $(( RATE*8/1000 )) --accept-timeout 900 --idle-timeout $IDLE_TIMEOUT --hold 20 --snap-every 0.5 --screenshot "$OUT/gui_screenshot.png" ) > "$OUT/receiver_stdout.txt" 2>&1 &
else
  ( cd "$R" && exec env PKT=$PKT "$PY" -u map_receiver.py --listen 0.0.0.0:$PORT --out "$OUT" --streamer "$RECV_STREAMER" --rate-kbit $(( RATE*8/1000 )) --accept-timeout 900 --idle-timeout $IDLE_TIMEOUT ) > "$OUT/receiver_stdout.txt" 2>&1 &
fi
RECV_PID=$!; sleep 2
kill -0 $RECV_PID 2>/dev/null || { log "ABORT: receiver died at start:"; tail -5 "$OUT/receiver_stdout.txt"; exit 5; }
start_sender() {   # started right after the SLAM launch: the encoder warm-up (numba compile, ~8 s) overlaps SLAM's model load
  log "sender start through tunnel (rate $RATE B/s, slot ${SLOT}s, PKT $PKT, $STREAMER, warm-up while SLAM loads)"
  SLOG="~/mapstream/sender_pkt_log_$PORT.txt"   # the tunnel uses its OWN ssh connection (no ControlMaster): ssh -O exit/cancel elsewhere cannot kill it
  timeout 900 ssh -o ConnectTimeout=15 -o ControlMaster=no -o ControlPath=none -o ServerAliveInterval=15 -o ExitOnForwardFailure=yes -R $PORT:127.0.0.1:$PORT home "$CONDA && cd ~/mapstream && PKT=$PKT python -u map_sender_slot.py --snapshots ~/MASt3R-SLAM/live_out_pkt --connect 127.0.0.1:$PORT --rate $RATE --slot $SLOT --urgent-bytes ${URGENT:-1000} --alpha-min ${ALPHA_MIN:-0.011} --bucket ${BUCKET:-0.5} --jit ${JIT:-1} --pose-hz ${POSE_HZ:-5} --streamer research/$STREAMER --timeout $SENDER_TIMEOUT --first-timeout $FIRST_TIMEOUT --connect-timeout 400 --log $SLOG --record ~/mapstream/stream_rec_$PORT.bin" > "$OUT/sender_stdout.txt" 2>&1 &
  SENDER_SSH=$!
}
if [ "$SRC" = "sim" ]; then
  log "simulated phone (port $SIMPORT) + SLAM start on home (frozen frame until SLAM is ready)"
  PIDS=$($SSH home "{ $CONDA && cd ~/MASt3R-SLAM && rm -rf live_out_pkt && mkdir live_out_pkt; } || exit 1; setsid nohup python -u sim_phone_server.py $CLIP $SIMPORT /tmp/slam_ready.flag > sim_phone.log 2>&1 < /dev/null & echo SIM=\$!; sleep 1; OMP_NUM_THREADS=8 MKL_NUM_THREADS=8 PHONE_STOP_ON_EOF=1 setsid nohup python -u main.py --dataset http://127.0.0.1:$SIMPORT/video --config $CONFIG --no-viz --live-export live_out_pkt > slam_pkt.log 2>&1 < /dev/null & echo SLAM=\$!")
  SIM_PID=$(echo "$PIDS" | sed -n 's/^SIM=//p'); SLAM_PID=$(echo "$PIDS" | sed -n 's/^SLAM=//p')
  [ -n "$SLAM_PID" ] && [ -n "$SIM_PID" ] || { log "ABORT: launch on home failed: $PIDS"; exit 1; }
  start_sender
  log "launched on home: sim pid $SIM_PID, slam pid $SLAM_PID ($($SSH home "ps -o pid=,cmd= -p $SIM_PID,$SLAM_PID 2>/dev/null | cut -c1-70 | tr '\n' ';'"))"
  ready=0
  for i in $(seq 1 300); do
    n=$($SSH home "c=\$(grep -a -c 'FPS:' ~/MASt3R-SLAM/slam_pkt.log 2>/dev/null); echo \${c:-0}; kill -0 $SLAM_PID 2>/dev/null && echo 1 || echo 0") || { sleep 2; continue; }
    set -- $n; [ -z "${2:-}" ] && { sleep 2; continue; }   # ssh hiccup -> retry, never 'died'
    [ "$1" -ge 1 ] && { ready=1; break; }
    if [ "$2" -eq 0 ]; then log "SLAM died before becoming ready:"; $SSH home 'grep -a -E "Error|rror:|Traceback" ~/MASt3R-SLAM/slam_pkt.log ~/MASt3R-SLAM/sim_phone.log | tail -3'; exit 1; fi
    sleep 2
  done
  [ "$ready" = 1 ] || { log "ABORT: SLAM not ready after 600 s"; exit 4; }
  $SSH home 'touch /tmp/slam_ready.flag'; log "SLAM ready -> clip starts playing"
else
  log "SLAM start on home with phone source $SRC"
  PIDS=$($SSH home "{ $CONDA && cd ~/MASt3R-SLAM && rm -rf live_out_pkt && mkdir live_out_pkt; } || exit 1; setsid nohup python -u main.py --dataset '$SRC' --config $CONFIG --no-viz --live-export live_out_pkt > slam_pkt.log 2>&1 < /dev/null & echo SLAM=\$!")
  SLAM_PID=$(echo "$PIDS" | sed -n 's/^SLAM=//p'); log "launched on home: slam pid $SLAM_PID"
  start_sender
fi
wait $SENDER_SSH; SENDER_RC=$?; log "sender exited ($SENDER_RC)"
scp -q home:$SLOG "$OUT/sender_pkt_log.txt" 2>/dev/null; scp -q home:~/mapstream/stream_rec_$PORT.bin "$OUT/stream_record.bin" 2>/dev/null; scp -q home:~/MASt3R-SLAM/slam_pkt.log home:~/MASt3R-SLAM/sim_phone.log "$OUT/" 2>/dev/null
$SSH home 'ls -la --time-style=full-iso ~/MASt3R-SLAM/live_out_pkt' > "$OUT/snapshots.txt" 2>/dev/null
if [ "$SENDER_RC" -ge 124 ] || ! grep -q "\[sender\] connected" "$OUT/sender_stdout.txt"; then log "FAIL: sender did not run properly (rc $SENDER_RC):"; tail -5 "$OUT/sender_stdout.txt"; exit 5; fi
for i in $(seq 1 90); do kill -0 $RECV_PID 2>/dev/null || break; sleep 2; done
if kill -0 $RECV_PID 2>/dev/null; then log "WARNING: receiver (pid $RECV_PID) still running 180 s after the sender exited; stopping it"; kill $RECV_PID; sleep 3; else log "receiver finished"; fi
if [ -n "$SIM_PID$SLAM_PID" ]; then   # the sim exits after the clip and SLAM on EOF; stop only this run's PIDs if they linger
  LING=$($SSH home "for p in $SIM_PID $SLAM_PID; do kill -0 \$p 2>/dev/null && { echo -n \"\$p \"; kill \$p; }; done; true")
  [ -n "$LING" ] && log "WARNING: home processes of this run still alive, stopped by PID: $LING"
fi
SNAPSTAT=$($SSH home "$CONDA && cd ~/MASt3R-SLAM && python -c \"
import glob, numpy as np
fs = sorted(glob.glob('live_out_pkt/snap_*.npz'))
z = np.load(fs[-1]); m = z['rgb'].mean(0); print(len(fs), len(z['pts']), int(z['kf']) + 1, '%.1f %.1f %.1f' % tuple(m))
\"" 2>/dev/null)
log "last snapshot on home (n_snaps pts keyframes mean_rgb): $SNAPSTAT"
"$PY" - "$OUT" "$RATE" "$STREAMER" "$SNAPSTAT" "$SLOT" <<'PYEOF'
import sys, os, glob, subprocess, re, numpy as np
out, rate, streamer, snapstat, slot = sys.argv[1], float(sys.argv[2]), sys.argv[3], sys.argv[4].split(), float(sys.argv[5])
fails = []
try: recs = [l.split() for l in open(f"{out}/recv_log.txt") if l and l[0].isdigit()]
except FileNotFoundError: recs = []; fails.append("no recv_log.txt")
rec = [r for r in recs if len(r) > 3 and r[2] == "REC"]
if rec:
    t = np.array([float(r[0]) for r in rec]); b = np.array([int(r[3]) for r in rec]); gaps = np.diff(t) if len(t) > 1 else np.array([0.0])
    print(f"records {len(rec)}, bytes {b.sum()} over {t[-1]-t[0]:.1f} s = {8*b.sum()/max(t[-1]-t[0],1e-6)/1e3:.1f} kbit/s, "
          f"updates/s {len(rec)/max(t[-1]-t[0],1e-6):.2f}, gap mean {gaps.mean():.2f}s max {gaps.max():.2f}s, median packet {int(np.median(b))} B, final pts {rec[-1][-1]}")
else: print("no records received"); fails.append("no records received")
# ---- final map vs the last snapshot
npts = 0; mean = None
try:
    with open(f"{out}/recv_final.ply", "rb") as f:
        hdr = b""
        while not hdr.endswith(b"end_header\n"): hdr += f.read(1)
        npts = int(re.search(rb"element vertex (\d+)", hdr).group(1))
        a = np.frombuffer(f.read(), dtype=[("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("r", "u1"), ("g", "u1"), ("b", "u1")], count=npts)
        mean = np.array([a["r"].mean(), a["g"].mean(), a["b"].mean()]) if npts else None
except Exception as e: fails.append(f"recv_final.ply unreadable: {e}")
snap_pts = int(snapstat[1]) if len(snapstat) >= 6 else 0; snap_mean = np.array([float(x) for x in snapstat[3:6]]) if len(snapstat) >= 6 else None
print(f"final map: {npts} pts, mean rgb {None if mean is None else np.round(mean, 1).tolist()}; last snapshot: {snap_pts} pts, mean rgb {None if snap_mean is None else snap_mean.tolist()}")
raw = "raw" in streamer
if raw:
    if snap_pts and npts < 0.95 * snap_pts: fails.append(f"final map {npts} < 95% of snapshot {snap_pts}")
    if mean is not None and snap_mean is not None and np.abs(mean - snap_mean).max() > 10: fails.append("mean colour off by > 10")
else:
    if len(rec) < 150: fails.append(f"only {len(rec)} records (< 150)")
    if npts < 300_000: fails.append(f"final map {npts} pts (< 300k)")
    if mean is not None and snap_mean is not None and np.abs(mean - snap_mean).max() > 15: fails.append("mean colour off by > 15")
# ---- sender side: keyframes ingested, encoder time, budget
try:
    sl = open(f"{out}/sender_pkt_log.txt").read().splitlines()
    snaps = [l for l in sl if " SNAP " in l]; ing = [l for l in snaps if "ingested=1" in l]
    kfs = max([int(re.search(r"kf=(\d+)", l).group(1)) for l in ing] + [-1]) + 1
    cloud = max([int(re.search(r"cloud_pts=(\d+)", l).group(1)) for l in ing] + [0])
    ch = [l for l in sl if " CHUNK " in l]; enc = [float(re.search(r"enc=([\d.]+)s", l).group(1)) for l in ch]
    over = [l for l in ch if " records=" in l and int(re.search(r"bytes=(\d+)", l).group(1)) > float(re.search(r"budget=(\d+)B", l).group(1)) + 1]
    print(f"sender: {len(ing)} snapshots ingested (keyframes {kfs}, cloud {cloud} pts), {len(ch)} chunks, enc max {max(enc) if enc else 0:.2f}s (after first: {max(enc[1:]) if len(enc) > 1 else 0:.2f}s), over-budget chunks {len(over)}")
    if not raw and len(enc) > 1 and max(enc[1:]) > 1.0: fails.append(f"encoder {max(enc[1:]):.2f}s per slot (> 1 s)")
    if not raw and over: fails.append(f"{len(over)} chunks over budget")
    if raw and cloud and npts < 0.95 * cloud: fails.append(f"receiver has {npts} of the sender's {cloud} pts")
except Exception as e: print("sender log not parsed:", e)
tb = subprocess.run(["grep", "-l", "Traceback", *glob.glob(f"{out}/*.txt"), *glob.glob(f"{out}/*.log")], capture_output=True, text=True).stdout.split()
if tb: fails.append("Traceback in " + ", ".join(os.path.basename(x) for x in tb))
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
if fails: print("FAIL: " + "; ".join(fails)); sys.exit(6)
print("PASS")
PYEOF
RC=$?; FINISHED=1; log "done (rc $RC)"; exit $RC
