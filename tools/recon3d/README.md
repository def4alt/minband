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

Driver env: `POS_RES` (default 1 = 10 cm; 2 = 1 m, the old default), `LINK_M` (3), `CE_LINK` (0: the grouping
distance is `max(link_m, ce_link x ce)`; lift.py merges its own duplicates, so error radii do not group parked rows),
`SEED` (the link's loss is a seeded PRNG), `CLOCK=manual` (steps on `{cmd:'step'}`), `DUMP=<file.jsonl>` (batch
replay, one state per 0.1 s step, then exit). Page: `?view=drone`, `?rec=1` (no video inset, for recording).

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
| static map | **not sent**: it stays on the drone, for placing objects; the operator's ground is a flat simulation at u = 0 (the reconstruction's ground plane) |
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
| + motion test and static estimate (**final**) | 56 % | 64 % (79 %) | 67 % (83 %) | 86 % (94 %) | 64 % | 6.4 px / 1.39 m | 656 | 30/484 |

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
  reached the operator as moving. A track now moves only when a weighted line fit over 2 s has a significant velocity
  (chi2 > 20, > 0.7 m/s, held 0.5 s); otherwise it reports the information-weighted mean of its last 5 s, no velocity.
  Parked shown moving 37 % -> 6 %; the cost is a few points of recall and slow movers reported still until caught.

Against the target (80 % of objects, the movers above all): movers 86 % as individual contacts (94 % counting
group rings), not-hard objects 83 % with rings / 67 % individually, all objects 79 % / 64 %. The objects missed are
mostly the far lot's < 20 px cars (64 % reach the operator); individual recall is held down by duplicate tracks the
edge groups (1.6 m link) and by stale contacts of fragmented tracks (the 656 unmatched, ~80 per frame, half of them
lost contacts).

## Limits, said plainly

- The operator's ground is flat (u = 0, the RANSAC ground plane, fitted to 27 % of the map on this clip): objects stand
  at their transmitted heights on their own rings, which here are a median 5-9 m above the sim's grid up to 500 m out
  and ~19 m beyond (the ground rises towards the mall and the town). From the drone's point of view they project where
  they belong; from the chase or orbit views they hover. A coarse ground from the contacts' own base heights would fix
  the look.
- Stale contacts: a track that fragments leaves its old contact "lost" at the receiver for `t_depart` (60 s); the
  final run still shows ~80 unmatched contacts per frame (half lost, half fresh: duplicates, false boxes at the
  0.1 floor, misplaced far cars).
- False dismounts: at the 0.1 floor the VisDrone model calls roof vents, chimneys and poles "pedestrian"; the receiver
  holds ~37 dismount contacts per frame against ~1.5 people in the inventory (164 of the 656 unmatched). Nothing
  filters person tracks by confidence or 3D size yet.
- Error rings of far objects (ce several metres along a grazing ray) span the drone view; `?rings=0` /
  `record.mjs --rings 0` hides them. Their 3 s trails show the same range jitter as thin lines towards the horizon,
  and a group of two merged duplicates can carry a ghost speed (one showed 21 m/s in the recording).

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
