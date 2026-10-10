# recon3d: the drone positions objects in 3D, the operator sees them in a simulation

Drone video -> MASt3R-SLAM reconstruction (calibrated: one focal length) -> YOLO26 detection (VisDrone
YOLO26x, tiled, with yolo26x-seg masks) -> every detection placed *in the reconstruction* -> 3D tracks with a
height, a heading and a tested motion state -> MinBand contact reports (real WASM edge, shaped link, WASM
receiver) -> the operator's 3D simulation: a flat synthetic ground, each received contact standing at the
position and height that crossed the link, as a solid of its measured size wearing the drone's own view of it,
seen from the drone's received pose.

**The map is not sent.** The reconstruction stays on the drone and is used only to place the objects (DESIGN.md
§5, the 3D handoff, moved onto the edge): a contact's ray meets the reconstructed terrain there, and the contact
goes out with its height (`dz`, ext bit1, in `pos_res` units: 10 cm at `pos_res` 1), its ray at the object, its
heading when it stands still (the course byte with speed 0, PROTOCOL.md 3.4), and the camera `Pose` stream in the
video regime. The earlier map-over-3d-map-stream work is parked on the local branch `wip/recon3d-map-stream`.

## Run

```bash
# one sequence end to end (~5-15 min on the RTX 5090: SLAM ~3 fps, YOLO ~2 fps at 1344 px with masks)
tools/recon3d/run.sh ../datasets/visdrone-mot/VisDrone2019-MOT-val/sequences/uav0000182_00000_v runs/recon3d/vd182
tools/recon3d/run.sh ../datasets/visdrone-mot/VisDrone2019-MOT-train/sequences/uav0000076_00720_v runs/recon3d/vd076

# the page (Node 22: `mise exec node@22 --` if the system node is older); every runs/recon3d/<run> is a scene
cd tools/recon3d && npm install && npm start            # http://localhost:8092
SCENE=vd076 PROFILE=lora npm start                      # start on another scene or link; the page switches scenes too
node scripts/screenshot.mjs out.png --at 6 --view chase # headless Chrome (Vulkan GPU; SOFTGL=1 without one)

# a calibrated run (any footage from one lens: FOCAL in source px; estimate it from an uncalibrated run, below)
FOCAL=1401 tools/recon3d/run.sh ../datasets/youtube/labrador_0240-0320.mp4 runs/recon3d/labrador-cal

# the operator's drone view, frame-exact (spawns the driver with CLOCK=manual), and a side-by-side with the footage
node scripts/record.mjs --out pov.mp4 --from 0 --to 40 --view drone --run-dir runs/recon3d/labrador-cal \
  --sbs ../../runs/recon3d/labrador-cal/clip-720p.mp4 --sbs-out pov_sbs.mp4

# replay through edge, link and receiver as fast as possible and score it against the eye audit (below)
tools/recon3d/eval/run_eval.sh runs/recon3d/labrador-cal mytag            # PROFILE=lora etc. pass through
```

Driver env: `POS_RES` (default 1 = 10 cm; 2 = 1 m, the old default), `LINK_M` (2: two vehicle tracks closer than a
car's width plus clearance are one object; 3 grouped the neighbours of a parked row), `CE_LINK` (0: the grouping
distance is `max(link_m, ce_link x ce)`; lift.py merges its own duplicates, so error radii do not group parked rows),
`T_MOVING_S` / `T_STOPPING_S` (0.75 / 1: the edge's waits before it calls a track moving / stopped; lift.py's own motion
test already decided, the core's defaults 2 / 5 s stay the core's), `REACQUIRE` (0: a new track within this many error
radii of a lost contact of its class takes that contact over) and `U_TOL` (0: a height change in metres that makes a
revision) — both measured below and left off,
`SEED` (the link's loss is a seeded PRNG), `CLOCK=manual` (steps on `{cmd:'step'}`), `DUMP=<file.jsonl>` (batch
replay, one state per 0.1 s step, then exit). Page: `?view=drone`, `?rec=1` (no video inset, for recording),
`?rings=0`, `?ground=flat` (the old u = 0 plane), `?snap=0` (no settling on the ground outside the drone view).
`lift.py --cache` keeps the lifted and ranged detections in the run dir (`lift_stage1.pkl`, `lift_ranging.pkl`) for
iterating on tracking: ~1.6 instead of ~4.5 min; delete them after changing lifting, terrain or scale. Without the CUDA
env (a Mac): a venv with numpy, scipy and opencv for lift.py and the scorer (`PY=<venv>/bin/python` for `run_eval.sh`);
`record.mjs` with `CHROME=<Playwright's Chromium> SOFTGL=1`.

