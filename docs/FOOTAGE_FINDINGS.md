# Footage findings: detection and tracking on battlefield-like drone footage

Status: 2026-10-10. Scope: detection and tracking for situational awareness with a human in the
loop (MinBand's edge). Affiliation is never assessed. Nothing here is built or wired for targeting.

What was measured: the footage pipeline in `tools/footage` (README there for the method) before and
after the changes of this round, on drone clips it was never tuned on, without labels. The numbers
come from `track.py` / `audit.py` runs in `runs/footage/` (not committed).

## Protocol

- Clips: a manifest split `dev` / `heldout` before any detector work. Tuning used only MEVA
  2018-03-13.16-00-14 (4K, the existing tuning clip) and the dev clips `amad-test1` (real military
  convoy, 596x336, 25 fps) and `hituav-60m-30_1` (thermal, 640x512, 7.5 fps). The synthetic dev clip
  (ARMA 3, burned-in boxes) was a pipeline smoke test only and is in no number below.
- Parameters were frozen at commit `d59938b` (the defaults in `track.py` / `mti.py` / `detect.py`).
  The five held-out clips were then each run once (`amad-test2`, `mvt-test10`, `meva-uav-0307-1720`,
  `hituav-120m-30_3`, `hituav-70m-90_1`); nothing was changed after looking at them. After freezing,
  only evaluation tooling changed (`c5f0f9a`: audit sampling and scoring); the pipeline did not.
- The dev and held-out military clips (`amad-test1`, `amad-test2`) share a stock-footage source
  (different scenes, terrain and altitude). Both are watermarked previews without a licence: numbers
  only, no frames anywhere in the repo. The same applies to `mvt-test10` (unknown licence).
- "Old" is the pipeline as it was: the original `detect.py` (VisDrone YOLO26n-P2, tile 640, conf
  0.15, with its NMS bug, frames smaller than a tile stretched) and the original tracker
  (`--legacy-tracker`), on the same frames. "Improved" is the frozen pipeline: fixed detector,
  motion detector (MTI), fusion, new tracker. Two ablations separate the parts: the new detector
  with the old tracker, and the new detector with the new tracker but no MTI.
- The visual audit is a **post-hoc visual audit by the developer, after freezing, for evaluation
  only, not ground truth**: 12 frames per clip drawn with seed 0 from the detection schedule, each
  box judged on the raw and the annotated frame side by side at legible scale; one person's reading
  of small objects in compressed video. Precision and recall are given with Wilson 95 % intervals;
  recall counts objects, precision counts boxes (a second box on an object is a false positive).

## What changed

1. **NMS bug (detector).** `cv2.dnn.NMSBoxes` takes x, y, w, h; it was given x1, y1, x2, y2, so every
   box was as large as its own coordinates and same-class neighbours suppressed each other. On 4K
   MEVA the VisDrone model kept 25-35 boxes per frame before and 60-95 after; on the held-out 1080p
   MEVA clip 14.5 before and 22.5 after. This is the largest single change in recall on MEVA-like
   scenes, and it has nothing to do with battlefield footage.
2. **Resolution-aware tiling.** A fixed 1.5x magnification into the 960 px input at every frame
   size; frames smaller than a tile are padded, not stretched. On the two 596x336 military clips the
   old detector found almost nothing (0.6 and 0.04 boxes per frame) and the new one 3.3 per frame.
3. **MTI** (class-agnostic motion, `mti.py`), with label-free overlay masking: the watermark,
   channel logo and HUD were found and masked on the clips that have one (7.1 % of the frame on
   amad-test1, 3.7 % amad-test2, 3.1 % mvt-test10, 0-0.3 % elsewhere); 28 appearance boxes on the
   mvt-test10 channel logo were dropped (the old pipeline tracked the logo as a vehicle).
4. **Fusion and classes.** Motion-only detections become class 100 (unclassified ground mover);
   appearance classes win where they exist.
5. **Tracker.** Coasting 2 s with growing gates, re-acquisition of lost tracks (3 s), tracks
   reported from 1 s of age, a static mode for parked objects, companion suppression.
6. **Registration** that survives thermal and long pans (optical-flow chain with hysteresis): the
   old registrar crashed on the dev thermal clip after 65 s.

## Military appearance model

`yolo8n_military.onnx` (github.com/MErenKaya/Military-Vision-Enhancement-YOLO; no licence file,
Ultralytics AGPL-3.0 in its metadata; 12 classes incl. military_tank, military_vehicle, soldier) was
tried on the dev convoy clip and on MEVA. On amad-test1 it boxed the armoured vehicles VisDrone
missed (4.6 boxes per frame vs 2.9) and labelled most of them armoured (2.4 per frame at conf
>= 0.35). On the civilian MEVA campus (15 frames sampled) it labelled 6.7 objects per frame armoured
at conf >= 0.35 and 2.9 at >= 0.5: cars, a trailer, portable toilets, roof units; at >= 0.6 it
still found 1.1 per frame there against 0.8 on the convoy. A label that fires as often on a school
car park as on a convoy is not information, and motion already finds the moving vehicles. Not used;
class 101 (armoured) exists in MinBand but nothing emits it in the frozen pipeline. The AMAD models
(github.com/InvictusRex, .pt only, no licence) were not tried, because the held-out stock clips are
that repository's own test videos; KasSahin/tank-datection-yolov8n (MIT) ships no weights.

## Label-free metrics

Per detector source (improved pipeline). Agreement: an MTI mover absorbed by an appearance box, or
an appearance box that motion confirmed. Persistence: the fraction of a source's detections that end
in a confirmed track lasting >= 1 s.

| Clip | Split | Source | Detections/frame | Agreement with another source | In a confirmed track >= 1 s |
|---|---|---|---:|---:|---:|
| amad-test1 (RGB 596x336) | dev | VisDrone appearance | 2.92 | 0.68 | 0.96 |
| amad-test1 (RGB 596x336) | dev | MTI (motion) | 5.14 | 0.43 | 0.85 |
| hituav-60m-30_1 (thermal) | dev | VisDrone appearance | 4.00 | 0.48 | 0.70 |
| hituav-60m-30_1 (thermal) | dev | MTI (motion) | 5.99 | 0.38 | 0.93 |
| amad-test2 (RGB 596x336) | held-out | VisDrone appearance | 3.33 | 0.63 | 0.78 |
| amad-test2 (RGB 596x336) | held-out | MTI (motion) | 4.54 | 0.70 | 0.94 |
| mvt-test10 (RGB 720p) | held-out | VisDrone appearance | 4.97 | 0.06 | 0.58 |
| mvt-test10 (RGB 720p) | held-out | MTI (motion) | 0.94 | 0.34 | 1.00 |
| meva-uav-0307-1720 (RGB 1080p) | held-out | VisDrone appearance | 22.49 | 0.00 | 0.76 |
| meva-uav-0307-1720 (RGB 1080p) | held-out | MTI (motion) | 0.13 | 0.35 | 0.87 |
| hituav-120m-30_3 (thermal) | held-out | VisDrone appearance | 3.32 | 0.25 | 0.59 |
| hituav-120m-30_3 (thermal) | held-out | MTI (motion) | 1.94 | 0.43 | 0.99 |
| hituav-70m-90_1 (thermal) | held-out | VisDrone appearance | 3.07 | 0.11 | 0.35 |
| hituav-70m-90_1 (thermal) | held-out | MTI (motion) | 0.53 | 0.64 | 0.98 |

Tracks, fragmentation and static jitter, old → improved (with the two ablations). Static: objects
that do not move (net displacement < 1 m over > 5 s): count, median position std, median KF speed.
MinBand bytes on a clean link (`npm run replay`, θ 0.15 m and 0.5 m).

| Clip | Split | Pipeline | Tracks | Births/min | Median track (s) | Entities/frame | Dets in a >=1 s track | Motion-only/frame | Static: n, std (m), KF speed (m/s) | MinBand B/s at 0.15 / 0.5 m | Mean error (cm) at 0.15 / 0.5 m |
|---|---|---|---:|---:|---:|---:|---:|---:|---|---|---|
| amad-test1 (RGB 596x336) | dev | old | 1 | 4 | 5.4 | 1.0 | 0.50 | - | - | 146 / 74 | 4.2 / 17.6 |
| amad-test1 (RGB 596x336) | dev | new detector, old tracker | 4 | 15 | 14.7 | 3.4 | 0.87 | - | - | 438 / 203 | 4.0 / 14.6 |
| amad-test1 (RGB 596x336) | dev | new detector + tracker | 4 | 15 | 10.5 | 2.8 | 0.86 | - | - | 370 / 159 | 4.4 / 14.3 |
| amad-test1 (RGB 596x336) | dev | improved (det + MTI) | 12 | 46 | 6.7 | 6.4 | 0.84 | 2.9 | - | 761 / 533 | 3.5 / 11.4 |
| hituav-60m-30_1 (thermal) | dev | old | 54 | 23 | 4.4 | 2.6 | 0.52 | - | 3, 0.60, 0.24 | 280 / 221 | 2.2 / 8.0 |
| hituav-60m-30_1 (thermal) | dev | new detector, old tracker | 68 | 29 | 4.8 | 3.4 | 0.49 | - | 3, 0.42, 0.30 | 277 / 211 | 2.7 / 10.5 |
| hituav-60m-30_1 (thermal) | dev | new detector + tracker | 54 | 23 | 9.1 | 4.3 | 0.56 | - | 2, 0.39, 0.00 | 378 / 263 | 3.0 / 10.7 |
| hituav-60m-30_1 (thermal) | dev | improved (det + MTI) | 109 | 47 | 9.3 | 9.7 | 0.84 | 3.7 | - | 890 / 658 | 2.5 / 10.2 |
| amad-test2 (RGB 596x336) | held-out | old | 0 | - | - | - | 0.00 | - | - | 0 / 0 | 0.0 / 0.0 |
| amad-test2 (RGB 596x336) | held-out | new detector, old tracker | 5 | 26 | 7.1 | 2.7 | 0.64 | - | - | 363 / 200 | 3.8 / 13.0 |
| amad-test2 (RGB 596x336) | held-out | new detector + tracker | 4 | 21 | 6.8 | 2.8 | 0.64 | - | - | 344 / 193 | 3.9 / 11.1 |
| amad-test2 (RGB 596x336) | held-out | improved (det + MTI) | 12 | 63 | 5.7 | 5.6 | 0.84 | 1.4 | - | 758 / 520 | 2.8 / 11.7 |
| mvt-test10 (RGB 720p) | held-out | old | 8 | 77 | 3.0 | 4.2 | 0.65 | - | - | 656 / 510 | 1.8 / 11.3 |
| mvt-test10 (RGB 720p) | held-out | new detector, old tracker | 10 | 97 | 2.5 | 4.5 | 0.59 | - | - | 936 / 784 | 1.6 / 10.3 |
| mvt-test10 (RGB 720p) | held-out | new detector + tracker | 6 | 58 | 2.8 | 3.3 | 0.51 | - | - | 758 / 617 | 2.4 / 13.7 |
| mvt-test10 (RGB 720p) | held-out | improved (det + MTI) | 14 | 135 | 2.4 | 7.2 | 0.63 | 0.6 | - | 995 / 863 | 1.9 / 8.8 |
| meva-uav-0307-1720 (RGB 1080p) | held-out | old | 70 | 120 | 3.9 | 11.2 | 0.65 | - | 9, 0.54, 0.28 | 1082 / 804 | 2.7 / 9.7 |
| meva-uav-0307-1720 (RGB 1080p) | held-out | new detector, old tracker | 96 | 164 | 4.2 | 18.7 | 0.72 | - | 10, 0.31, 0.13 | 1394 / 992 | 3.3 / 9.6 |
| meva-uav-0307-1720 (RGB 1080p) | held-out | new detector + tracker | 80 | 137 | 7.6 | 22.2 | 0.76 | - | 12, 0.34, 0.00 | 1696 / 1086 | 3.8 / 9.5 |
| meva-uav-0307-1720 (RGB 1080p) | held-out | improved (det + MTI) | 84 | 144 | 7.5 | 22.9 | 0.76 | 0.1 | 12, 0.34, 0.00 | 1783 / 1132 | 3.8 / 9.4 |
| hituav-120m-30_3 (thermal) | held-out | old | 15 | 31 | 2.4 | 2.6 | 0.46 | - | 1, 0.28, 0.23 | 342 / 260 | 1.8 / 9.8 |
| hituav-120m-30_3 (thermal) | held-out | new detector, old tracker | 10 | 21 | 4.0 | 2.1 | 0.46 | - | 1, 0.23, 0.41 | 276 / 199 | 1.8 / 10.4 |
| hituav-120m-30_3 (thermal) | held-out | new detector + tracker | 7 | 15 | 6.4 | 2.4 | 0.50 | - | - | 300 / 198 | 3.0 / 11.8 |
| hituav-120m-30_3 (thermal) | held-out | improved (det + MTI) | 45 | 94 | 3.2 | 7.4 | 0.69 | 1.1 | - | 588 / 488 | 1.5 / 5.6 |
| hituav-70m-90_1 (thermal) | held-out | old | 3 | 5 | 8.8 | 1.0 | 0.04 | - | - | 54 / 54 | 1.4 / 1.4 |
| hituav-70m-90_1 (thermal) | held-out | new detector, old tracker | 4 | 7 | 19.1 | 2.2 | 0.28 | - | - | 143 / 123 | 1.8 / 8.6 |
| hituav-70m-90_1 (thermal) | held-out | new detector + tracker | 4 | 7 | 29.2 | 2.9 | 0.33 | - | - | 153 / 132 | 2.1 / 7.6 |
| hituav-70m-90_1 (thermal) | held-out | improved (det + MTI) | 9 | 16 | 4.1 | 2.7 | 0.39 | 0.2 | - | 213 / 173 | 1.6 / 6.5 |

Reading it:

- The detector changes are where the objects come from: on the low-resolution military clips the
  old pipeline tracked 1 object (amad-test1) and none (amad-test2); the new detector alone tracks 4
  and 5, and with MTI 12 and 12.
- The tracker changes cut births per minute where the detector is the same (new detector, old →
  new tracker): MEVA 4K 182 → 105 (on the old detector cache, below), held-out MEVA 164 → 137,
  mvt-test10 97 → 58, amad-test2 26 → 21, hituav-120m 21 → 15; median track length roughly doubles;
  parked objects stop drifting (KF speed 0.13-0.41 m/s → 0.00).
- MTI raises births per minute again on some clips (amad-test2 21 → 63, hituav-120m 15 → 94): motion
  tracks fragment more than appearance tracks (an object that stops, or turns side-on, loses its
  motion blob), and long low-sun shadows add false movers. That is the price of finding
  unclassified movers; it is visible in the bytes.
- Bytes go up with the improved pipeline because it tracks more objects (entities per frame 2.8 →
  5.6 on amad-test2, 2.4 → 7.4 on hituav-120m). Per entity the cost is about flat.

### Tracker changes on the tuning clip (MEVA 4K, 15-105 s)

On the detector cache with the old NMS (the fixed-NMS re-detection of this clip did not finish, see
Unfinished). Same detections in every row; only the tracker (and MTI) change.

| Tracker | Tracks | Births/min | Median track (s) | Entities/frame | Static: n, std (m), KF speed (m/s) | Static wander: global / per object (m RMS) | MinBand B/s at 0.15 / 0.5 m | Mean error (cm) at 0.15 / 0.5 m |
|---|---:|---:|---:|---:|---|---|---|---|
| original (`--legacy-tracker`) | 273 | 182 | 4.2 | 27.2 | 47, 0.21, 0.12 | 0.10 / 0.81 | 2701 / 1801 | 3.5 / 11.8 |
| new tracker | 157 | 105 | 12.9 | 36.2 | 35, 0.17, 0.00 | 0.09 / 0.31 | 2985 / 2043 | 3.4 / 8.6 |
| new tracker + MTI | 208 | 139 | 11.2 | 41.9 | 38, 0.17, 0.00 | 0.08 / 0.34 | 3636 / 2494 | 3.3 / 8.5 |
| new, coast 1 s | 192 | 128 | 8.0 | 29.2 | 45, 0.16, 0.00 | 0.08 / 0.33 | 2769 / 1958 | 3.3 / 8.5 |
| new, no static mode | 159 | 106 | 12.9 | 35.7 | 35, 0.23, 0.13 | 0.10 / 1.05 | 3062 / 2094 | 3.2 / 10.9 |
| new, no re-acquisition | 195 | 130 | 9.4 | 33.8 | 42, 0.16, 0.00 | 0.08 / 0.45 | 2886 / 2011 | 3.4 / 8.6 |
| new, min age 0 | 157 | 105 | 13.5 | 37.0 | 34, 0.17, 0.00 | 0.09 / 0.37 | 3102 / 2143 | 3.4 / 8.7 |

Per entity, bytes fall with the new tracker (99 → 82 B/s per entity at θ 0.15, 66 → 56 at 0.5) while
it keeps more objects alive (27 → 36 per frame). The minimum age saves 4-5 % of the bytes, the
static mode 3 % and 2 cm of error at θ 0.5.

Static jitter is per object, not global: on MEVA 4K the deviation of static tracks from their own
mean is 0.81 m RMS per object but only 0.10 m in common across all of them (the registration); with
the static mode it falls to 0.31 m per object.

## Visual audit (post-hoc, developer, after freezing; not ground truth)

Per clip, old vs improved, at the detection level (every box the tracker saw) and the track level
(boxes in confirmed tracks, what reaches the link):

| Clip | Split | Frames | Objects | Old: P det | Old: R det | Improved: P det | Improved: R det | Old: P track | Old: R track | Improved: P track | Improved: R track |
|---|---|---:|---:|---|---|---|---|---|---|---|---|
| amad-test1 (RGB 596x336) | dev | 12 | 47 | 0.73 [0.43-0.90] | 0.17 [0.09-0.30] | 0.63 [0.51-0.74] | 0.87 [0.75-0.94] | 1.00 [0.51-1.00] | 0.09 [0.03-0.20] | 0.67 [0.55-0.78] | 0.87 [0.75-0.94] |
| hituav-60m-30_1 (thermal) | dev | 12 | 166 | 0.83 [0.70-0.91] | 0.23 [0.18-0.30] | 0.94 [0.87-0.97] | 0.54 [0.47-0.62] | 0.89 [0.72-0.96] | 0.14 [0.10-0.21] | 0.96 [0.90-0.99] | 0.48 [0.41-0.56] |
| amad-test2 (RGB 596x336) | held-out | 12 | 94 | n/a | 0.00 [0.00-0.04] | 0.88 [0.77-0.94] | 0.48 [0.38-0.58] | n/a | 0.00 [0.00-0.04] | 0.93 [0.81-0.98] | 0.41 [0.32-0.52] |
| mvt-test10 (RGB 720p) | held-out | 12 | 69 | 0.56 [0.44-0.68] | 0.52 [0.41-0.64] | 0.56 [0.44-0.67] | 0.55 [0.43-0.66] | 0.54 [0.39-0.68] | 0.30 [0.21-0.42] | 0.57 [0.42-0.71] | 0.33 [0.23-0.45] |
| meva-uav-0307-1720 (RGB 1080p) | held-out | 12 | 109 | 0.40 [0.33-0.48] | 0.58 [0.48-0.67] | 0.40 [0.34-0.46] | 0.87 [0.80-0.92] | 0.55 [0.46-0.65] | 0.51 [0.42-0.61] | 0.50 [0.43-0.57] | 0.83 [0.75-0.89] |
| hituav-120m-30_3 (thermal) | held-out | 12 | 60 | 0.60 [0.46-0.73] | 0.48 [0.36-0.61] | 0.73 [0.59-0.83] | 0.58 [0.46-0.70] | 1.00 [0.86-1.00] | 0.38 [0.27-0.51] | 0.71 [0.54-0.83] | 0.40 [0.29-0.53] |
| hituav-70m-90_1 (thermal) | held-out | 12 | 70 | 0.70 [0.48-0.85] | 0.20 [0.12-0.31] | 0.74 [0.59-0.85] | 0.41 [0.31-0.53] | 0.00 [0.00-0.79] | 0.00 [0.00-0.05] | 0.78 [0.55-0.91] | 0.20 [0.12-0.31] |

Pooled (sums of TP, FP, FN over the clips of each group):

| Group | Objects | Old P det | Old R det | Improved P det | Improved R det | Old P track | Old R track | Improved P track | Improved R track |
|---|---:|---|---|---|---|---|---|---|---|
| dev (amad-test1, hituav-60m) | 213 | 0.81 [0.69-0.89] | 0.22 [0.17-0.28] | 0.81 [0.75-0.87] | 0.62 [0.55-0.68] | 0.90 [0.75-0.97] | 0.13 [0.09-0.18] | 0.84 [0.77-0.89] | 0.57 [0.50-0.63] |
| held-out RGB (amad-test2, mvt-test10, meva-0307) | 272 | 0.45 [0.39-0.52] | 0.36 [0.31-0.42] | 0.50 [0.44-0.55] | 0.65 [0.60-0.71] | 0.55 [0.47-0.63] | 0.28 [0.23-0.34] | 0.58 [0.52-0.64] | 0.56 [0.50-0.62] |
| held-out thermal (hituav-120m, hituav-70m) | 130 | 0.63 [0.51-0.74] | 0.33 [0.26-0.42] | 0.74 [0.63-0.82] | 0.49 [0.41-0.58] | 0.96 [0.80-0.99] | 0.18 [0.12-0.25] | 0.73 [0.60-0.83] | 0.29 [0.22-0.38] |
| held-out, all five | 402 | 0.49 [0.44-0.55] | 0.35 [0.31-0.40] | 0.54 [0.50-0.59] | 0.60 [0.55-0.65] | 0.61 [0.53-0.68] | 0.25 [0.21-0.29] | 0.61 [0.55-0.66] | 0.48 [0.43-0.52] |
| held-out military vehicles (amad-test2, mvt-test10) | 163 | 0.56 [0.44-0.68] | 0.22 [0.16-0.29] | 0.70 [0.61-0.77] | 0.51 [0.43-0.58] | 0.54 [0.39-0.68] | 0.13 [0.09-0.19] | 0.76 [0.65-0.84] | 0.38 [0.31-0.46] |

- On the held-out clips the improved pipeline finds markedly more of the visible objects (pooled
  recall 0.35 → 0.60 per detection, 0.25 → 0.48 per track) at about the same precision (0.49 →
  0.54 per detection, 0.61 → 0.61 per track).
- The held-out military vehicle clips: recall per track 0.13 → 0.38 and precision 0.54 → 0.76; on
  amad-test2 the old pipeline saw nothing at all.
- Thermal: the RGB-trained VisDrone model is weak on white-hot blobs. MTI carries recall on the
  thermal clips where people walk (the dev thermal clip 0.14 → 0.48 per track), but on the held-out
  nadir clip the walking file moves slowly and closely spaced and the tracker confirms few of them
  (0.20 per track); on hituav-120m the old pipeline had perfect track-level precision on few tracks
  and the improved one trades it (1.00 → 0.71) for a small gain in recall.
- Precision on the held-out MEVA clip stays low (0.40 per detection) because VisDrone boxes roof
  vents, HVAC fans and carport roofs as cars and people; MTI adds almost nothing there (0.13 motion
  boxes per frame): little moves in that clip.
- Residual false movers seen in the audit: the far end of long low-sun shadows (amad-test1), surf on
  the beach (mvt-test10), tree tops and roof edges under a fast-translating camera.

## MinBand on the held-out tracks

`npm run replay` on each clip's improved `tracks.csv`: clean link at θ 0.15 m, and the three link
profiles (telemetry: 450 B/s budget, 5 % loss, 6-tick delay; lora: 1500 B/s, 10 %, 36 ticks; hf:
8000 B/s, 1 %, 60 ticks). Error is the mean over tracked rows, with 2 m charged for a row whose entity
is not yet at the receiver (new tracks under delay). x264 is `h264.sh` on the same pixels (CRF 23
native, and its cheapest row, 360p or native CRF 28). `tools/eval` reports (`eval/summary.md` per
clip) are in `runs/footage/<clip>/eval/`.

| Clip | Entities/frame | x264 CRF 23 native (kbit/s) | x264 lowest row (kbit/s) | MinBand 0.15 m (B/s) | err (cm) | telemetry 450 B/s, 5 % loss: B/s, err (cm) | lora 1500 B/s, 10 %: B/s, err | hf 8000 B/s, 1 %: B/s, err | x264 native / MinBand | x264 lowest / MinBand |
|---|---:|---:|---:|---:|---:|---|---|---|---:|---:|
| amad-test2 (RGB 596x336) | 5.6 | 641 | 308 (native CRF 28) | 758 | 2.8 | 492, 42 | 455, 89 | 815, 100 | 106x | 51x |
| mvt-test10 (RGB 720p) | 7.2 | 3007 | 521 (360p CRF 28) | 995 | 1.9 | 931, 36 | 850, 109 | 1053, 126 | 378x | 65x |
| meva-uav-0307-1720 (RGB 1080p) | 22.9 | 5025 | 341 (360p CRF 28) | 1783 | 3.8 | 776, 44 | 945, 72 | 1092, 75 | 352x | 24x |
| hituav-120m-30_3 (thermal) | 7.4 | 743 | 281 (360p CRF 28) | 588 | 1.5 | 363, 53 | 433, 124 | 716, 107 | 158x | 60x |
| hituav-70m-90_1 (thermal) | 1.8 | 662 | 232 (360p CRF 28) | 213 | 1.6 | 152, 29 | 179, 87 | 254, 78 | 389x | 136x |

The ratio to x264 is set by the scene: 24x the cheapest x264 row on the busy MEVA campus (23
entities), 136x on the sparse nadir thermal clip. Under the link profiles the error is dominated by
delay and by new tracks that are not yet at the receiver, not by bytes.

## Ground scale

No clip has a known camera. The field of view is assumed (85 deg) for all of them; the scale comes
from people's box widths (0.55 m) where there are >= 20 confident person boxes, else vehicles' box
widths (3 m at any heading, +-50 %), with a bootstrap over the boxes.

| Clip | Split | Scale from | Boxes | Pitch (deg) | Height (m) | GSD centre (cm/px) | Bootstrap 90 % GSD (cm/px) | Walker median speed (m/s) |
|---|---|---|---:|---:|---:|---:|---|---:|
| amad-test1 (RGB 596x336) | dev | vehicles | 182 | 51 | 29 | 11.3 | 10.6-11.9 | - |
| hituav-60m-30_1 (thermal) | dev | people (pitch assumed) | 445 | 45 | 8 | 3.4 | 3.2-3.5 | 1.20 |
| amad-test2 (RGB 596x336) | held-out | vehicles | 60 | 11 | 9 | 15.1 | 14.7-15.9 | - |
| mvt-test10 (RGB 720p) | held-out | vehicles | 62 | 69 | 25 | 3.9 | 3.5-4.1 | 1.14 |
| meva-uav-0307-1720 (RGB 1080p) | held-out | people | 317 | 80 | 20 | 1.9 | 1.6-2.6 | 0.80 |
| hituav-120m-30_3 (thermal) | held-out | vehicles | 102 | 66 | 42 | 13.2 | 12.0-15.9 | - |
| hituav-70m-90_1 (thermal) | held-out | people (pitch assumed) | 55 | 45 | 6 | 2.5 | 2.2-3.1 | 0.93 |

- The bootstrap interval covers box noise only. The width assumptions dominate: +-50 % on the
  vehicle fits, and on thermal the RGB model's boxes are looser than the warm blob (about twice its
  width), so the thermal scale may be up to 2x too small.
