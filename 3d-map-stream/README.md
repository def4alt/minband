# 3d-map-stream — live 3D map over a 10–20 kbit/s link

Phone/drone video → MASt3R-SLAM (RTX 5090 box) → coloured point cloud → LOD voxels + entropy coding → per-keyframe chunks /
sub-second packets over a rate-limited socket → receiver renders the map from any viewpoint. Only the map bytes cross the link
(35 KB for a 23 s flight at 10 kbit/s; the raw map is 7.8 MB).

## Layout

| Folder | What |
|---|---|
| `slam/` | MASt3R-SLAM additions: `phone_stream.py` (live phone camera source: http/rtsp URL or virtual webcam), `live_export.py` + `main.py.patch` (`--live-export DIR` writes a map snapshot after every keyframe), `install_5090.sh` (CUDA 12.8 / torch cu128 / sm_120 build), `PHONE.md` (how to run). |
| `codec/` | Lossless voxel codec study. `codec_combo.py` = octree occupancy with a context-modelled range coder (`constriction`, ~2.7 bits/voxel) + 16-colour palette context coder (~2.5 bits/voxel); `verify.py` is its harness. |
| `streaming/` | The live streamer and its benchmarks. `stream_best.py` (verified best: LOD bands 0.5/1/2/4 m by distance to the flight path, CONF density pruning, coding statistics carried across chunks), `stream_best_rec.py` (same, per-band records applied one by one → first image after ~1.3 s), `stream_lod_conf.py` (baseline). Benchmarks: `live_eval.py` (causal per-slot harness: LIVE / FINAL / time-averaged operator view), `quality.py` (render-based PSNR/SSIM/holes), `live_sim.py`, `subchunk_sim.py`. Transport: `sender/map_sender.py` (5090 box: snapshot → chunk → records at N bytes/s over TCP), `map_receiver.py` (laptop: applies records, renders, logs, writes the final map), `fake_sender.py` (local test). Demos: `demo_live_render*.py` (side-by-side video: full map vs what has arrived). |
| `tools/` | `fly_higher.py` (flythrough renderer), `view.py` (Open3D viewer; needs `env -u WAYLAND_DISPLAY`), `voxel_codec.py`, `map_bandwidth.py`. |
| `results/` | `live_eval` JSON results of every streamer variant. |
| `docs/` | Findings and READMEs of the delivered configurations (10 kbit/s live, 20 kbit/s offline). |

## Numbers (23 s test flight, suburb, drone ~38 m up)

| | bytes | operator view | final map |
|---|---|---|---|
| 10 kbit/s, per-keyframe chunks (`stream_best`) | 35.9 KB | PSNR 18.0 | PSNR 19.0 |
| 20 kbit/s, 1 s planning slots | 71.8 KB | time-avg 17.5, first image 1.0 s | PSNR 19.2 |

Reference: 0.7 m voxels of the whole map = 91 KB, PSNR 16.6. Keep 16 colours (8 loses the red roofs); no colour blending (looks mushy).


## Update: 20 kbit/s, 1 s planning slots, 500 B packets (`streaming/stream_best_pkt.py`)

`PKT=500 python streaming/live_eval_pkt.py streaming/stream_best_pkt.py --rate 20 --slot 1`: 72.4 KB for the flight, 174 packets
= 6 updates/s, first image after 0.31 s, +5.3 % packet overhead, operator view PSNR 18.2, final map 19.2. Record-by-record decode
verified identical to whole-chunk decode (`streaming/test_records_pkt.py`). Packet sizes measured: 1000 B 3.7/s (+1.3 %),
500 B 6.0/s (+5.3 %), 250 B 10.9/s (+11.5 %) — 500 B is the sweet spot.

## SLAM input frame rate

MASt3R-SLAM on the same flight at 7 / 2 / 1 fps input: 5 / 5 / 4 keyframes, 523k / 511k / 418k points, no tracking loss.
2 fps gives the same map as 7 fps on a smooth drone flight (GPU mostly idle); use 4–5 fps for hand-held video with quick turns.

## Run

```
# SLAM box (conda env mast3r-slam, see slam/PHONE.md)
python main.py --dataset http://PHONE_IP:8080/video --config config/base.yaml --no-viz --live-export live_out
python streaming/sender/map_sender.py --snapshots live_out --connect 127.0.0.1:5555 --rate 2500 --streamer stream_best_rec.py
# laptop (python with numpy, scipy, open3d, Pillow, constriction)
python streaming/map_receiver.py --listen 0.0.0.0:5555 --out run/
# tunnel: run the sender inside  ssh -R 5555:127.0.0.1:5555 <slam-box> '...'
# benchmark a streamer offline (needs maps/houses_7fps.ply/.txt next to quality.py, not in the repo)
python streaming/live_eval.py streaming/stream_best.py --rate 20 --slot 1
```
Limitations: encode 1–4 s per slot (pure Python), SLAM pose corrections after a chunk was sent are not handled, one receiver per stream.

## Live end-to-end (2026-10-10 evening): it runs

