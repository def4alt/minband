# dem: light 2.5D elevation map (VO + monocular depth) vs MASt3R

Question: can a light onboard pipeline (Jetson Orin class) replace MASt3R-SLAM for the operator's map?
Pipeline: ORB-SLAM3 monocular poses and sparse map points -> Depth Anything V2 Small relative depth per keyframe ->
per-keyframe fit of `1/z = a*d + b + c*u + e*v` to the map points that keyframe observes -> back-project ->
top-surface raster in a RANSAC ground-plane grid -> median over keyframes. Compared against a DEM rasterised from
the MASt3R-SLAM point cloud, and against MoGe2-Aerial (UAV-tuned metric depth, ViT-L + LoRA).

Footage: MEVA uav1 (`tools/footage`, `meva.py fetch 2018-03-13.16-00-14 clips/`), no IMU, so ORB-SLAM3 runs
visual-only. Intrinsics are estimated from MASt3R pointmaps: f = 2190 px at 3840 wide, HFOV ~82 deg, i.e. 548 px at 960x540
(`orbslam3/camera_960.yaml`, no distortion).

## Files

| file | what |
|---|---|
| `make_frames.sh` | video -> `runs/dem/frames_{clip30,full}` (960x540 JPEG, 10 fps, TUM `rgb.txt`); other footage via `SPECS`, `SIZE` |
| `orbslam3/minband_export.patch` | applies to UZ-SLAMLab/ORB_SLAM3 @ 4452a3c: `System::ExportMonoMap` (map points, keyframe observations), headless `mono_seq` runner, `run_seq.sh`, clang/gcc-portable fixes (`stdint.h`, `int mnFullBAIdx`) |
| `run_pipeline.sh` | ORB-SLAM3 on both sequences -> `fuse.py` -> optional MoGe2-Aerial / MASt3R reference -> `warp.py`, `objects.py`, `compose.py` |
| `depth.py` | Depth Anything V2 (small/base, relative), MoGe2-Aerial (metric) and Depth Anything 3 (one view, or a window of SLAM-posed views) wrappers; CUDA > MPS > CPU |
| `fuse.py` | the fusion above; `--fit none/scale/affine/affine_uv`, `--model small/base/moge2aerial/da3small/da3base/da3metric`, DA3 `--window N --window-mode causal/centered --process-res` |
| `depth_video.py` | DA3 debug video: DA3-Small and DA3Metric-Large depth per frame vs the projected SLAM map points, plus a predicted/SLAM table per depth bin (`*_bins.json`) |
| `stella/` | stella_vslam (maintained OpenVSLAM fork): `build_mac.sh` + `macos.patch` (headless arm64), `run_seq.sh`, `export_map.py` (msgpack map -> the ORB-SLAM3 export files), camera configs |
| `orbslam3/camera_houses.yaml` | second clip, `houses_flythrough.mp4` 960x538, f 642 px (MoGe2-Aerial; it gives 549 px on uav1, MASt3R 548) |
| `dem_from_cloud.py` | DEM from a point cloud + trajectory (the MASt3R reference) |
| `single_frame.py` | SLAM-free DEM from one frame with MoGe2-Aerial (heights in metres) |
| `grid.py` | ground plane, rasterisation, relief/hillshade, oblique splat render |
| `warp.py` | ground warp (low-passed ground minus plane, p95-p5 and rms, in camera altitudes) |
| `objects.py` | nDSM objects > 3 m: area and p90 height (same building across pipelines) |
| `compose.py` | comparison sheet (ortho, relief, oblique per row) |

Heights are scale-free: units of the median camera altitude above the ground plane. MoGe2-Aerial gives metres per
SLAM unit (`m_per_unit_median` in `meta.json`), which converts them (camera ~73 m up on this clip).

## Results on an Apple M4 (16 GB, MPS), 2026-10-10

| | MASt3R-SLAM | ORB-SLAM3 + DA-V2 Small | ORB-SLAM3 + MoGe2-Aerial | MoGe2-Aerial, one frame |
|---|---|---|---|---|
| model | ViT-L ~700M | 25M depth + ORB-SLAM3 | 371M | 371M |
| time | 2 s/frame, 426 s full flight | track 16 ms/frame, depth 138 ms/kf, 19 s full flight | depth 2.4 s/kf | 2.7 s |
| coverage | 78% | 86% (full) | 81% (clip30) | 77% |
| ground warp p95-p5 | 0.086 alt | 0.071 alt (full) | 0.107 alt (clip30; DA-S on clip30: 0.101) | n/a |
| keyframe height spread | n/a | 0.8% alt (full) | 1.2% alt | n/a |
| main building | merged blobs 8-9 m | 9.4-10.7 m | 11.5 m | 11.6 m, metric without SLAM |

