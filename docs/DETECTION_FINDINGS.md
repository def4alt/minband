# Detection findings: which detector for the drone footage (2026-10-10)

Clip: `tools/footage/clips/battlefield/meva-uav-0307-1720.mp4` (MEVA UAV, 1920x1080, 29.97 fps, 35 s,
drone at 24-28 m over a campus). Run dir `runs/footage/meva-uav-0307-1720/`, frames 0, 6, ..., 1050
(176 detection frames, the same for every cache). Cars are ~50 px long, people ~20 px, the museum
tank ~100 px. Apple M4, onnxruntime CPU, 10 threads.

## Method

There is no ground truth. One person read four audit frames (150, 300, 600, 900) from the raw
frames and wrote down every real object (38: 30 cars/vans, 2 tanks, 8 people, 1 flatbed truck) with
its pixel box in `tools/footage/audit-inventory-meva-uav-0307-1720.json`, plus three regions where
the reading is uncertain (ignored). `tools/footage/detect_eval.py score` then counts, for every
cache and confidence floor, the inventory objects with at least one box on them (any class), the
boxes on nothing (false) and the extra boxes on an already counted object (dup). A box is "on" an
object when its IoU with the inventory box is >= 0.1 or it contains the object's centre. One
reader, four frames, compressed video: the numbers rank detectors, they are not accuracies.

Audit images (boxes drawn on the raw frames, class and confidence) are in
`runs/sidebyside/audit/models/<tag>-<frame>.jpg` for frames 150, 300, 600, 750, 900, 1050.

Runtime is the wall clock of `track.py detect` over the 176 frames (decode + ORB registration
included, ~0.2 s/frame of it) divided by 176; the m-size runs overlapped with another detection
process, so their numbers are pessimistic by up to 2x. Single-frame, idle-machine inference times
of the tiled detector alone are given in the table where measured.

## Models tried

| tag | model | training data | input | export | licence |
|---|---|---|---|---|---|
| `det` | `aerial-guardian.onnx` (YOLO26n-P2, the current model) | VisDrone | fixed 960, tiles 640 (1.5x) | end-to-end | AGPL-3.0 (Ultralytics) + VisDrone non-commercial |
| `coco` | `yolo11n.onnx` | COCO | dynamic, tiles 640 (1.0x) | classic | AGPL-3.0 |
| `coco26n` `coco26s` `coco26m` | YOLO26 n/s/m (Ultralytics 8.4.175, `yolo26*.pt`) | COCO | dynamic 640 | classic (a dynamic export is not end-to-end) | AGPL-3.0 |
| `coco_s` `coco_m` | YOLO11 s/m (`yolo11*.pt`) | COCO | dynamic 640 | classic | AGPL-3.0 |
| `vd26s` `vd26m` `vd11m` | YOLO26s / YOLO26m / YOLO11m fine-tuned on VisDrone-DET at imgsz 1280 by DetectionBench (`huggingface.co/dronefreak/visdrone-yolo26s`, `-yolo26m`, `-yolo11m`, file `best.pt`) | VisDrone | dynamic 640, tiles 640 (1.0x) | classic | AGPL-3.0 (card) + VisDrone CC BY-NC-SA 3.0: research / hackathon only, not a product |
| `vd26m_t1280` `vd26s_t1280` | same, tiles 1280 (2 padded tiles per 1080p frame) | | | | |
| `rfdetr_vd` | RF-DETR Nano fine-tuned on VisDrone (`huggingface.co/dronefreak/visdrone-rfdetr-nano`, `checkpoint_best_total.pth`), PyTorch CPU via `rfdetr` 1.11.2, tiles 640 | VisDrone | 640 | none (PyTorch) | Apache-2.0 package and card, VisDrone non-commercial |
| `vd26m_tta` | `vd26m` + horizontal-flip TTA, boxes kept only when the mirrored pass agrees (IoU >= 0.3) | | | | |