`streaming/run_live_test.sh` drives the whole chain: phone stream (real URL or `streaming/sim_phone_server.py`, which holds a frozen
frame until SLAM prints its first `FPS:` line) -> MASt3R-SLAM with `--live-export` (`slam/live_export.py`, per-keyframe offsets) ->
`streaming/sender/map_sender_slot.py` (append-only keyframe ingestion: waits for the 2nd keyframe because MASt3R-SLAM rescales the
first one, then adds each new keyframe's points once and never re-sends refinements; `stream_rt.py` encoder, 1 s slots, 500 B packets,
token-bucket rate limit) -> `ssh -R` tunnel -> `streaming/map_receiver.py` (or `map_receiver_gui.py`, Open3D at ~28 fps) with
`stream_rt`'s decoder. Measured at 20 kbit/s: 176 packets, 18.6 kbit/s on the wire, 5.4 updates/s (gap 0.19 s mean / 0.57 s max),
encoder 0.2-0.6 s per 1 s slot (8 s one-time setup), 393k received points with correct colours.

Live-specific findings: (1) Python stdout is block-buffered when redirected - launch SLAM with `python -u` or readiness checks on its
log hang forever; (2) numba's on-disk cache breaks when a module is loaded under different names in different processes -
`cache=False` in the deployed copies; (3) `stream_rt` caches the transposed colour array (`CfT`) - the sender refreshes it when it
swaps the cloud; (4) the encoder's occupancy grids must be growable (the live cloud grows); (5) between keyframes 19-32 % of the
already-sent map moves by >0.5 m (and 88 % after keyframe 1), so a world-fixed voxel stream must send each keyframe once and ignore
refinements - or anchor voxels to keyframes (not done); (6) `decoder_rt.py` is bit-exact offline but does not parse the live header yet.

## Update 2026-10-11: real-time live pipeline (warm sender, live pose, budget-matched, viewer, replay)

What changed since the 2026-10-10 live run (all in `streaming/` and `slam/`):

| Part | Change |
|---|---|
| SLAM (`slam/main.py.patch`, `slam/live_export.py`) | Every tracked frame writes its pose to `<live-export dir>/pose_live.bin` (atomic, ~20 Hz). Keyframe export copies each keyframe under the shared lock and caps 150k points per keyframe. |
| Sender (`streaming/sender/map_sender_slot.py`) | Encoder warm-up at start (numba compile + setup on a synthetic scene, ~11 s, overlaps SLAM model load; first keyframe then costs 0.1 s instead of 8 s). Incremental ingestion of new keyframes (byte-identical output, 65–95 ms instead of up to 1.2 s). numpy hugepage madvise off (0.2–1 s allocation stalls on the 5090 box). A new keyframe is encoded at once with a ~1 KB coarse first look (`--urgent-bytes`). Just-in-time slots (`--jit 1`): never encode more than the link can send before the next slot, so a new keyframe never waits behind queued data. Live pose feed at `--pose-hz` (5 Hz, priority slot, counted in the budget). 1 Hz `STAT` frame (keyframes, cloud size, encode time, backlog, "nothing to send" / "waiting for SLAM"). `--record FILE` writes every frame with its send time. TCP_NODELAY. |
| Codec (`streaming/stream_rt.py`) | `ALPHA_MIN` / `--alpha-min` resolution floor so the content rate stays under the link budget (no refinement tail); first-look budget can no longer go below one packet. |
| Receivers (`streaming/map_receiver*.py`) | Skip `STAT` frames. GUI: solid shaded voxel cubes (`--draw cubes`, default; `hybrid` = old flat tiles), chunked geometry uploads, view modes (keys `1` first person, `3` third person, `T` top, `V` cycle, `F` free mouse; `--view`), network statistics panel (link kbit/s vs budget, 60 s graph, packets/s, pose Hz and age, sender status, "why is nothing moving" line), panel composited into the recorded frames. |
| Replay (`streaming/map_replay.py`, `streaming/replay_view.sh`) | Plays a recorded stream into the viewer with the sender's timing (no network jitter): `VIEW=first bash streaming/replay_view.sh <run dir> [speed]`. |
| Raw baseline (`streaming/stream_raw.py`) | No voxels, no compression (float32 xyz + rgb per new keyframe) — for "is the live SLAM map itself OK?". |
| Harness (`streaming/run_live_test.sh`) | Defaults: 30 kbit/s, 0.5 s slots, alpha floor 0.011, bucket 0.5 s, JIT on. Sender starts together with SLAM (warm-up during model load). Own ssh connection for the tunnel, PID-only cleanup, PASS/FAIL summary. Env: `RATE SLOT URGENT ALPHA_MIN BUCKET JIT POSE_HZ VIEW GUI CLIP SRC PY`. |

Measured (live over the laptop↔5090 ssh tunnel; church clip 29 s, 50 kbit/s cap, alpha floor 0.011):
keyframe written by SLAM → first packet sent **0.55 s median**, → on screen **0.78 s median**; queue ahead of a new keyframe
0 B median; average **27.7 kbit/s** total (map 25.7, pose 1.6, status 0.5); 0 decode errors. The ssh tunnel over the
internet adds 0.17–3 s spikes of its own (bare probe, no app traffic) — use the replay to judge the pipeline alone.
SLAM needs ~5 s of flight before its 2nd keyframe; nothing is sent before that (the 1st keyframe is rescaled when the 2nd arrives).

Known limits: the viewer falls back to software OpenGL (llvmpipe) when started from a sandboxed shell — run it from a normal
desktop terminal for GPU rendering. Large oblique scenes (1.9M SLAM points) make one encode take 0.6–0.9 s.