* ORB-SLAM3: clip30 initialises at frame 19, 33 keyframes, 4278 points; full flight initialises at frame 125,
  55 keyframes, 5937 points; no lost frames, no resets. It is multi-threaded and not deterministic: a rerun
  initialised at frames 12 / 128 and gave warp 0.074 alt and a 9.5 m main building, so compare within that noise.
* Scale-only fit (`--fit affine`) leaves a far-field dome (depth error 3.9%); `affine_uv` brings it to 1.3-1.6%.
* MoGe2-Aerial metric scale across keyframes: CV 9%; implied altitude 72.7 m (single frame: 68 m).
* Link budget: the full-area DEM at 1 m with 0.5 m height steps (lossless WebP) plus a q30 WebP ortho is ~9 KB
  (the voxel map stream used 38.5 KB for the same scene).

Known gaps: ortho colour is a median over keyframes (soft); residual tilt near the image corner with few map points;
no IMU so VIO untested; Orin timing not measured (only M4 MPS so far); RS3DAda (nadir-only nDSM) not tried.

## Depth Anything 3 and stella_vslam, 2026-10-11

Depth Anything 3 (ByteDance Seed, Nov 2025; there is no DA4) runs in its own venv, `~/src/github.com/ByteDance-Seed/Depth-Anything-3`:
`uv pip install torch==2.14.1 torchvision "numpy<2" scipy matplotlib pillow einops huggingface_hub safetensors omegaconf
imageio opencv-python trimesh evo "moviepy==1.0.3" plyfile pycolmap typer requests addict msgpack` then `uv pip install
--no-deps -e .` (no xformers on macOS; it falls back to plain SwiGLU). pycolmap ships its own libomp, so run with
`KMP_DUPLICATE_LIB_OK=TRUE`. DA3-Small loads as 34M params (the card says 0.08B), Apache 2.0. With `--window N` the keyframe
and the N-1 before it (`causal`, the live case) go in with their ORB-SLAM3 world-to-camera poses and K; DA3 normalises
the poses to median camera distance 1 (`depth.py` skips its 0.1 clamp, which SLAM units hit) and predicts depth at its own
scale whatever the pose scale, so the per-keyframe fit to the map points is still needed.

uav1 clip30, same ORB-SLAM3 run for all rows (depth error = median relative error at the map points after the fit):

| depth | fit | depth error | kf spread | ground warp p95-p5 | main building | M4 per keyframe |
|---|---|---|---|---|---|---|
| DA-V2 Small (518 short side) | affine_uv | 1.58% | 1.18% alt | 0.101 alt | 10.7 m | 139 ms |
| DA3-Small one frame, 504 long side | affine_uv | 2.13% | 1.82% | 0.092 | merged blobs | 91 ms |
| DA3-Small one frame, 952 | affine_uv | 2.08% | 1.78% | 0.104 | merged blobs | 276 ms |
| DA3-Small 8 posed kf causal, 504 | affine_uv | 1.64% | 1.21% | 0.099 | 11.0 m | 364 ms |
| DA3-Small 8 posed kf centred, 504 | affine_uv | 1.65% | 1.32% | 0.099 | 10.9 m | 418 ms |
| DA3-Small 16 posed kf centred, 504 | affine_uv | 1.61% | 1.12% | 0.102 | 11.3 m | 1.1 s |
| DA3-Small 8 posed kf causal, 952 | affine_uv | **1.45%** | **1.10%** | 0.098 | 10.6 m | 2.2 s |
| DA3-Small 8 posed kf causal, 952 | scale only | 4.22% | 2.48% | | | |
| DA3-Small 8 posed kf causal, 952 | none (Umeyama pose scale) | 6.88% | 6.60% | | | |
| MoGe2-Aerial | affine_uv | 1.45% | 1.18% | 0.107 | 11.5 m | 2.4 s |