Not tried: `Xuban/LibreRFDETRTinys-visdrone` (needs the `libreyolo` package, CC BY-NC-SA, no time), D-FINE / DEIM (COCO
weights only; the COCO models below show COCO training is the problem, not the architecture), bigger VisDrone
YOLO26 l/x (`dronefreak/visdrone-yolo26l`, `-yolo26x`: available, 2-4x slower than m).

Downloaded checkpoints were scanned (`pickletools`) before loading: only `torch.*`, `ultralytics.nn.*` and
`collections.OrderedDict` globals, i.e. plain model pickles. Weights live in `tools/footage/models/` (gitignored).

## Results on the 1080p clip

`objects` = 38 inventory objects on frames 150/300/600/900; `found` = objects with a box; `false` = boxes on
nothing (sum over the 4 frames); `dup` = extra boxes on a counted object. `conf` = confidence floor applied
to the cache (every cache was written at 0.15, the tracker's low threshold; 0.30 is close to its high
threshold 0.35). Runtime: wall clock of the full `track.py detect` run over 176 frames / 176 (includes
~0.2 s/frame decode + registration; m-size runs overlapped with another run, so up to 2x pessimistic);
in brackets the tiled detector alone on one 1080p frame, idle machine, 10 threads.

| cache | s/frame | boxes/frame | found @0.15 | false @0.15 | found @0.30 | false @0.30 | found @0.40 | false @0.40 | misses at 0.30 |
|---|---|---|---|---|---|---|---|---|---|
| `det` (current) | 0.6 (0.55) | 21.1 | 37 | 47 | 31 | 15 | | | tank f600, dark car f600 top-right, 2 bottom-edge cars f300, dark SUV f900 |
| `coco` yolo11n | 0.3 (0.19) | 13.0 | 23 | 17 | 21 | 5 | | | all 8 people, the far cars and van at the top of f300 |
| `coco26n` | 0.3 (0.16) | 14.0 | 26 | 13 | 20 | 2 | | | all people, f300 far cars |
| `coco26s` | 0.5 (0.39) | 10.9 | 23 | 7 | 22 | 2 | | | all people, f300 far cars |
| `coco_s` yolo11s | 0.8 (0.49) | 12.5 | 27 | 21 | 23 | 10 | | | all people |
| `coco26m` | 2.3 (0.85) | 12.5 | 32 | 11 | 25 | 9 | 25 | 5 | all people; f300 far cars at 0.30 |
| `coco_m` yolo11m | 2.3 (0.93) | 9.7 | 20 | 1 | 19 | 0 | 19 | 0 | all people, all f300 far cars, bottom-edge cars |
| `vd26s` | 0.9 (0.39) | 17.7 | 34 | 21 | 33 | 10 | | | 1 person f150, flatbed f300, bottom-edge car f600/f900; false: garage roofs as 150-250 px trucks |
| **`vd26m`** | 2.4 (0.96) | 22.1 | **37** | 26 | **36** | 6 | 35 | 4 | flatbed f300, tank f900 (0.18); false at 0.30: 5 roof objects as person/moto, 1 cart-like object |
| **`vd11m`** | 2.1 (1.06) | 15.9 | **37** | 10 | **36** | 6 | 35 | **1** | flatbed f300, bottom-edge car f900 |
| `rfdetr_vd` (6 audit frames only) | 2.6 | 40.3 | 35 | 107 | 32 | 30 | 32 | 10 | tank f600+f900, flatbed f300, bottom-edge cars; floods roofs with 0.15-0.3 persons/trucks |
| `vd26m_tta` (flip TTA, agreement) | 4.4 (1.9) | 15.7 | 37 | 14 | 36 | 4 | 35 | 3 | as `vd26m`; the f900 tank comes back at 0.15 |

Per-frame detail (found/objects, f = false, d = dup) at conf 0.30:

