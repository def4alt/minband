# Real drone footage

MinBand on footage from a real drone instead of the phone: detect and track the objects the way a
drone edge would, then feed the tracks to the same WASM edge, eval and live server as a phone log.

Footage: [MEVA](https://mevadata.org) UAV drop 1 (Kitware / IARPA, **CC-BY-4.0**): 45 clips from
two DJI Inspire 1 v2 drones with Zenmuse X3 cameras, 3840x2160 at 30 fps, over the Muscatatuck
Urban Training Center (school, bus station, parking), re-encoded by the publisher at CRF 26. No
annotations ship with the UAV clips, so the tracks come from a detector and tracker, as on a real
edge.

## Pipeline

```bash
python3 -m venv .venv && .venv/bin/pip install onnxruntime opencv-python-headless numpy scipy
.venv/bin/python meva.py fetch 2018-03-13.16-00-14 clips/          # 270 MB by range request (meva.py index lists all)
curl -sSLO https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n.onnx   # or a VisDrone-trained ONNX
.venv/bin/python track.py detect clips/2018-03-13.16-00-14.16-03-38.uav1.mp4 --model yolo11n.onnx \
    --start 15 --end 105 --out ../../runs/footage/meva-uav-2018-03-13.16-00-14   # slow: ~3 s per 4K frame on 4 cores
.venv/bin/python track.py track ../../runs/footage/meva-uav-2018-03-13.16-00-14    # seconds: ground fit, tracker, tracks.csv
.venv/bin/python track.py preview ../../runs/footage/meva-uav-2018-03-13.16-00-14 clips/<clip>.mp4
./h264.sh clips/<clip>.mp4 15 105 ../../runs/footage/meva-uav-2018-03-13.16-00-14   # x264 on the same pixels
cd ../eval && npm run eval -- --gt ../../runs/footage/<run>/tracks.csv \
    --baseline-a ../../runs/footage/<run>/baseline_a.json --out ../../runs/footage/<run>/eval
cd ../../server && TRACKS=../runs/footage/<run>/tracks.csv TRACKS_CAMERA=<summary.json camera_m> npm run sim
```

- `detect.py`: a YOLO ONNX model on overlapping tiles at a fixed 1.5x magnification into the
  network input at any resolution (people are 15-35 px tall in 4K from 60 m; a frame smaller than a
  tile is padded, not stretched), class-wise NMS; classes are mapped to MinBand ids by the names the
  export carries (COCO, VisDrone, military-vehicle models). The NMS used to be fed x1 y1 x2 y2 where
  OpenCV takes x, y, w, h, which suppressed same-class neighbours (people in a group): fixed, and the
  pre-NMS rows are cached too (`detections-raw.npy`).
- `track.py detect`: about 5 Hz whatever the frame rate (every 6th frame at 30 fps, 5th at 25, 2nd
  at 7.5; the phone's detector runs at ~12 Hz, its tracker at 30 Hz), the detector plus a homography
  onto the first frame (ORB/RANSAC to the reference, an optical-flow chain with hysteresis when the
  view has left it or the texture is thermal), so the drone's own drift and yaw do not become object
  motion. A lost registration ends the clip there instead of crashing.
- `track.py mti`: class-agnostic moving-target indication on the same frames (below).
- `track.py track`: a flat-ground pinhole camera with the focal length from the field of view
  (Zenmuse X3 16:9 video: ~85 deg horizontal), its pitch and height fitted from people's box widths
  (1/range is linear in the image row), detections at the bottom centre of their boxes (feet,
  tyres), fusion of the caches named in `--sources`, a constant-velocity Kalman tracker in metres,
  the state written at every frame. `summary.json` reports the fit and two checks: walkers' median
  speed (~1.3-1.4 m/s expected) and the extent of the scene.
- `h264.sh`: libx264 veryfast, zerolatency, 2 s keyframes at the clip's own frame rate, no
  B-frames, CRF 23 and 28, at native and at 720p, 480p and 360p below it; `baseline_a.json` (CRF 23)
  replaces the configured H.264 rows in `tools/eval` and the server (`MINBAND_BASELINE_A`).
- `audit.py`: label-free metrics and the visual audit protocol (below).

Limits, said plainly: the metric scale rests on an assumed person width (0.55 m) and a flat
ground; the detector is a nano model, so dense groups are under-counted and tracks fragment; the
source was already re-encoded at CRF 26, so x264 on it is a lower bound for camera-original video.

## Battlefield footage

The same pipeline on drone footage of military vehicles and people, measured without labels on
clips it was never tuned on. Results, per clip and before/after: `docs/FOOTAGE_FINDINGS.md`.

```bash
.venv/bin/python track.py detect  clip.mp4 --model aerial-guardian.onnx --out DIR   # VisDrone-trained YOLO26n-P2, tile auto
.venv/bin/python track.py mti     clip.mp4 --out DIR                               # motion: detections-mti.npy, overlay-mask.png
.venv/bin/python track.py track   DIR --sources det,mti                            # fusion + tracker: tracks.csv, summary.json, detlog.npy
.venv/bin/python track.py track   DIR --sources det --legacy-tracker --no-overlay --out DIR/det-legacy   # the original tracker
.venv/bin/python audit.py metrics DIR DIR/det-legacy                               # label-free metrics: audit-metrics.json
.venv/bin/python audit.py sample  DIR clip.mp4 --old DIR/old --out DIR/audit       # 12 fixed-seed frames, raw + annotated
.venv/bin/python audit.py score   DIR/audit                                        # P / R with Wilson intervals from DIR/audit/labels.json
./run_clip.sh clip.mp4 DIR          # all of the above for one clip, plus the old pipeline (olddet.py), x264, tools/eval, replays (minband.sh)
.venv/bin/python tables.py label_free; .venv/bin/python pooled.py              # the tables of docs/FOOTAGE_FINDINGS.md
```

`olddet.py` runs the old `detect.py` (read from git at 6f21d55) for the before/after rows; the full
from-scratch sequence (venv, model, clip URLs, runs, audit, tables) is in docs/FOOTAGE_FINDINGS.md.

### Moving-target indication (`mti.py`)

Camouflage, decoys and vehicles no training set has defeat an appearance model's class labels;
motion does not. For each detection frame the frames 0.3 s before and after are aligned onto it by a
homography from sparse optical flow (Shi-Tomasi + pyramidal LK, forward-backward checked, RANSAC at
1 px) at a working width of ~1920 px, matched in gain and offset (auto-exposure, light), and
differenced against the 3x3 min/max of the warped frame (up to 1 px of misregistration is free).
The differences become z-scores against a per-block (32 px) robust noise estimate, so compression
noise, texture and vegetation raise their own threshold. A pixel is a seed if it differs from both
the past and the future (z > 5: no ghosts at the old or new position, no single-frame flicker); the
blob grows into the union of both differences (z > 3), which is symmetric about the object at the
current frame. Morphology (open 2x2, close 5x5), connected components, a 6 px minimum.

Static overlays (watermarks, HUD text, channel logos) stay put in the image while the scene moves
under them, so they difference: `overlay_mask()` finds them from the clip itself. The mean of 24
frames in image coordinates keeps an overlay sharp while the scene blurs; the scene-coordinate mean
warped back does the opposite; only where the ground moved > 60 px over the clip, and only structure
present in >= 80 % of the frames, so a target the camera follows is not masked. No hand-drawn masks.
The mask is not differenced, and appearance boxes mostly inside it are dropped.

On the ground (`track.py track`): adjacent blobs are merged (a vehicle and its own shadow, a body
split in two), then filtered by size (0.3-25 m), aspect (<= 6) and persistence with net motion:
greedy constant-velocity tracklets across consecutive detection frames (gate 1.5 m + motion); a
blob passes once its tracklet has >= 3 hits and moved >= max(0.5 m, 0.3 m/s x its duration) in the
last 2 s. Parallax from tall structures under a hovering or drifting camera differences but does not
go anywhere, and fails this test. **Residual false movers**: the far end of a long low-sun shadow (a
separate blob metres behind its vehicle; motion-only tracks in lockstep with another track within
8 m are tracked but not reported, which removes only some of them), surf, and the parallax of tall
things under a camera that translates fast (they appear to travel over the ground at a fraction of
its speed).

### Fusion and classes

Per detection frame: appearance boxes from different models merge when they overlap; an MTI blob
that overlaps an appearance box (grown by half its size) or lies within 1.5 m (dismount) / 4 m
(vehicle) of its foot point is that object, and the appearance class wins. The rest become
**unclassified movers, class 100**. MinBand's class ids from 100 up are its own (core/src/classes.rs):
100 mover (keeps speed, on the ground, <= 25 m/s), 101 armoured vehicle (vehicle prior, emitted only
by a military model, which the frozen pipeline does not use); soldiers stay 0 (dismount), military
trucks 7. CoT stays `a-u-G` for all of them, with the class in the callsign and remarks.

The tracker keeps dismounts, vehicles and movers as class groups; a mover track is promoted when an
appearance detection associates with it, and a classified track is kept alive by motion detections.
Tracker changes against the original, each switchable (`--legacy-tracker` restores all of them):
coasting 2 s with a gate that grows with the time since the last detection; re-acquisition of a lost
track (same id) for 3 s instead of a new birth; tracks reported from 1 s of age, so short spurious
tracks never reach the link; a static mode (measured motion over 3 s below 0.3 m/s for a dismount,
0.5 m/s for a vehicle) that averages a parked object's position and reports zero velocity until a
large innovation; motion-only companions not reported.

A specialist appearance model (e.g. a military one, `detect --tag mil`) can be fused with
`--sources det,mil,mti`: its boxes are only tracked where motion or another model corroborates them,
and its class only replaces the group's at confidence >= 0.5 (`--confirm-sources`, `--label-conf`).

### Ground scale without a known camera

People's box widths (0.55 m) when there are >= 20 confident person boxes; otherwise vehicles' box
widths (3 m at any heading: a car seen side-on is ~4.5 m, end-on ~1.8 m, so +-50 %); otherwise an
assumed pitch and height (45 deg, 100 m: the scale is then a guess). When the rows do not constrain
the pitch, it is assumed (45 deg) and only the height is fitted. The field of view is assumed (85
deg) for every clip. `summary.json` gives the method, the number of boxes and a bootstrap 90 %
interval of the scale at the image centre; the bootstrap covers box noise only, not the width
assumption. The walkers' median speed is the independent check where there are walkers.

### Evaluation protocol (held-out clips)

The clips in the battlefield manifest are split `dev` / `heldout` before any detector work.
Parameters are tuned on MEVA and the dev clips only, then frozen in a commit; each held-out clip is
run once with the frozen pipeline and reported as it comes out. A bug found afterwards is fixed and
both runs are reported.

`audit.py metrics` (label-free, identical on every clip): per detector source, detections per frame,
agreement with the other sources, and the fraction of its detections that end in a confirmed track
lasting >= 1 s (a persistence proxy for precision); track count and length distribution, births per
minute (a fragmentation proxy), mean entities per frame, and for objects that do not move (net
displacement < 1 m over > 5 s) the position std and KF speed, split into a global part (the whole
scene moving together: registration) and a per-object part.

`audit.py sample` / `score`: a **post-hoc visual audit by the developer, after freezing, for
evaluation only, not ground truth**. 12 frames per clip drawn with a fixed seed (0) from the
detection schedule; each frame rendered raw and annotated at legible scale (tiles of ~1280 px,
low-res clips upscaled), with every box the tracker saw in the improved pipeline (cyan dismount,
yellow vehicle, magenta motion-only; thick when the detection ended in a confirmed track) and the
old pipeline's own boxes (green) where they differ. The developer records per frame the boxes on a
visible object (TP), on nothing or on an object that already has a box (FP, duplicate), and the
visible objects without a box (FN); `score` gives precision and recall with Wilson 95 % intervals
for the old pipeline (VisDrone only, original detector and tracker) and the improved one, at the
detection and at the track level. Frames of clips without a licence for reuse (stock and channel
footage) stay in `runs/`, which is not committed, and never go into docs.

### Military appearance models

Tried: `yolo8n_military.onnx` from github.com/MErenKaya/Military-Vision-Enhancement-YOLO (12 classes
incl. military_tank, military_vehicle, soldier, civilian_vehicle; no licence file in the repo,
Ultralytics AGPL-3.0 in its metadata). On the dev convoy clip it finds armoured vehicles VisDrone
misses and labels them armoured; on the civilian MEVA campus it labels cars, a trailer and portable
toilets armoured at about the same rate (FOOTAGE_FINDINGS.md), and motion already finds the moving
vehicles. It is not used. Not tried: the AMAD models (github.com/InvictusRex, .pt only, no licence),
because the held-out stock clips are that repository's own test videos, and
KasSahin/tank-datection-yolov8n (MIT), which ships no weights. Weights are never committed.
