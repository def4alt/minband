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
| `make_frames.sh` | video -> `runs/dem/frames_{clip30,full}` (960x540 JPEG, 10 fps, TUM `rgb.txt`) |
| `orbslam3/minband_export.patch` | applies to UZ-SLAMLab/ORB_SLAM3 @ 4452a3c: `System::ExportMonoMap` (map points, keyframe observations), headless `mono_seq` runner, `run_seq.sh`, clang/gcc-portable fixes (`stdint.h`, `int mnFullBAIdx`) |
| `run_pipeline.sh` | ORB-SLAM3 on both sequences -> `fuse.py` -> optional MoGe2-Aerial / MASt3R reference -> `warp.py`, `objects.py`, `compose.py` |
| `depth.py` | Depth Anything V2 (small/base, relative) and MoGe2-Aerial (metric) wrappers; CUDA > MPS > CPU |
| `fuse.py` | the fusion above; `--fit affine` or `affine_uv`, `--model small/base/moge2aerial` |
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