| cache | f150 (2) | f300 (13) | f600 (10) | f900 (13) |
|---|---|---|---|---|
| `det` | 2/2 f0 | 10/13 f1 | 7/10 f10 d1 | 12/13 f4 d2 |
| `coco` | 0/2 f1 | 2/13 f0 | 9/10 f1 d1 | 10/13 f3 |
| `coco26m` | 0/2 f4 | 7/13 f1 | 8/10 f2 d1 | 10/13 f2 d1 |
| `vd26s` | 1/2 f1 | 11/13 f0 | 9/10 f4 d1 | 12/13 f5 d2 |
| `vd26m` | 2/2 f3 | 12/13 f1 | 10/10 f0 d1 | 12/13 f2 d4 |
| `vd11m` | 2/2 f0 d1 | 12/13 f0 | 10/10 f2 d1 | 12/13 f4 d3 |
| `rfdetr_vd` | 2/2 f10 | 12/13 f3 | 8/10 f5 d1 | 10/13 f12 |

What the numbers say:

- Training data matters more than architecture or size. Every COCO model, YOLO11 or YOLO26, n to m, misses
  all 8 people (20 px tall from above is not a COCO person) and most of the far cars at the top of frame 300
  (perspective: the top of the frame is 2x farther away, cars there are ~30 px). YOLO26 vs YOLO11 at the same
  size is a wash on this footage (26n/26s slightly fewer false boxes, 26m slightly more recall than 11m, both
  within one or two boxes).
- The VisDrone fine-tunes of the m models are a different class: 36/38 at the tracker's high threshold with
  6 false boxes, against 31/38 and 15 false for the current `aerial-guardian` model, and they find the
  objects the two-detector consensus was losing (the dark SUV at the frame edge, the bottom-edge vans, the
  tank in f600 at 0.5-0.6 as `truck`). `vd11m` is the cleaner single model (1 false box at conf 0.40 with
  35/38), `vd26m` the one with the most recall at the low threshold. Both see the tank only as `truck` (no
  armoured class in VisDrone).
- Their remaining false boxes are of two kinds: (1) `vd26s` and, at 0.15, the m models call the gabled garage
  roofs in the lower-left `truck` with 150-250 px boxes, 3-5x a car; (2) small roof vents at 0.15-0.4 as
  `person` / `moto`. Kind (1) is a size gate away (a 220 px box at this altitude is a 20 m vehicle); kind (2)
  does not pass the high threshold and does not persist as a track.
- RF-DETR nano (VisDrone) is not competitive here: at a floor of 0.40 it matches `vd26s` (32/38, 10 false) at
  3x the runtime, and it floods the roofs below 0.3. DetectionBench's own VisDrone table agrees (RF-DETR nano
  37.9 mAP50 vs YOLO26m 49.1), so the bigger RF-DETR small/medium were not run.
- Horizontal-flip TTA on `vd26m` (audit frames): the union adds one object (the f900 tank) and doubles the
  false boxes; the agreement mode (`--mode and`) keeps 36/38 at 0.30 and cuts the false boxes from 6 to 4
  (26 to 14 at 0.15) for 2x the runtime. Marginal; the two-model consensus below does the same job better.

## Consensus pairs

`detect_eval.py consensus RUN A B --iou T --mode and|union`: a box of A is kept when any box of B (any class)
overlaps it by IoU >= T; `union` also adds B's matched boxes that A lacks (class-wise NMS). Scored like the caches.