- The fitted heights are not credible where the field of view is wrong for the camera: 6-8 m on the
  HIT-UAV clips flown at 60-70 m, 9 m at an 11 deg pitch on amad-test2. The scale at the objects
  (GSD) is what the tracker uses, and it is constrained by the boxes, not by the height.
- Independent checks where there are walkers: 0.8-1.2 m/s median walking speed (people walk at
  1.3-1.4 m/s; groups and the audit's slow file walk slower), so the scale is plausible to within
  roughly a factor of 1.5.
- θ is in metres, so the scale error moves MinBand's bytes: a 2x scale error is roughly a 2x θ error.

## Limits

- Five held-out clips, 6-35 s each, two of them from one stock source; 12 audited frames per clip;
  one auditor. The intervals are wide and the audit is a developer's reading, not ground truth.
- The appearance model is a nano VisDrone model; thermal is out of its training distribution.
- MTI needs motion: a parked or camouflaged-and-still object is invisible to it; slow, closely spaced
  walkers (the nadir thermal file) merge or fall under the threshold.
- Shadows, surf and the parallax of tall structures under a fast camera leave false movers.
- Fragmentation is still high on busy scenes (MEVA 4K: ~100 births per minute for ~80 objects).
- The ground model is a flat plane with an assumed field of view.
- MinBand numbers are replays at a fixed θ or a fixed link profile, not a field link.

## Unfinished

- **MEVA 4K with the fixed detector.** The re-detection of MEVA 2018-03-13.16-00-14 (15-105 s) with
  the NMS fix was killed by a timeout at frame 2832 of 3150, so the MEVA 4K tracker table above uses
  the old-NMS detections, and there is no "improved" MEVA 4K row with the fixed detector. It is not
  needed for the held-out conclusions (the held-out 1080p MEVA clip ran the full pipeline). To finish
  locally (~45 min on 4 cores):

  ```bash
  cd tools/footage
  .venv/bin/python -I track.py detect clips/2018-03-13.16-00-14.16-03-38.uav1.mp4 --model models/aerial-guardian.onnx \
      --tile 640 --start 15 --end 105 --out ../../runs/footage/meva-2018-03-13.16-00-14
  .venv/bin/python -I track.py mti clips/2018-03-13.16-00-14.16-03-38.uav1.mp4 --out ../../runs/footage/meva-2018-03-13.16-00-14
  .venv/bin/python -I track.py track ../../runs/footage/meva-2018-03-13.16-00-14 --sources det,mti
  .venv/bin/python -I track.py track ../../runs/footage/meva-2018-03-13.16-00-14 --sources det --legacy-tracker --no-overlay \
      --out ../../runs/footage/meva-2018-03-13.16-00-14/det-legacy
  .venv/bin/python -I audit.py metrics ../../runs/footage/meva-2018-03-13.16-00-14 ../../runs/footage/meva-2018-03-13.16-00-14/det-legacy
  ./minband.sh ../../runs/footage/meva-2018-03-13.16-00-14/tracks.csv ../../runs/footage/meva-2018-03-13.16-00-14
  ```

- **Visual audit on MEVA 4K** (the tuning clip) was not done; the label-free metrics were. To do it:
  `audit.py sample <run> <clip> --old <run>/old --out <run>/audit --tile-width 1280`, label
  `<run>/audit/labels.json` (format in `audit.py`), `audit.py score <run>/audit`.
- The per-clip audit labels (`runs/footage/<clip>/audit/labels.json`) are local, like the frames they
  refer to; the numbers are in this file.

## Reproducing

From a fresh checkout (clips and weights are never committed; the battlefield clip URLs and licences
are in the manifest kept with the clips, summarised here):

```bash
cd tools/footage
python3 -m venv .venv && .venv/bin/pip install onnxruntime opencv-python-headless numpy scipy
mkdir -p models clips/battlefield
# Appearance model: the VisDrone-trained YOLO26n-P2 ONNX (end2end 300x6, 960 input, AGPL-3.0) as
# models/aerial-guardian.onnx; it is not redistributed here.
# MEVA (CC-BY-4.0): the tuning clip, and the held-out clip by byte range from the public drop:
.venv/bin/python meva.py fetch 2018-03-13.16-00-14 clips/
curl -sS -r 11876031488-11928476543 -o clips/battlefield/meva-uav-0307-1720.mp4 \
    https://s3.amazonaws.com/mevadata-public-01/uav-drop-01/meva-uav-drop-01.tar   # range of tar member 2018-03-07.17-20-30.17-21-05.uav1.mp4 as recorded in the clip manifest; the held-out run used it downscaled to 1920x1080
# HIT-UAV thermal samples (CC-BY-4.0):
for c in 60m-30_1 120m-30_3 70m-90_1; do curl -sSL -o clips/battlefield/hituav-$c.mov \
    https://raw.githubusercontent.com/suojiashun/HIT-UAV-Infrared-Thermal-Dataset/main/video_sample/$c.mov; done
# Military clips (no licence for reuse: local evaluation only, never commit frames):
curl -sSL -o clips/battlefield/amad-test1.mp4 "https://media.githubusercontent.com/media/InvictusRex/Drone-Based-Reconnaissance-of-Military-Assets/legacy-ml-only-implementation/Testing%20Videos/Test1.mp4"
curl -sSL -o clips/battlefield/amad-test2.mp4 "https://media.githubusercontent.com/media/InvictusRex/Drone-Based-Reconnaissance-of-Military-Assets/legacy-ml-only-implementation/Testing%20Videos/Test2.mp4"
curl -sSL -o clips/battlefield/mvt-test10.mp4 https://github.com/Lin-Sinorodin/Military_Vehicles_Tracking/releases/download/v1.0.0/test10.mp4
# (the evaluation transcoded the .mov / .gif sources to .mp4 with ffmpeg first; same frames)
cd ../../core && wasm-pack build --target nodejs --out-dir pkg-node --release -- --features wasm
cd ../tools/eval && npm ci && cd ../footage
# Each clip: frozen pipeline + old pipeline + ablations + x264 + eval + replays (run_clip.sh header)
./run_clip.sh clips/battlefield/amad-test1.mp4 ../../runs/footage/dev-amad-test1
for c in hituav-60m-30_1 amad-test2 mvt-test10 meva-uav-0307-1720 hituav-120m-30_3 hituav-70m-90_1; do
  ./run_clip.sh clips/battlefield/$c.mp4 ../../runs/footage/$c; done
# Visual audit (after freezing): sample, look, write labels.json, score
.venv/bin/python -I audit.py sample ../../runs/footage/<clip> clips/battlefield/<clip>.mp4 \
    --old ../../runs/footage/<clip>/old --out ../../runs/footage/<clip>/audit
.venv/bin/python -I audit.py score ../../runs/footage/<clip>/audit
# Tables of this file
for t in sources label_free audit minband ground; do .venv/bin/python -I tables.py $t; done
.venv/bin/python -I pooled.py
```

## Viewer class names (for viewer/src/scene.ts, not changed here)

`CLASS_NAME` needs: 1 bicycle, 2 car, 3 motorcycle, 5 bus, 7 truck, 100 mover, 101 armoured.
`kindOf` should draw 1, 2, 3, 5, 7, 101 as a vehicle (or at least not as a carried object) and 100 as
an unclassified mover.