* DA3-Small as a one-frame drop-in is worse than DA-V2 Small; it rounds objects into blobs and puts low swells in the
  open field. Posed windows bring it level at 504 and slightly ahead at 952, at 16x the depth time. It still needs the
  affine+uv fit: scale alone leaves 4%, and the scale from DA3's own pose alignment is 7% off on 8 close keyframes.
* DA3's camera head puts uav1 at f 571 px (4% off) with 6 views, 714 px from one view; MoGe2-Aerial gives 549 px.

stella_vslam (`stella/build_mac.sh`; e445b54, g2o 20230223_git, OpenCV/Eigen from `~/.local/orbslam3`), same camera and ORB
settings, real-time pacing: clip30 initialises at frame 19 (ORB-SLAM3: 19), tracks 15 ms/frame, 11 keyframes and 1604
points (ORB-SLAM3: 33 / 4278); full flight initialises at frame 132 (125), 13 ms/frame, no lost frames, 25 keyframes and
3116 points (55 / 5937). After a Sim(3) fit its trajectory is within 0.16% of the path of ORB-SLAM3's (full; 0.5% clip30).
With DA-V2 Small on clip30: depth error 1.48%, warp 0.096, but the far field streaks where few keyframes see it and the
main building merges with its neighbour (fewer keyframes, fewer map points). No IMU support (OpenVSLAM never had VI).

### houses_flythrough.mp4

Oblique forward flight over a hilly suburb, horizon and kilometres of far field in view (1900x1066, 30 fps, 23.6 s ->
236 frames at 960x538). ORB-SLAM3: initialises at frame 5, 231 frames tracked, 38 keyframes, 4302 points. stella: frame 7,
230, 21, 2967. MoGe2-Aerial puts the camera 46 m above the ground plane (scale CV 4%). Flat-ground warp and object
metrics do not apply to a hilly, wooded scene; `cmp_houses_near.png` keeps pixels within 1.5x the median map-point depth
(`--max-depth-x 1.5`), `cmp_houses.png` the default 4x, where everything beyond ~3x smears radially.

| depth (ORB-SLAM3 unless noted) | depth error | kf spread, 4x | kf spread, 1.5x |
|---|---|---|---|
| DA-V2 Small | 4.05% | 6.6% alt | 1.2% alt |
| DA3-Small one frame, 952 | 4.69% | 4.1% | 1.6% |
| DA3-Small 8 posed kf, 952 | 4.14% | 12.5% | 1.3% |
| MoGe2-Aerial | 3.79% | 6.9% | 2.7% |
| stella + DA-V2 Small | 3.26% | 3.1% | 0.7% |

How far DA3 depth holds (`depth_video.py`, `runs/dem/houses/da3_depth.mp4`): every frame, one view, scaled per frame to the
ORB-SLAM3 map points in view and to metres with MoGe2-Aerial's metres per SLAM unit; predicted / SLAM depth over all
frames (median, p10-p90):

| SLAM depth | DA3-Small | DA3Metric-Large |
|---|---|---|
| 40-60 m | 1.09 (0.97-1.35) | 1.33 |
| 60-150 m | 1.04-1.05 (0.93-1.15) | 1.07-1.22 |
| 150-230 m | 0.97 (0.85-1.12) | 0.89 |
| 230-355 m | 0.82 (0.72-0.96) | 0.66 |
| 355-550 m | 0.71 | 0.46 |
| 550-860 m | 0.63 | 0.35 |
| 860-1340 m | 0.54 | 0.25 |

DA3-Small is within ~5% from 60 m out to ~230 m (5x the 46 m altitude) and compresses beyond (a 1 km point comes out
at ~540 m); DA3Metric-Large is compressed over the whole range (1.33 at 50 m down to 0.25 at 1 km), and its own metric
scale (DA3 README: focal * output / 300) is 64x short (54-75x): it reads aerial views as a ~2 m miniature. The map
points are projected with each frame's pose, so ones hidden behind roofs and trees widen the spread a little.
DA3-Small 223 ms, DA3Metric-Large 1.05 s per frame at 952 on the M4.
On uav1 clip30 (`runs/dem/da3_depth_uav1_clip30.mp4`, map points at 47-230 m) DA3-Small stays within ~5% (1.04, 1.01,
0.95 per bin) and DA3Metric-Large is 19x short (16-24x), so its metric error is not even constant across scenes.
