# Footage findings: detection and tracking on battlefield-like drone footage

Status: 2026-10-10 (round 2 added the same day). Scope: detection and tracking for situational awareness with a human in the
loop (MinBand's edge). Affiliation is never assessed. Nothing here is built or wired for targeting.

What was measured: the footage pipeline in `tools/footage` (README there for the method) before and
after the changes of this round, on drone clips it was never tuned on, without labels. The numbers
come from `track.py` / `audit.py` runs in `runs/footage/` (not committed).

Two rounds. Round 1 (detector, MTI, fusion, tracker; cloud box, x86) is the "Round 1" sections.
Round 2 (tracker churn and false movers, and the MEVA 4K re-detection; a local Mac) is the next
section.

## Round 2: tracker churn and false movers (2026-10-10)

### How it was run

- Machine: Apple M4 (arm64), ONNX Runtime 1.31 on the CPU (no GPU used), OpenCV 5.0, Python 3.14.
  Every row in this section, round 1 included, was rerun on it from the same caches, so compare rows
  within this section. Against the round-1 tables below (x86), the appearance detector gives slightly
  different boxes at the 0.15 confidence edge: mvt-test10 4.58 boxes per frame here, 4.97 there
  (OpenCV 4.14 and 5.0 give identical boxes here, so it is the inference numerics, not the decoder or
  resize). Same direction and size everywhere; not bit-identical.
- Tuning: only the MEVA 4K tuning clip (2018-03-13.16-00-14, 15-105 s, re-detected with the fixed
  detector, below) and the dev clips amad-test1 and hituav-60m-30_1. Parameters frozen at `a90e244`;
  each held-out clip then run once (`run_clip.sh`), nothing changed after. The held-out clips had been
  run once before, in round 1; none of the round-2 changes was tried on them before freezing.
- No dev clip has surf (it is only in the held-out mvt-test10), so nothing was tuned for surf. On this
  machine no motion-only track on surf was reported in either round.
- Round 1 is `round1.py` (track.py as frozen at `d59938b`, read from git) on the same detection caches;
  round 2 is `track.py` at `a90e244`.

### What changed (tracker only; detector, MTI and fusion unchanged)

1. **Companions by size, not distance.** A motion-only track in lockstep (offset std <= 0.6 m over
   1.5 s) with a confirmed track at most half its size (box diagonals on the ground) within 20 m is
   not reported: the tip of a low-sun shadow sits 10-15 m behind its vehicle, beyond round 1's 8 m.
   Round 1 hid any lockstep motion-only track within 8 m, whatever its size, which also hid walkers
   in groups and convoy vehicles behind a smaller blob. Held back about 1 s while the test cannot yet
   decide; a found companion stays hidden 1.5 s.
2. **Parallax.** The camera centre at every frame comes from the plane-to-image homography. A tall
   static object's top slides over the ground plane against the camera's motion at h / (H - h) of its
   speed (dev: tree tops at 0.18-0.24 of the camera's 3.5 m/s at 30 m, i.e. 5-10 m trees). A
   motion-only track that keeps within cos 0.5 of that direction, below 0.6 of the camera's speed, over
   1.5 s, is not reported; only while the camera moves faster than 1 m/s.
3. **Re-acquisition where an object stopped.** A track older than 5 s stays re-acquirable for 20 s at
   the place it was last seen, with half the base gate. On MEVA 4K most remaining re-births were
   walkers who stopped (waiting at the bus station) and were found 8-12 s later within 1 m of where
   they were lost, while the Kalman prediction had carried their lost track 5 m on.
4. **Static coasting 4 s** (reported) for parked objects the detector misses for a few seconds.
5. **Riders.** A dismount track and a bicycle or motorcycle detection associate (VisDrone boxes a rider
   either way; a quarter of the re-births on MEVA 4K were such label flips).

### MEVA 4K with the fixed detector

The re-detection that round 1 could not finish (killed at 88 % by a time limit): 450 detection frames,
15-105 s, 60-95 boxes per frame, 16 minutes on this machine. Rows from the label-free table below:

| Pipeline | Tracks | Births/min | Median track (s) | Entities/frame | Static: n, per-object wander (m RMS) | MinBand B/s at 0.15 m | per entity |
|---|---:|---:|---:|---:|---|---:|---:|
| old (old detector, original tracker) | 286 | 191 | 4.2 | 27.2 | 46, 1.01 | 2317 | 85 |
| new detector, original tracker | 329 | 219 | 8.2 | 65.4 | 46, 0.64 | 5440 | 83 |
| round 1 (det + MTI) | 247 | 165 | 14.0 | 75.8 | 48, 0.74 | 5910 | 78 |
| round 2 (det + MTI) | 206 | 137 | 22.9 | 79.2 | 35, 0.43 | 6008 | 76 |

The fixed detector finds about three times the objects of the old one (27 → 79 entities per frame);
round 2 tracks them with fewer tracks than the old pipeline tracked a third of them (286 → 206), a
median track of 23 s instead of 4 s, and less wander on parked objects. Bytes follow the entity count;
per entity they fall (85 → 76 B/s). The visual audit on MEVA 4K was not done (see Unfinished).

### Label-free metrics, all clips (this machine)

Columns as in the round-1 table below. "new detector + round-2 tracker, no MTI" is the ablation with
the round-2 tracker (in round 1 the same row used the round-1 tracker).

| Clip | Split | Pipeline | Tracks | Births/min | Median track (s) | Entities/frame | Dets in a >=1 s track | Motion-only/frame | Static: n, std (m), KF speed (m/s) | MinBand B/s at 0.15 / 0.5 m | Mean error (cm) at 0.15 / 0.5 m |
|---|---|---|---:|---:|---:|---:|---:|---:|---|---|---|
| MEVA 16-00-14 (4K) | tuning | old | 286 | 191 | 4.2 | 27.2 | 0.75 | - | 46, 0.23, 0.10 | 2317 / 1506 | 3.5 / 11.0 |
| MEVA 16-00-14 (4K) | tuning | new detector, old tracker | 329 | 219 | 8.2 | 65.4 | 0.81 | - | 46, 0.18, 0.10 | 5440 / 3349 | 3.7 / 12.6 |
| MEVA 16-00-14 (4K) | tuning | new detector + round-2 tracker, no MTI | 185 | 123 | 25.8 | 76.8 | 0.86 | - | 33, 0.16, 0.00 | 5753 / 3540 | 3.8 / 10.9 |
| MEVA 16-00-14 (4K) | tuning | round 1 (det + MTI) | 247 | 165 | 14.0 | 75.8 | 0.85 | 1.0 | 48, 0.16, 0.00 | 5910 / 3630 | 3.8 / 11.0 |
| MEVA 16-00-14 (4K) | tuning | round 2 (det + MTI) | 206 | 137 | 22.9 | 79.2 | 0.86 | 1.0 | 35, 0.18, 0.00 | 6008 / 3681 | 3.7 / 10.8 |
| amad-test1 (RGB 596x336) | dev | old | 2 | 8 | 5.8 | 1.0 | 0.60 | - | - | 142 / 75 | 3.1 / 16.9 |
| amad-test1 (RGB 596x336) | dev | new detector, old tracker | 4 | 15 | 13.7 | 3.2 | 0.90 | - | - | 426 / 213 | 4.5 / 14.5 |
| amad-test1 (RGB 596x336) | dev | new detector + round-2 tracker, no MTI | 4 | 15 | 10.7 | 2.9 | 0.88 | - | - | 361 / 172 | 4.7 / 14.0 |
| amad-test1 (RGB 596x336) | dev | round 1 (det + MTI) | 13 | 49 | 5.6 | 6.5 | 0.84 | 3.0 | - | 746 / 513 | 3.3 / 10.7 |
| amad-test1 (RGB 596x336) | dev | round 2 (det + MTI) | 12 | 46 | 4.2 | 5.3 | 0.81 | 3.0 | - | 666 / 447 | 3.5 / 12.2 |
| hituav-60m-30_1 (thermal) | dev | old | 53 | 23 | 4.3 | 2.6 | 0.52 | - | 3, 0.60, 0.24 | 284 / 224 | 2.2 / 8.0 |
| hituav-60m-30_1 (thermal) | dev | new detector, old tracker | 68 | 29 | 4.8 | 3.4 | 0.49 | - | 3, 0.42, 0.30 | 274 / 207 | 2.7 / 10.6 |
| hituav-60m-30_1 (thermal) | dev | new detector + round-2 tracker, no MTI | 40 | 17 | 11.0 | 4.4 | 0.62 | - | 1, 0.35, 0.00 | 404 / 273 | 3.1 / 11.3 |
| hituav-60m-30_1 (thermal) | dev | round 1 (det + MTI) | 104 | 45 | 9.2 | 9.8 | 0.85 | 3.7 | - | 879 / 633 | 2.5 / 10.6 |
| hituav-60m-30_1 (thermal) | dev | round 2 (det + MTI) | 91 | 39 | 11.5 | 10.4 | 0.88 | 3.7 | 2, 0.37, 0.00 | 945 / 667 | 2.8 / 11.4 |
| amad-test2 (RGB 596x336) | held-out | old | 0 | - | - | - | 0.00 | - | - | 0 / 0 | 0.0 / 0.0 |
| amad-test2 (RGB 596x336) | held-out | new detector, old tracker | 4 | 21 | 7.2 | 2.6 | 0.65 | - | - | 285 / 200 | 3.9 / 9.2 |
| amad-test2 (RGB 596x336) | held-out | new detector + round-2 tracker, no MTI | 4 | 21 | 6.6 | 2.7 | 0.66 | - | - | 283 / 202 | 3.9 / 9.0 |
| amad-test2 (RGB 596x336) | held-out | round 1 (det + MTI) | 11 | 58 | 4.8 | 5.0 | 0.81 | 1.5 | - | 709 / 558 | 2.4 / 8.8 |
| amad-test2 (RGB 596x336) | held-out | round 2 (det + MTI) | 10 | 53 | 3.8 | 4.6 | 0.81 | 1.5 | - | 672 / 533 | 2.5 / 8.6 |
| mvt-test10 (RGB 720p) | held-out | old | 9 | 87 | 2.4 | 4.4 | 0.69 | - | - | 622 / 496 | 2.3 / 11.1 |
| mvt-test10 (RGB 720p) | held-out | new detector, old tracker | 10 | 97 | 2.3 | 4.5 | 0.61 | - | - | 654 / 529 | 2.2 / 11.1 |
| mvt-test10 (RGB 720p) | held-out | new detector + round-2 tracker, no MTI | 4 | 39 | 5.0 | 3.7 | 0.56 | - | - | 529 / 387 | 2.0 / 12.0 |
| mvt-test10 (RGB 720p) | held-out | round 1 (det + MTI) | 13 | 126 | 3.0 | 7.5 | 0.63 | 0.5 | - | 860 / 615 | 2.5 / 7.9 |
| mvt-test10 (RGB 720p) | held-out | round 2 (det + MTI) | 10 | 97 | 2.0 | 5.6 | 0.61 | 0.5 | - | 713 / 542 | 2.8 / 11.0 |
| meva-uav-0307-1720 (RGB 1080p) | held-out | old | 68 | 116 | 3.8 | 10.9 | 0.65 | - | 6, 0.50, 0.22 | 1093 / 847 | 2.5 / 9.6 |
| meva-uav-0307-1720 (RGB 1080p) | held-out | new detector, old tracker | 88 | 150 | 4.2 | 17.4 | 0.71 | - | 5, 0.22, 0.07 | 1375 / 1002 | 3.2 / 9.8 |
| meva-uav-0307-1720 (RGB 1080p) | held-out | new detector + round-2 tracker, no MTI | 73 | 125 | 9.1 | 22.1 | 0.76 | - | 10, 0.34, 0.00 | 1872 / 1132 | 3.9 / 10.1 |
| meva-uav-0307-1720 (RGB 1080p) | held-out | round 1 (det + MTI) | 79 | 135 | 7.0 | 20.6 | 0.75 | 0.1 | 8, 0.18, 0.00 | 1665 / 1073 | 3.7 / 9.7 |
| meva-uav-0307-1720 (RGB 1080p) | held-out | round 2 (det + MTI) | 77 | 132 | 8.2 | 22.6 | 0.76 | 0.1 | 10, 0.34, 0.00 | 1914 / 1166 | 3.9 / 10.0 |
| hituav-120m-30_3 (thermal) | held-out | old | 15 | 31 | 2.4 | 2.6 | 0.46 | - | 1, 0.28, 0.23 | 342 / 260 | 1.8 / 9.8 |
| hituav-120m-30_3 (thermal) | held-out | new detector, old tracker | 10 | 21 | 4.0 | 2.1 | 0.46 | - | 1, 0.23, 0.41 | 276 / 199 | 1.8 / 10.4 |
| hituav-120m-30_3 (thermal) | held-out | new detector + round-2 tracker, no MTI | 7 | 15 | 6.4 | 2.5 | 0.50 | - | - | 305 / 201 | 3.1 / 11.6 |
| hituav-120m-30_3 (thermal) | held-out | round 1 (det + MTI) | 44 | 92 | 3.3 | 7.3 | 0.69 | 1.1 | - | 587 / 484 | 1.5 / 5.6 |
| hituav-120m-30_3 (thermal) | held-out | round 2 (det + MTI) | 42 | 88 | 2.9 | 6.7 | 0.68 | 1.1 | - | 582 / 476 | 1.8 / 6.4 |
| hituav-70m-90_1 (thermal) | held-out | old | 3 | 5 | 8.8 | 1.0 | 0.04 | - | - | 54 / 45 | 1.4 / 6.2 |
| hituav-70m-90_1 (thermal) | held-out | new detector, old tracker | 4 | 7 | 19.1 | 2.2 | 0.28 | - | - | 139 / 122 | 1.9 / 8.7 |
| hituav-70m-90_1 (thermal) | held-out | new detector + round-2 tracker, no MTI | 4 | 7 | 29.2 | 2.9 | 0.33 | - | - | 154 / 132 | 2.0 / 8.2 |
| hituav-70m-90_1 (thermal) | held-out | round 1 (det + MTI) | 9 | 16 | 4.1 | 2.7 | 0.39 | 0.2 | - | 211 / 172 | 1.6 / 6.6 |
| hituav-70m-90_1 (thermal) | held-out | round 2 (det + MTI) | 9 | 16 | 4.1 | 2.7 | 0.39 | 0.2 | - | 211 / 173 | 1.7 / 7.2 |

### Motion-only tracks: real or not (post-hoc, developer, after freezing; not ground truth)

`audit.py movers`: one crop per reported track whose majority class is the unclassified mover (100),
at the detection nearest the middle of its reported span, the object and 4x its surroundings; the
developer labels each real (a moving dismount or vehicle), false (shadow, parallax, vegetation, an
edge, nothing visible) or unsure. Tracks (reported seconds):

| Clip | Split | Round 1: real | Round 1: false | Round 1: unsure | Round 2: real | Round 2: false | Round 2: unsure |
|---|---|---|---|---|---|---|---|
| MEVA 16-00-14 (4K) | tuning | 0 (0 s) | 18 (85 s) | 0 (0 s) | 0 (0 s) | 13 (81 s) | 0 (0 s) |
| amad-test1 (RGB 596x336) | dev | 1 (5 s) | 7 (30 s) | 0 (0 s) | 1 (9 s) | 6 (9 s) | 0 (0 s) |
| hituav-60m-30_1 (thermal) | dev | 18 (111 s) | 2 (6 s) | 4 (15 s) | 18 (192 s) | 2 (6 s) | 4 (16 s) |
| amad-test2 (RGB 596x336) | held-out | 3 (13 s) | 1 (0 s) | 0 (0 s) | 2 (8 s) | 0 (0 s) | 0 (0 s) |
| mvt-test10 (RGB 720p) | held-out | 0 (0 s) | 4 (11 s) | 1 (2 s) | 0 (0 s) | 2 (4 s) | 0 (0 s) |
| meva-uav-0307-1720 (RGB 1080p) | held-out | 0 (0 s) | 0 (0 s) | 0 (0 s) | 0 (0 s) | 0 (0 s) | 0 (0 s) |
| hituav-120m-30_3 (thermal) | held-out | 2 (11 s) | 19 (50 s) | 9 (21 s) | 2 (9 s) | 16 (29 s) | 9 (19 s) |
| hituav-70m-90_1 (thermal) | held-out | 5 (11 s) | 0 (0 s) | 0 (0 s) | 5 (11 s) | 0 (0 s) | 0 (0 s) |
| held-out, all | | 10 (35 s) | 24 (61 s) | 10 (23 s) | 9 (28 s) | 18 (32 s) | 9 (19 s) |

What the false ones were: amad-test1 (dev) shadow tips of the convoy behind a low sun (4 tracks),
tree tops sliding with the drone's motion (2), a fragment on a truck; MEVA 4K tree tops and bushes in
wind, lamp posts, curbs, bike racks, roof edges and roof units under a drone hovering at ~80 m;
mvt-test10 a headland tip on the horizon, a tree top against the sky, footprints in sand, a second box
on an amphibious vehicle; hituav-120m blobs with nothing visible at the box at 120 m (people are 2-5 px
there, so 9 tracks stay unsure). amad-test2's one real track lost in round 2 is the same convoy vehicle:
its motion blob is half the size of the vehicle ahead, in lockstep 14 m away, so it was held back as a
companion for about 1.8 s, then classified as a car by appearance (no longer a motion-only track).

### Reading it

- **Churn, tuning clip.** MEVA 4K: 247 → 206 tracks (-17 %), 165 → 137 births per minute, median
  track 14.0 → 22.9 s, per-object wander of parked objects 0.74 → 0.43 m. Against the original
  pipeline on the same clip: 286 tracks for 27 entities per frame then, 206 for 79 now.
- **Churn, held-out.** Small: 156 → 148 tracks over the five clips; the largest change is mvt-test10
  (13 → 10 tracks, 126 → 97 births per minute). The short held-out clips (6-35 s) have little room
  for the 20 s re-acquisition and the static coasting to act.
- **False movers.** Held-out: 24 → 18 false motion-only tracks, 61 → 32 s of them on the link (-48 %),
  for 35 → 28 s of real ones (the amad-test2 vehicle above; about 2 s less on one hituav-120m walker). Dev
  amad-test1: 30 → 9 s false with the real vehicle 5 → 9 s; dev thermal: real 111 → 192 s with false
  unchanged (6 s), because round 1's 8 m rule had hidden walkers in groups.
- **What it does not fix.** A hovering drone over trees in wind and high-contrast roof edges (MEVA 4K:
  13 false tracks, 81 s, left): the motion is not parallax, and neither net displacement nor
  straightness over 3 s separates it from real walkers on these clips. Surf was not tuned for.
- **Bytes** follow the entities kept. mvt-test10 860 → 713 B/s (fewer false movers), amad-test2
  709 → 672; the held-out MEVA clip 1665 → 1914 B/s, because static coasting and the long
  re-acquisition keep parked objects in the picture (20.6 → 22.6 entities per frame).

### MinBand on the held-out tracks (round 2)

As the round-1 table below, from the round-2 `tracks.csv`, x264 on this machine's ffmpeg.

| Clip | Entities/frame | x264 CRF 23 native (kbit/s) | x264 lowest row (kbit/s) | MinBand 0.15 m (B/s) | err (cm) | telemetry 450 B/s, 5 % loss: B/s, err (cm) | lora 1500 B/s, 10 %: B/s, err | hf 8000 B/s, 1 %: B/s, err | x264 native / MinBand | x264 lowest / MinBand |
|---|---:|---:|---:|---:|---:|---|---|---|---:|---:|
| amad-test2 (RGB 596x336) | 4.6 | 648 | 313 (native CRF 28) | 672 | 2.5 | 529, 37 | 497, 85 | 776, 92 | 121x | 58x |
| mvt-test10 (RGB 720p) | 5.6 | 3064 | 527 (360p CRF 28) | 713 | 2.8 | 638, 32 | 597, 82 | 847, 114 | 537x | 92x |
| meva-uav-0307-1720 (RGB 1080p) | 22.6 | 5169 | 351 (360p CRF 28) | 1914 | 3.9 | 828, 48 | 1005, 65 | 1143, 69 | 338x | 23x |
| hituav-120m-30_3 (thermal) | 6.7 | 796 | 309 (360p CRF 28) | 582 | 1.8 | 365, 57 | 397, 108 | 698, 103 | 171x | 66x |
| hituav-70m-90_1 (thermal) | 1.7 | 713 | 257 (360p CRF 28) | 211 | 1.7 | 151, 32 | 169, 78 | 249, 72 | 423x | 152x |

### Bugs found on the way

- `tools/eval` replay: `Math.max(...errs)` overflowed the call stack on MEVA 4K (~211 000 rows), and
  `minband.sh` hid the error and wrote empty results, so no MinBand numbers existed for the 4K clip
  with the fixed detector. Fixed (a loop; a test with 500 000 values); `minband.sh` now fails loudly.
- `h264.sh` used GNU `stat -c`; on macOS every x264 baseline failed. Fixed (`wc -c`).

## Round 1: protocol

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

## Round 1: what changed

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

## Round 1: label-free metrics

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

On the detector cache with the old NMS (the fixed-NMS re-detection of this clip did not finish in
round 1; it is in round 2 above). Same detections in every row; only the tracker (and MTI) change.

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

## Round 1: visual audit (post-hoc, developer, after freezing; not ground truth)

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

## Round 1: MinBand on the held-out tracks

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
- Shadows, surf and the parallax of tall structures under a fast camera leave false movers (round 2
  removes about half of the false-mover time on the held-out clips; trees in wind and roof edges
  under a hovering drone remain, and surf was not tuned for).
- Fragmentation is still high on busy scenes (MEVA 4K with the fixed detector: 137 births per minute
  for 79 entities per frame in round 2, 165 in round 1).
- The ground model is a flat plane with an assumed field of view.
- MinBand numbers are replays at a fixed θ or a fixed link profile, not a field link.

## Unfinished

- **MEVA 4K with the fixed detector**: done in round 2 (above), 16 minutes on an M4.
- **Visual audit on MEVA 4K** (the tuning clip, frame level) was not done; the label-free metrics and
  the motion-only track audit were. To do it: `audit.py sample <run> <clip> --old <run>/old --out
  <run>/audit --tile-width 1280`, label `<run>/audit/labels.json` (format in `audit.py`), `audit.py
  score <run>/audit`.
- **Frame-level audit of round 2.** The precision / recall tables (round 1) were not redone for the
  round-2 tracker; round 2 was audited only at the motion-only track level.
- **Surf.** Only the held-out mvt-test10 has it, so no round-2 change was tuned for it. A licensed dev
  clip with surf would allow it.
- **Hovering-drone clutter** (trees in wind, roof edges, roof units on MEVA 4K): needs an appearance
  cue (vegetation texture, a roof mask) or a longer motion test; not attempted.
- The per-clip audit labels (`runs/footage/<clip>/audit/labels.json`, `runs/footage/<clip>/movers/labels.json`)
  are local, like the frames they refer to; the numbers are in this file.

## Reproducing

From a fresh checkout (clips and weights are never committed; the battlefield clips' URLs, sha256,
licences and splits are in `tools/footage/battlefield-clips.json`):

```bash
cd tools/footage
python3 -m venv .venv && .venv/bin/pip install onnxruntime opencv-python-headless numpy scipy
mkdir -p models clips
# Appearance model: the VisDrone-trained YOLO26n-P2 ONNX (end2end 300x6, 960 input, AGPL-3.0) from
# github.com/Halok600/The-Aerial-Guardian (web/model/aerial-guardian.onnx); never committed here.
curl -fsSL -o models/aerial-guardian.onnx https://raw.githubusercontent.com/Halok600/The-Aerial-Guardian/main/web/model/aerial-guardian.onnx
# MEVA (CC-BY-4.0) tuning clip:
.venv/bin/python meva.py fetch 2018-03-13.16-00-14 clips/
# The eight battlefield clips: download, sha256 check, and the same normalisation as the evaluation
# copies (remux; GIF -> H.264; MEVA held-out 4K -> 1920 wide). Military clips have no licence for
# reuse: local evaluation only, never commit frames.
./battlefield.sh clips/battlefield
cd ../../core && wasm-pack build --target nodejs --out-dir pkg-node --release -- --features wasm
cd ../tools/eval && npm ci && cd ../footage
# Each clip: frozen pipeline (round 2) + round 1 + old pipeline + ablations + x264 + eval + replays
# (run_clip.sh header; detection caches that exist are reused, FORCE=1 redoes them)
./run_clip.sh clips/battlefield/amad-test1.mp4 ../../runs/footage/dev-amad-test1
for c in hituav-60m-30_1 amad-test2 mvt-test10 meva-uav-0307-1720 hituav-120m-30_3 hituav-70m-90_1; do
  ./run_clip.sh clips/battlefield/$c.mp4 ../../runs/footage/$c; done
# MEVA 4K tuning clip, 15-105 s (the detection is the slow part: ~16 min on an M4, ~1 h on 4 x86 cores)
START=15 END=105 ./run_clip.sh clips/2018-03-13.16-00-14.16-03-38.uav1.mp4 ../../runs/footage/meva-2018-03-13.16-00-14-bf
# Motion-only track audit (round 2 section): crops, label, score
.venv/bin/python -I audit.py movers clips/battlefield/<clip>.mp4 ../../runs/footage/<clip>/round1 ../../runs/footage/<clip> \
    --out ../../runs/footage/<clip>/movers
.venv/bin/python -I audit.py movers-score ../../runs/footage/<clip>/movers
# Visual audit (after freezing): sample, look, write labels.json, score
.venv/bin/python -I audit.py sample ../../runs/footage/<clip> clips/battlefield/<clip>.mp4 \
    --old ../../runs/footage/<clip>/old --out ../../runs/footage/<clip>/audit
.venv/bin/python -I audit.py score ../../runs/footage/<clip>/audit
# Tables of this file
for t in sources label_free audit minband ground movers; do .venv/bin/python -I tables.py $t; done
.venv/bin/python -I pooled.py
```

## Viewer class names

Done in `viewer/src/scene.ts` after the merge: 1 bicycle, 2 car, 3 motorcycle, 5 bus, 7 truck as
vehicles and two-wheelers, 101 armoured as a vehicle, 100 mover as the small heading box (its size
is unknown).