| pair (IoU 0.3, `and`) | boxes/frame | found @0.15 | false @0.15 | found @0.30 | false @0.30 |
|---|---|---|---|---|---|
| `det` + `coco` (the current `cons`) | 8.1 | 21 | 0 | 19 | 0 |
| `det` + `coco26s` | 8.4 | 22 | 0 | 20 | 0 |
| `vd26m` + `coco26m` | 9.8 | 32 | 0 | 31 | 0 |
| `vd26m` + `coco_m` | 7.8 | 20 | 0 | 20 | 0 |
| `det` + `vd26s` | 12.3 | 33 | 6 | 30 | 1 |
| `det` + `vd26s`, IoU 0.5 | 11.7 | 33 | 5 | 30 | 1 |
| `det` + `vd26m` | 12.2 | 36 | 6 | 31 | 2 |
| `det` + `vd26m`, IoU 0.5 | 11.9 | 36 | 4 | 31 | 1 |
| `vd26s` + `vd26m` | 12.4 | 34 | 5 | 33 | 3 |
| **`vd26m` + `vd11m`** (`vd_pair`) | 13.4 | **36** | **4** | 35 | 2 |
| `vd26m` + `vd11m`, `union` (`vd_pair_union`) | 14.1 | 36 | 4 | 36 | 3 |

- Any pair with a COCO model has zero false boxes and zero people: COCO never confirms a person, and it loses
  the far cars. The current `cons` (21/38) is precise and half blind.
- `vd26m` AND `vd11m` at IoU 0.3 (`vd_pair`) keeps 36/38 at the low threshold with 4 false boxes in 4 frames
  (three 6-10 px "persons" at 0.2-0.4 on roof edges and one 0.80 "car" on a cart-like object by the garages
  that may be real). Per frame at 0.15: f150 2/2 f0, f300 12/13 f0, f600 10/10 f1, f900 12/13 f3. It misses
  the flatbed at the top-left corner of f300 (neither model has it) and one bottom-edge car in f900.
  IoU 0.5 instead of 0.3 removes one false box and no object on the `det` pairs; 0.3 is fine, the two models
  draw nearly the same boxes.
- `det` + `vd26m` is the cheapest upgrade that keeps the current model (36/38 at 0.15, 6 false; 4 at IoU 0.5).

## Recommendation

Replace the VisDrone-nano + COCO-nano pair by the two VisDrone m models and keep the consensus rule:

```sh
cd tools/footage
.venv/bin/pip install -U ultralytics onnx onnxslim            # once; torch comes with it (CPU)
# weights: huggingface.co/dronefreak/visdrone-yolo26m and visdrone-yolo11m, file best.pt, into models/visdrone-dl/
.venv/bin/python export_models.py models/visdrone-dl/visdrone-yolo26m.pt --name vd26m
.venv/bin/python export_models.py models/visdrone-dl/visdrone-yolo11m.pt --name vd11m
V=clips/battlefield/meva-uav-0307-1720.mp4; O=../../runs/footage/meva-uav-0307-1720
.venv/bin/python -I track.py detect $V --model models/vd26m.onnx --tag vd26m --out $O --conf 0.15   # tile auto = 640, 1.0x
.venv/bin/python -I track.py detect $V --model models/vd11m.onnx --tag vd11m --out $O --conf 0.15
.venv/bin/python -I detect_eval.py consensus $O vd26m vd11m --iou 0.3 --mode and --out vd_pair      # detections-vd_pair.npy
.venv/bin/python -I detect_eval.py score $O vd26m vd11m vd_pair --inventory audit-inventory-meva-uav-0307-1720.json
.venv/bin/python -I detect_eval.py draw  $O vd_pair --video $V --out ../../runs/sidebyside/audit/models
```

Settings: tile 640 at 1.0x (the dynamic export runs a 640 px tile at 640; the m models were trained at
1280 on full VisDrone frames, so objects of 20-100 px are in their training range without magnification;
see the tile-1280 rows below), conf floor 0.15 in the cache, the tracker's 0.35/0.15 thresholds as they are.
Cost: two m models, about 2.0 s per 1080p frame together on this machine (0.96 + 1.06, idle, 8 tiles of
640) against 0.74 for the current pair (0.55 + 0.19); at the phone's 12 Hz detector rate this is a CoreML question, not an ONNX-CPU one.

If one model only: `vd11m` at a 0.40 floor (35/38, 1 false box in 4 frames) or `vd26m` at 0.30.

For the tracking agent (consensus.py / track.py):

