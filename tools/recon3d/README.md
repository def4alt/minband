# recon3d: the drone sends 3D, the operator sees the reconstructed scene

Drone video -> MASt3R-SLAM reconstruction -> YOLO instance segmentation -> every detection placed
*in the reconstruction* (no flat ground) -> 3D tracks with a height -> MinBand contact reports
(real WASM edge, shaped link, WASM receiver) -> a 3D page where each received contact stands in the
reconstructed scene as a solid of its measured size wearing the drone's own view of it.

This is DESIGN.md §5 (the 3D handoff) moved onto the edge: the edge has the reconstruction, so a
contact's ray meets the reconstructed terrain there and the contact goes out with its height
(`Contact.dz`, ext bit1), its ray at the object itself, and the camera `Pose` stream in the video
regime. The static map is 3d-map-stream's channel.

## Run

```bash
# one sequence end to end (~5-15 min on the RTX 5090: SLAM ~3 fps, YOLO ~2 fps at 1344 px with masks)
tools/recon3d/run.sh ../datasets/visdrone-mot/VisDrone2019-MOT-val/sequences/uav0000182_00000_v runs/recon3d/vd182
tools/recon3d/run.sh ../datasets/visdrone-mot/VisDrone2019-MOT-train/sequences/uav0000076_00720_v runs/recon3d/vd076

# the page (Node 22: `mise exec node@22 --` if the system node is older); every runs/recon3d/<run> is a scene
cd tools/recon3d && npm install && npm start            # http://localhost:8092
SCENE=vd076 PROFILE=lora npm start                      # start on another scene or link; the page switches scenes too
node scripts/screenshot.mjs out.png --at 6 --view chase # headless Chrome (Vulkan GPU; SOFTGL=1 without one)
```

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
| static map | **not on this link**: 3d-map-stream's channel (10-20 kbit/s); the page loads it whole |
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

## Limits, said plainly

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
