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