1. Sources `vd26m,vd11m` (or `det,vd26m`) instead of `det,coco`; the agreement rule and IoU 0.3 can stay.
2. Take the class from the higher-confidence box of the agreeing pair, not from the first source: `vd26m`
   calls the silver car at the bottom of f300 `moto` 0.3 while `vd11m` says `car` 0.6; the consensus cache
   currently inherits A's class.
3. A size gate on appearance boxes in metres (e.g. drop vehicle boxes longer than ~14 m on the ground)
   removes the garage-roof "trucks" that both VisDrone models emit at 0.15-0.65, the one false-positive
   family the consensus does not fully remove when `vd26s` or `det` is in the pair.
4. The tank is `truck` at 0.18-0.6 in the VisDrone models (0.55 in `det`); if an armoured label matters,
   that is a separate (military) model or a track-level rule, not a detector setting.

## Inference settings

| cache | tiles / 1080p frame | s/frame (detector only, idle) | found @0.15 | false @0.15 | found @0.30 | false @0.30 |
|---|---|---|---|---|---|---|
| `vd26m` tile 640 (1.0x, 8 tiles) | 8 | 0.96 | 37 | 26 | 36 | 6 |
| `vd26m_t1280` tile 1280 (1.0x, 2 padded tiles) | 2 | 1.09 | 35 | 10 | 33 | 5 |
| `vd26s` tile 640 | 8 | 0.39 | 34 | 21 | 33 | 10 |
| `vd26s_t1280` tile 1280 | 2 | 0.39 | 35 | 8 | 31 | 3 |

- Bigger tiles (more context, same magnification) cost the same (two padded 1280 px tiles are as many
  pixels as eight 640 px tiles) and lose 2-3 objects at 0.30 (the f900 tank, the dark SUV, a far car) while
  removing a few roof false boxes. Tile 640 stays.
- Magnification above 1.0x was not needed on this clip: the VisDrone fine-tunes were trained at 1280 on
  full 2000 px VisDrone frames, so a 20 px person is in their training range. A fixed-960 export
  (`export_models.py --imgsz 960 --fixed`, tiles 640 at 1.5x like `aerial-guardian`) would be the way to
  try it for the 4K clip; not run (time).
- Confidence floor: keep writing caches at 0.15 and let the tracker's 0.35 / 0.15 pair do the gating; the
  score tables give the trade-off per model (`vd11m` is clean at 0.40, `vd26m` is best read at 0.30).
- NMS is class-wise at IoU 0.5 as before; the `dup` column (same object, two classes: `car` + `truck` on
  vans, `car` + `moto` on small cars) is 3-5 per 4 frames for the m models and the tracker's fusion merges
  them.

## 4K clip (80 m, people 15-35 px): `vd26m` on a 31-frame window

`runs/footage/meva-2018-03-13.16-00-14-bf/detections-vd26m_w.npy`, frames 1200-1380 (same grid as `det`),
tile 640 at 1.0x (28 tiles, ~3 s/frame while another run shared the CPU). No inventory was made; one frame
(1200) was read at 1:1 (`runs/sidebyside/audit/models/4k-{det,vd26m_w}-1200{,-crop}.jpg`).

| | boxes/frame @0.3 | persons/frame @0.3 | boxes with a match (IoU 0.3) in the other cache |
|---|---|---|---|
| `det` (aerial-guardian, 1.5x) | 55.9 | 39.9 | 89 % |
| `vd26m_w` (1.0x) | 82.2 | 63.8 | 65 % |

On frame 1200 the crowd scene has two groups of 10-20 people and a dozen walkers on the paths; `vd26m` boxes
all of the walkers and nearly all of the groups at 0.4-0.75, `det` at 0.3 misses about a third of them
(walkers on the left path, several in the lower group). 89 % of `det`'s boxes are confirmed by `vd26m`; the
extra third of `vd26m`'s boxes are, on the frame read, mostly people `det` lacks, not clutter. A proper
audit of this clip needs its own inventory; the direction is the same as on the 1080p clip.