YOLO26 needs ultralytics >= 8.4: `tools/recon3d/.venv` is a venv over the CUDA env with `ultralytics==8.4.175`
(`python -m venv --system-site-packages`); run it with `YOLO_CONFIG_DIR=tools/recon3d/.venv/ultralytics-config` so
it does not rewrite the system ultralytics' settings. Weights in `models/`: `visdrone-yolo26x.pt`
(huggingface.co/dronefreak/visdrone-yolo26x, `best.pt`; AGPL-3.0 + VisDrone non-commercial: research/hackathon
only) and `yolo26x-seg.pt` (Ultralytics). Pickles were scanned before loading (torch, ultralytics, collections only).

`run.sh` needs the MASt3R-SLAM checkout (`SLAM_DIR`, default `~/MASt3R-SLAM`) with its conda env
(`SLAM_ENV`, default `mast3r-slam`, which sets `TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD`) and an
ultralytics env with CUDA and `lap` (`YOLO_ENV`, default `YOLO-CUDA-13`). The core's Node WASM
package must be built (`core/README.md`). Outputs go to `runs/` (gitignored); weights to
`tools/recon3d/models/` (`*.pt` is gitignored).

## Stages

| stage | file | env | what |
|---|---|---|---|
| 1 | `slam_export.py` | mast3r-slam | MASt3R-SLAM's loop with a per-frame export: every frame's pose (re-anchored on its keyframe's *final* optimised pose) and its pixel-aligned pointmap; keyframes for the map |
| 2 | `detect.py` | YOLO | YOLO11x-seg + ByteTrack, road users only, class-agnostic NMS, mask outlines |
| 3 | `lift.py` | YOLO (numpy, scipy, cv2) | lift, metric frame, terrain, causal tracks, chips, static map (below) |
| 4 | `src/driver.ts`, `web/` | Node 22 | real WASM edge -> shaped link -> WASM receiver; the 3D page |

`lift.py` in order:

1. **Lift.** A detection's mask selects its pixels in the frame's pointmap; the confident ones at the
   object's depth give the object's *direction* from the camera.
2. **Metric frame.** Ground plane by RANSAC on the static map (normals near the cameras' up), north =
   first camera heading, origin on the ground below it. Monocular SLAM has no scale: the median
   ground-plane length of the car surfaces is set to 4.5 m (`--car-length`), the same kind of stated
   assumption as `tools/footage`'s person width.
3. **Terrain ranging.** The ray meets a height field of the static map (lowest surface per 0.5 m
   cell) at the class's centre height above it. One frame's depth jitters by metres along the ray at
   a 15 deg look-down angle (first version: pedestrians "walking" at 5 m/s, cars streaking along the
   line of sight); the fused multi-view terrain does not.
4. **Tracks.** ByteTrack ids associate; a causal constant-velocity Kalman filter on the ground per
   object (gated); height = terrain + class centre height. Re-identification in 3D joins ByteTrack id
   switches; two tracks within one object's size for 5 frames merge; a person box mostly inside a
   two-wheeler box is its rider; a "dismount" sustaining > 3.5 m/s is relabelled a rider. Each row
   carries the tracker's own error radius in the reconstruction's frame (filter spread + ranging
   error), not a GNSS model.
5. **Chips.** Per object, from its best view in its first 2 s: length and width measured in the
   reconstruction (clamped to class priors), the masked crop (WebP with alpha, <= 128 px), and the
   3x4 projection from the object's frame into that crop (SLAM pose, metric frame, that frame's own
   focal length). The page projects the crop onto the faces the drone saw.
6. **Static map.** Keyframe pointmaps fused in the metric frame with every detected object masked out
   (moving cars leave no smears), 0.15 m voxels.

## What crosses the link, and what does not yet

| | status |
|---|---|
| contacts with height (`dz`), rays at the object, `Ego`, camera `Pose` | real: core v2 frames through the shaped link, decoded by the WASM receiver |
| chips | **simulated channel**: real compressed sizes (median ~0.6 kB), at most half the link (the spec's chip cap), 400 kbit/s on the video link; the core does not emit `ChipHead`/`ChipSym` yet |
| heading of a still object | real: the course byte with speed 0 (PROTOCOL.md 3.4); lift.py measures it only where the view looks down >= 20 deg (a grazing view's footprint axis is the line of sight), so on the Labrador clip mostly near cars and movers |
| static map | **not sent**: it stays on the drone, for placing objects; the operator's ground is a simulated height field from the contacts' base heights (below) |
| clock | the SLAM run's frame clock (30 fps); no GNSS: an arbitrary origin |

## Scenes and numbers

| | `vd182`: busy city street | `vd076`: quiet village street |
|---|---|---|
| source | VisDrone2019-MOT val `uav0000182_00000_v`, 12 s, 1344x756 | VisDrone2019-MOT train `uav0000076_00720_v`, 12 s, 1904x1070 |
| SLAM | 363 of 363 frames tracked, 10 keyframes | 361 of 361 tracked, 6 keyframes |
| scale (car length 4.5 m) | 13.3 m per SLAM unit, 3 067 car sightings | 7.4 m/unit, 2 002 sightings |
| camera | 19 m up, 15 deg look-down, 72 m path | 29 m up, 38 deg look-down, 56 m path |
| detections ranged on the terrain | 13 150 (4 148 from the direction alone) | 6 971 of 6 980 |
| tracks reported / with chips | 203 / 203 | 99 / 99 |
| speed median / p90 | car 0.65 / 4.7 m/s, dismount 1.18 / 6.3 | car 0.54 / 9.5, dismount 0.79 / 3.0 |
| error radius (reconstruction frame) | median ~0.5 m | |
| contact stream, video regime | ~2-3.6 kB/s for 60-80 live contacts | ~1.7 kB/s for ~30 |
| contact stream, lora (2 kbit/s) | ~270 B/s, detail level 2 (groups) | |
| chip on the wire, median (p90) | 546 B (1.1 kB) | 1.1 kB (2.3 kB) |
| map | 155 k points, 144 x 162 x 30 m | 143 k points, 104 x 132 x 18 m |

Tried and dropped: VisDrone `uav0000263_03289_v` (a highway interchange, quiet): SLAM drifted in
scale between keyframes over featureless asphalt (the road came out as stacked sheets), and its cars
on the overpass need a multi-level terrain (below). Its run is in `runs/recon3d-rejected/`.

## Labrador City clip: measured against an eye audit

YouTube `Gr72NzOci-0`, 2:40-3:20 (40 s, 1080p, 30 fps): a drone ~40 m up flies slowly over houses towards a long red
mall with a packed parking lot 150-400 m out, at 15 deg look-down; ~100 road users per frame, nearly all parked cars,
37 % of them under 20 px; 16 movers. Run dirs `runs/recon3d/labrador` (uncalibrated, old detector) and
`labrador-cal` (final).

**Ground truth** (`eval/gt-labrador.json`, `eval/movers-labrador.json`): 8 frames (every 5 s) with every road user
boxed: a union of five detector configurations at a 0.08 floor proposed candidates, readers judged every candidate on
crops and scanned every 2x tile of the frame for objects no candidate covered (50 added, mostly cars half hidden behind
cars); 800 objects, 175 marked hard (tiny, occluded or uncertain). Movers: every object that clearly moves in the 40 s,
found by motion-compensated frame differences (SIFT homography) and detector boxes shifting against the ground, each
verified on warped time strips; 16 vehicles, 324 sightings every 0.5 s. Caveat: VisDrone YOLO26x was one of the
candidate sources, so an object it and the other four all missed is in only if a reader found it.

**Score** (`eval/score_e2e.py`): at every inventory frame and mover sighting the edge's tracks (through the edge's
camera) and the receiver's contacts (through the pose the receiver decoded: what the operator's drone view draws)
are projected into the frame; a point matches a box it falls in (grown by max(6 px, 30 %)), one to one. "single" =
matched by an individual contact, in brackets also by a group's ring; "unmatched" = contacts in view on no inventory
object at the 8 frames (false, stale or misplaced); "parked moving" = receiver contacts on parked cars whose motion
state is moving (the operator would see them drift); clean link (video regime).

| pipeline | edge | operator: all | not hard | mover sightings | < 20 px | position error (median) | unmatched | parked moving |
|---|---|---|---|---|---|---|---|---|
| yolo11x-seg 1344, uncalibrated SLAM, old protocol (pos_res 1 m, ce_link 2) | 17 % | 17 % (23 %) | 22 % (28 %) | 42 % (45 %) | 2 % | 16.3 px / 3.30 m | 143 | 51/125 |
| same, new protocol (pos_res 10 cm, ce_link 0) | 17 % | 22 % (23 %) | 27 % (28 %) | 44 % (45 %) | 2 % | 15.8 px / 3.26 m | 158 | 64/160 |
| + calibrated SLAM (`--focal 1401`) | 17 % | 22 % (25 %) | 27 % (30 %) | 46 % (46 %) | 3 % | 4.7 px / 0.85 m | 123 | 56/163 |
| + YOLO26x max, far terrain tier, nothing deleted, ray-aligned noise | 62 % | 68 % (83 %) | 71 % (85 %) | 89 % (96 %) | 74 % | 6.2 px / 1.44 m | 760 | 188/513 |
| + image-space re-identification | 58 % | 66 % (80 %) | 70 % (83 %) | 87 % (94 %) | 69 % | 6.8 px / 1.53 m | 679 | 186/499 |
| + motion test and static estimate | 56 % | 64 % (79 %) | 67 % (83 %) | 86 % (94 %) | 64 % | 6.4 px / 1.39 m | 656 | 30/484 |
| same, re-run on an M4 Mac (tag `mac_base`: identical) | 56 % | 63.5 % (78.9 %) | 67.2 % (82.6 %) | 85.8 % (93.8 %) | 53.3 % (64.0 %) | 6.4 px / 1.39 m | 656 | 30/484 |
| + false-dismount filter | 56 % | 62.6 % (78.0 %) | 66.2 % (81.8 %) | 85.8 % (93.8 %) | 51.7 % (62.0 %) | 6.3 px / 1.38 m | 560 | 30/477 |
| + re-identification at the static estimate, tentative tracks (`m1_all`) | 55 % | 64.8 % (72.5 %) | 69.3 % (77.0 %) | 86.4 % (87.7 %) | 49.6 % (54.1 %) | 6.7 px / 1.41 m | 477 | 35/494 |
| + 3D static estimate (`m2_3d`) | 65 % | 81.2 % (87.0 %) | 85.0 % (90.4 %) | 85.2 % (89.5 %) | 72.7 % (77.7 %) | 4.8 px / 1.02 m | 391 | 44/626 |
| + tracks fused only with image overlap, image-space motion test, mover heights from their ray, edge waits 0.75 s / 1 s (`m3_merged`) | 87.5 % | 79.0 % (97.0 %) | 83.0 % (98.6 %) | 88.6 % (98.1 %) | 70.2 % (92.6 %) | 4.7 px / 0.93 m | 324 | 23/604 |
| + edge grouping distance 3 -> 2 m (tag `final`) | 87.5 % | 85.9 % (96.2 %) | 89.8 % (98.6 %) | 91.0 % (97.5 %) | 77.7 % (90.1 %) | 4.6 px / 0.90 m | 393 | 22/657 (3 %) |
| + near false vehicles held back, 15 sightings before a report (**final**, tag `final2`) | 87.2 % | **88.6 % (96.4 %)** | 92.2 % (98.6 %) | 89.2 % (96.0 %) | 78.9 % (90.5 %) | 4.6 px / 0.90 m | 370 | 24/679 (4 %) |

From the Mac rows on, the "< 20 px" column has the with-rings figure in brackets too, and the scorer's projections are
unchanged. Movers shown moving (`motion_check.py`): 36 % (`mac_base`) -> 41 % -> 40 % -> 61 % -> 65 % -> **66 %** (209/319) final.
`final` is a fresh, uncached lift.py run (the cached runs gave the identical scene).

What each step was:

- **Protocol.** `pos_res` 1 m -> 10 cm (dx, dy and now dz; the edge's revision band is `max(ce, 2 pos_res)`, so 1 m
  let a car drift 2 m at the operator) and `ce_link` 2 -> 0 (two error radii grouped parked rows 2.7 m apart into
  rings: half the far cars had ce > 1.5 m). ~10 % more bytes on the video link. Heading of still objects on the course
  byte. Core tests 49/49.
- **Calibrated SLAM.** Uncalibrated, MASt3R's implied focal length wandered 1351-1504 px frame to frame, so a 3D point
  re-projected with one camera (the operator's drone view) moved radially by up to tens of pixels. `slam_export.py
  --focal F` runs MASt3R-SLAM calibrated (constant K in tracking and the backend, pointmaps constrained to its rays);
  F = 1401 px, the median of the uncalibrated run's per-frame estimates. Position error 3.3 m -> 0.85 m.
- **Detector.** The old yolo11x-seg at 1344 px found 31 % of the inventory (none under 20 px). VisDrone YOLO26x, full
  frame 1280 + 640 px tiles at 1.5x and 2x, merged, ByteTrack without `fuse_score` (it silently dropped every box
  under ~0.25): 91 % tracked (`detect.py` defaults; benchmark in `labrador-det/`, 0.47 s/frame with the seg pass).
- **Lifting.** 62 % of the new detections were deleted: MASt3R's confidence is at its floor at the top of an oblique
  frame (the lot, 300-600 m out), the confident map has no ground there, and a low-confidence detection without a
  terrain hit was dropped. Now a far terrain tier (every keyframe point, 4 m cells; their depth is within ~20 %, judged
  by the cars' apparent size per metre of range) ranges them, the map cuts out each object's own pixels instead of its
  dilated box (which had removed the ground between parked cars), and what still misses keeps its own depth with a
  large ce. The tracker's measurement noise is ray-aligned (pixel error across the line of sight, height error
  stretched by the grazing angle along it).
- **Re-identification** in the image (a lost track's prediction must land in the new box): fewer stale contacts, a
  little recall lost.
- **Motion test.** The constant-velocity filter turned metres of along-ray noise into ghost speeds: 37 % of parked cars
  reached the operator as moving. A track now moves only when a test says so for 0.5 s; otherwise it reports the
  information-weighted mean of its last 5 s, no velocity. Parked shown moving 37 % -> 6 %, but only 36 % of the movers
  (108/302) were shown moving. What limited them, in order: (1) the 3D line fit (chi2 > 20 over 2 s) is the wrong
  instrument for a far object: a parked car passes it one time in five (the errors of one object over 2 s are correlated,
  not the ~60 independent looks the fit counts), and a threshold that rejects those rejects slow movers; (2) tracks of
  *different* cars were fused whenever their 3D positions came within 1.2 m, and far cars are ranged on 4 m terrain
  cells and share coordinates: 40 tracks held disjoint boxes in the same frame (12.8k times), m10 (45 of the 324 mover
  sightings, a pickup driving through the lot) and m16 lived inside parked cars' tracks and were never tested;
  (3) the core's own waits on top: a mover is `unknown` until its speed has stayed above 0.7 m/s for 2 s (and a parked
  car until it has been calm for 5 s), after lift's 0.5-1.5 s: 121 mover sightings were `unknown`, 56 `static`;
  (4) the edge's 5 s `t_stopping` kept every blip of the test shown as moving for five more seconds.
  Now: tracks fuse only with box overlap (>= 0.3, `SAME_OBJECT_IOU`) and a second 2D id in a frame at another place in
  the image is another object; the main test is in the image (`flow_tests`): the drone's motion moves every pixel, but a
  parked car moves with its still neighbours, so a track moves when its box centre has shifted against the median shift
  of its nearest still neighbours by more than 5 px over 0.5 s or 8 px over 1 s (parked cars: 0.7 px median, 99th
  percentile 6-9 px; movers: median 12-20 px) and by more than the 0.7 m/s that shift is worth at its range, independent of
  the ranging; the 3D fit (chi2 > 100, its chi2 divided by the fit's reduced chi2: a step is no velocity) stays for tracks
  with fewer than 3 still neighbours; a mover's height is its ray's, not the terrain at its lagging filtered position
  (20-77 px of vertical error that lost the match to parked neighbours); the driver gives the core `t_moving` 0.75 s and
  `t_stopping` 1 s (`T_MOVING_S`, `T_STOPPING_S`; the core's defaults stay). Movers shown moving 36 % -> 62 %, parked cars
  shown moving 6 % -> 4 %, mover sightings 86 % -> 92 % (94 % -> 98 % with rings). What is left: movers still `unknown`
  (37) or `static` (67) are mostly the first seconds of a track, creeping starts below the threshold (m1, m14), and
  mover contacts that lose the one-to-one match to a parked neighbour inside the same small box; the parked cars shown
  moving include movers' contacts matched to a parked box next to them.

- **False dismounts.** At the 0.1 floor the VisDrone model calls roof vents, chimneys, AC units and poles "pedestrian":
  the receiver held ~42 dismounts per frame against ~1.5 people in the inventory. A person track is now reported only
  once (causally, from its history) its summed confidence reaches 9 (~1 s of a far person at 0.3), its median box aspect,
  corrected for the look-down angle, is >= 1.6 (a standing adult 1.75 x 0.5 m; the 12 audited people measure 2.1-2.6,
  vents and chimneys 1.0-1.5: the threshold was chosen with them in view) and, where the fine map surrounds it, it does
  not stand > 3 m above the low ground within 15 m (a roof). Unmatched dismounts per frame 20.5 -> 6.6, held 41.8 ->
  12.9 (crops of the survivors: mostly people the audit did not box). The 0.9 point of individual recall it cost is
  mostly false dismounts that had "hit" cars (the scorer does not check class; counting only class-consistent hits the
  cost is 0.4 point; dropping random halves of the false dismounts costs the same on this scorer).
- **Re-identification and tentative tracks.** Of the 656 unmatched, 231 were lost vehicle contacts (median track life
  1.3 s: flickers, not fragments), 261 fresh vehicle contacts (half on empty ground), 164 false dismounts; only 54 of 214
  ended vehicle tracks had a successor within 3 m, all re-identification can join. Re-identification now uses the motion
  test's verdict and predicts a parked track at its static estimate (chi2 <= 25 with 0.5 m of drift, in the box);
  `REID_STATIC_S` 10 -> 30 s (the window barely matters). A track is reported after 0.7 s, or at once at detector
  confidence >= 0.5: flickers never reach the edge. Merging duplicates by static estimate plus box overlap was tried and
  dropped: in the 8 inventory frames duplicates and row neighbours have the same overlap, size ratio and separation.
  The edge-side alternative (`REACQUIRE`: a new track inside a lost contact's error radius takes it over) trades
  recall for unmatched at every gate (1 ce: 63.5 -> 60.0 %, 656 -> 575; on the final scene 85.9 -> 83.0 %, 393 -> 319): far
  cars' error radii span their row neighbours. It stays off. The "with rings" drop of this step is rings around flicker
  clusters that had covered untracked objects.
- **3D static estimate.** With the above, 124 of the 220 inventory objects the operator missed entirely had an edge
  track on the right box that projected off it: by 2 px across the line of sight but by a p90 of 126-278 px
  vertically. A still track reported the horizontal mean of its measurements and the terrain's height *under that
  mean*, and the mean range of a far car lands on another terrain cell (a roof edge, the 4 m far tier). Every
  measurement lies on its own ray at the terrain where that ray meets it, so the estimate is now the 3D mean of the
  measured points with 3D ray-aligned covariances (a ranging error slides a point along its ray, down as well as out),
  height included: vertical error p90 126 -> 15 px at 150-300 m, operator individual recall 65 -> 81 %. Nothing is
  snapped onto the current ray; the estimate is the measurements' own.
- **Motion** (the bullet above): the 3D line fit was the wrong instrument for far objects, and the 3D duplicate rule
  fused *different* far cars (4 m terrain cells), hiding two movers inside parked cars' tracks. Now: tracks fuse only
  with box overlap and a second 2D id elsewhere in the image splits off (107 splits); the main motion test is in the
  image (a box's shift against its still neighbours'); a mover's height is its ray's; the edge waits 0.75 s / 1 s.
  That also gave the edge 87.5 % individual recall (cars no longer lost inside fused tracks), and with the neighbours
  of a parked row now separate tracks, the edge's 3 m grouping distance turned them into rings: 2 m (a car's width
  plus clearance) keeps them individual. The sweep, all reported: 3 m 79.0 % / 324 unmatched, 2.5 m 83.1 % / 354, 2 m
  85.9 % / 393, 1.5 m 88.2 % / 437, 1 m 91.0 % / 533 (duplicates come through as singles).
- **Height revisions** (`U_TOL`): a contact's height reaches the receiver only on a revision made for another reason.
  Revising on a height change of 0.5-2 m cost a point of recall and 17-24 % more bytes on the old scene and nothing on the
  final one (+5 % bytes): lift.py's heights wobble with the terrain under each estimate. Off.

Against the target (>= 80 % of all objects as individual contacts; movers shown moving > 80 % with parked shown moving
< 5 %): all objects **88.6 %** individually (96.4 % with rings), not hard 92.2 %, < 20 px 78.9 %, mover sightings 89.2 %;
parked shown moving **4 %**; movers shown moving **66 %** — not met. What keeps movers from being shown moving: the
first seconds of a track (`unknown`, 39 sightings), creeping starts below 0.7 m/s (m1, m14, m5), and mover contacts that
lose the one-to-one match to a parked neighbour's contact in the same small box (m10's lags ~28 px for its first 3 s,
its filter starting at rest). Unmatched 656 -> 370 (~46 per frame).

**Thin links** (`PROFILE=hf|lora|telemetry`, 45 s replay, the receiver's contacts projected through the edge's camera
(`score_e2e.py --rx-cam edge`): these links carry `Ego`, not the camera `Pose`, so there is no drone view to score;
this measures where the contacts are in the world):

| link | bytes delivered (45 s) | all objects: single (+ rings) | mover sightings | position error | unmatched |
|---|---|---|---|---|---|
| video (clean), `mac_base` -> final | 243 kB -> 225 kB | 63.1 % (78.5 %) -> 88.5 % (96.4 %) | 85.8 % (93.8 %) -> 89.2 % (96.0 %) | 1.31 -> 0.81 m | 659 -> 372 |
| hf, 9.6 kbit/s | 49 kB -> 50 kB | 28.8 % (82.5 %) -> 46.8 % (84.0 %) | 51.9 % (78.1 %) -> 63.3 % (79.6 %) | 1.90 -> 1.04 m | 212 -> 127 |
| lora, 2 kbit/s | 8.9 kB -> 9.0 kB | 19.4 % (73.6 %) -> 27.8 % (75.5 %) | 29.3 % (67.6 %) -> 34.9 % (74.7 %) | 1.66 -> 1.19 m | 92 -> 47 |
| telemetry, 600 bit/s | 2.2 kB -> 2.2 kB | 4.2 % (32.6 %) -> 10.0 % (36.1 %) | 9.9 % (33.3 %) -> 16.4 % (30.6 %) | 1.24 -> 0.90 m | 25 -> 12 |

The thin links coarsen into groups by design (PROTOCOL.md 6.4): individuals collapse, rings hold on hf and lora.

**Presentation view** (the page's default; `?raw=1` draws every contact as the receiver holds it). Two changes, both
measured. In lift.py, false vehicles near the camera: a vehicle track that has not moved, whose median box is >= 60 px
or whose median range is < 100 m, and whose median detector confidence stays < 0.45, is not reported (close up the
detector is sure of a real car: every audited vehicle with a box >= 60 px was >= 0.78; the 21 tracks it holds back are
roofs, sheds, walls, bushes and blurred patches on their crops; the audit has one vehicle within 100 m, so that part is
judged on the crops): every audit number unchanged, unmatched 393 -> 378. And 15 sightings (half a second of frames)
before a track is reported, against 6: the detector's flickers (a roof called a bus for six frames, a bush) never reach
the edge, whose contacts outlive their tracks by a minute: individual 85.9 -> 88.6 %, mover sightings 91.0 -> 89.2 %,
unmatched 378 -> 370 (30 sightings: 87.2 %, movers 88.3 %, 296). Widening the near rule further would cost real objects:
the audit has a car at 130-160 m and a person at 100-130 m at 0.33-0.34, like the junk. In the page: a live contact is drawn once it
is 1 s old or its confidence (on the wire) is >= 0.5, a lost one only if it was that confident and for 10 s; bodies are
opaque, a group is one object, no labels, fades of 0.6 s; parked objects hold an anchor (moved only past 1 m or 0.3 of
their ring), every object glides to its target through a critically damped spring on the correction (movers continue
along their own recent path, so they do not lag), a jump over 12 m is faded rather than slid, height and heading eased.
Scored like the audit, the drawn set is 81.5 % of the inventory as single objects, 95.4 % with groups, every mover
sighting the raw view had, 169 unmatched (raw: 88.6 / 96.4 %, 370). The ground writes no depth: it is a backdrop, so a
car whose sent height is below the field (bases are noisy by a metre or two) is not drawn under it, and outside the
drone view an object below the field is stood on it. In the drone view at 30 fps (4-40 s, 1920x1080):

| | objects drawn per frame | visible jumps > 30 px in one frame | jerk p99 / p99.9, movers (px/frame^2) | parked |
|---|---|---|---|---|
| raw | 156 | 47 | 7.7 / 59.3 | 1.19 / 6.6 |
| presentation | 118 | 0 | 1.2 / 3.0 | 0.55 / 1.8 |

(The page draws a state once at its own time when it arrives, ahead of the recorder's next frame; that draw must not
advance the smoothing, or every object holds for three frames and moves in 10 Hz steps.)

Recording: `runs/recon3d/labrador-cal/rec/pov.mp4` (the operator's drone view, 0-40 s, `--rings 0`, clean link) and
`pov_sbs.mp4` (the footage beside it), made on the Mac with Playwright's Chromium in SwiftShader; objects stand at
their sent heights on the contact-height ground, and only movers draw trails.

## Limits, said plainly

- The operator's ground is a simulation built from the contacts' own base heights (`u` minus the class's centre height; per 16 m
  cell a low percentile, a 30 m kernel average of the residuals from a robust plane fit, eased over 2 s; `?ground=flat` is the old
  u = 0 plane). The RANSAC plane is not the local ground here (objects stand a median 6-9 m above it); against the field the
  contacts' bases are a median 2.3-3.4 m off (1.4-2.2 m for contacts with a ring <= 3 m; the field cannot beat the noise of the
  heights it is made of, and leave-one-out is within 0.7 m of that). Steadiness: refitting the plane to every new contact and feeding each
  contact's latest base made the ground heave (25 fixed points ahead of the drone each travelled a median 15 m up and
  down in 37 s); the plane now follows its refits over 15 s, a contact's base is its own 4 s average, liveness no
  longer weighs, and a cell averages its field over the time it has had contacts (up to 20 s): travel 15 -> 4.6 m,
  all of it settling (travel = range; ~0.3 m per 5 s late in the clip). The price: the shown field sits a median
  3.3-4.0 m from the live contacts' bases instead of 2.3-3.4 m. An object within 3 m of the field stands on it, one more
  than 6 m off keeps its sent height up to 12 m, beyond that it is drawn on the ground (`?snap=0` draws every object at its
  sent height). In the drone view every object stays at its sent height (that is what puts it on its pixels: 3 m of
  height moves a car 200 m out by ~20 px) and the ground comes up to it; the settling is for the chase, orbit and top
  views. Trails are drawn only for contacts the receiver calls moving, smoothed, and faded by how far they travelled
  past their own error ring and how straight.
- Stale contacts: a track that fragments leaves its old contact "lost" at the receiver for `t_depart` (60 s); the
  final run still shows ~49 unmatched contacts per frame (lost flickers, duplicates the 2 m link no longer groups,
  false boxes at the 0.1 floor, misplaced far cars).
- False dismounts: the person filter (above) leaves ~13 dismount contacts per frame against ~1.5 audited people; its
  aspect threshold was chosen with the 12 audited people in view, and they are all static, tiny and low-confidence, so a
  pedestrian seen from steeply above (aspect near 1) would be filtered too.
- Error rings of far objects (ce several metres along a grazing ray) span the drone view; `?rings=0` /
  `record.mjs --rings 0` hides them. A group of two merged duplicates can carry a ghost speed (one showed 21 m/s in the recording); group trails are not drawn.

- One terrain surface per 0.5 m cell: an object on a bridge deck is placed on the road below. Columns
  of occupied voxels (any surface below the ray) were tried and were worse on streets (walls and
  eaves read as ground), so they are not used.
- MASt3R does not trust the depth of small, fast objects (median confidence at its floor on the
  highway clip); those are placed from their pixels' direction and the terrain alone, which is
  noisier (part of the dismount speeds above). Far objects beyond the reconstructed area are dropped.
- The headless screenshots (GPU through Vulkan) show a dark rectangle at the top left once the video
  plays; nothing in the DOM is there, and it was not checked in a desktop browser.

- The scale rests on a 4.5 m car; a scene without cars needs another reference (altitude from the
  barometer, a known lane width).
- Chips are a view-dependent texture on a box: from far off the drone's line of sight the box shows
  its plain faces.
- Riders on two-wheelers the detector misses are only caught by speed; slow ones stay dismounts.
- Groups still form where people walk together or vehicles queue within ~1.6 m (detail level 0 of
  `ContactConfig { link_m: 3 }`); the page draws a group as a ring and a count.
- Offline replay: SLAM and detection run first, the driver replays their output in real time.
