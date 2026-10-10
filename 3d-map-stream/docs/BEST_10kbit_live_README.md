# Best 10 kbit/s LIVE map stream — `stream_best.py`

A drone films a suburb, MASt3R-SLAM reconstructs a coloured point cloud (`houses_7fps.ply`, 522k points, 1 SLAM unit ~ 40 m),
and the map is streamed to the operator over a **10 kbit/s** radio link: one chunk per keyframe (5 keyframes, ~6 s apart),
each chunk must fit its slot (1250 B/s x slot length ~ 7 KB), causal (only poses 0..k known at chunk k).
The whole area is visible from keyframe 0, so the stream sends the area **coarse first, then refines near the drone**.

## What to look for

* `flythrough_live_growing.mp4` — the operator's view: at time t the map contains only the chunks that have **fully arrived**
  (chunk k goes on air at keyframe time t_k, 1250 B/s, back to back if the previous chunk is still sending). Overlay: slot shown,
  bytes received so far, progress bar of the chunk in flight. The first 5.8 s are empty sky (chunk 0 still in flight),
  then the whole suburb pops in at 1-4 m voxels; from ~17.8 s on (chunk 2 received) the area in front of the drone is 1 m voxels.
  Chunk 4 only lands at 28.9 s, i.e. after the 23 s flight (the link is saturated the whole time).
* `flythrough_final.mp4` — the same camera path over the final decoded map (`../tools/fly_higher.py final.ply --raise-m 0 --extend 5 --single --voxel 0.0125`).
* `compare.png` — reference / baseline / chosen, left: live view slot 2, right: final standard view 4. Look at the roofs, road and
  tree crowns: both streamers are crisp blocky voxels (no blur); the chosen one has slightly fewer holes and more 1 m voxels in the
  foreground because its coder spends ~15% fewer bits per voxel.
* `map_after_slot{k}.ply` — the decoded map after each slot (open in MeshLab/CloudCompare; `final.ply` = after slot 4,
  `final.txt` = trajectory, `final.splat` = point size in SLAM units, 0.0125 = 0.5 m).
* `wire/chunk00..04.bin` — the actual bytes; `wire/manifest.json` — sizes, budgets, encode/decode times.

## Benchmark (`research/live_eval.py`, 10 kbit/s)

```
stream_best.py:     35.9 KB in 5 slots at 10 kbit/s (all slots within budget) | max enc 1.8s | LIVE PSNR 18.01 SSIM 0.542 holes 1.1% | FINAL PSNR 19.03 SSIM 0.636 holes 0.9% | 304,705 pts
stream_lod_conf.py: 35.1 KB in 5 slots at 10 kbit/s (all slots within budget) | max enc 9.3s | LIVE PSNR 17.83 SSIM 0.526 holes 1.4% | FINAL PSNR 18.82 SSIM 0.619 holes 1.1% | 301,921 pts   (baseline)
```
Run-to-run noise is ~±0.1 dB (k-means palette). The gain is +0.2 dB live / +0.2 dB final, fewer holes, and 5x faster encoding.

Per slot (chosen `stream_best.py`; baseline live PSNR in the last column):

| slot | t_k (s) | slot len (s) | budget (B) | bytes | use | on air (s) | enc (s) | dec (s) | pts after | live PSNR | baseline live PSNR |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 0 | 0.0  | 5.86 | 7321 | 7245 | 99.0% | 0.0-5.8   | 1.3 | 0.10 | 64,407  | 16.56 | 16.31 |
| 1 | 5.9  | 5.86 | 7321 | 7228 | 98.7% | 5.9-11.6  | 1.2 | 0.11 | 69,815  | 17.52 | 17.42 |
| 2 | 11.7 | 6.14 | 7679 | 7593 | 98.9% | 11.7-17.8 | 1.8 | 0.21 | 294,066 | 18.73 | 18.50 |
| 3 | 17.9 | 5.29 | 6607 | 6529 | 98.8% | 17.9-23.1 | 1.4 | 0.21 | 300,906 | 18.86 | 18.61 |
| 4 | 23.1 | 5.86 | 7321 | 7256 | 99.1% | 23.1-28.9 | 1.2 | 0.24 | 304,705 | 18.38 | 18.30 |
| total | | | 36,250 | 35,851 | | | | | | 18.01 | 17.83 |

"pts after" is the densified decoded map (coarse voxels are expanded to 0.5 m points for rendering); the on-air window is
start = max(t_k, previous chunk done), end = start + bytes / 1250 B/s.

## How it works (`research/stream_best.py`)

* **LOD voxel bands** (0.5 / 1 / 2 / 4 m) on a grid fixed from keyframe 0 (world frame + origin sent once, 48 B). Each slot codes
  an octree per band; the first chunk covers the whole area at 2-4 m, later chunks refine to 1 m (rarely 0.5 m) around the drone's
  current position; voxel selection prunes low-confidence (sparse) cells (CONF density pruning, the one selection that helped).
* **One octree range-coder stream per chunk** (constriction), occupancy coded level by level with an adaptive context model whose
  tables are **carried across chunks** on both sides (no model restart cost), plus a **known-voxel context**: every cell knows whether
  the receiver already holds a coarser voxel there (refinements are almost always inside received coverage, cells outside are
  almost certainly empty). This is where the ~15% byte saving over the baseline comes from.
* **Colour**: one 16-colour palette fitted once on the sender's map, sent once (48 B); greedy RD index choice; context model
  (plane vote + previous z + parent voxel colour) carried across chunks and bands. Crisp palette voxels, no smoothing.
* **Budget fit**: per slot, a warm-started power-law secant search on the refinement radius/level, capped at 4-7 trials; the band
  voxel ids are precomputed once so each trial is cheap (encode 1.2-1.9 s per slot).
* **Decoder** uses only the bytes (carried state is rebuilt deterministically from them); densifies coarse voxels into 0.5 m
  points so the renderer draws one splat size.

## How to run

```
PY=/tmp/claude-1000/-home-matteo-Documents-3d/ec416e8c-37fe-40e2-9d59-d38cd41f70c1/scratchpad/venv_geo/bin/python
cd /home/matteo/Documents/3d/slam_results/research
$PY live_eval.py stream_best.py --views views_best --json results_live/stream_best.json   # benchmark (~1-3 min)
$PY run_stream.py stream_best.py ../00_BEST_10kbit_live                                   # wire/chunk*.bin, map_after_slot*.ply, final.*
$PY render_live.py stream_best.py ../00_BEST_10kbit_live/wire ../00_BEST_10kbit_live/flythrough_live_growing.mp4
cd ../00_BEST_10kbit_live && $PY ../tools/fly_higher.py final.ply --raise-m 0 --extend 5 --single --voxel $(cat final.splat) --out flythrough_final.mp4
```
`compare.png` was stacked from `research/views_live_base/{live02,final04}.png` (top = reference, bottom = baseline) and
`research/views_best/{live02,final04}.png` (bottom = chosen).

## Dependencies

Python with numpy, scipy, open3d, Pillow and **constriction** (range coder) — all in the scratch venv above
(`.../scratchpad/venv_geo/bin/python`); ffmpeg on PATH for the videos. The streamer imports `lod_common.py`,
`codec_lod_bands.py` and `../compress_study/codec_combo.py` (+ `codec_geo_octctx2.py`, `codec_col_greedy.py`) from the research dir.

## Known limitations

* **Encode time** 1.2-1.9 s per slot here (simulation, CPU shared), fine for a 6 s keyframe interval but it is a multi-trial budget
  search; a real-time sender should warm-start from the previous slot's solution (already done) and cap trials.
* **Latency**: the link is saturated; a new chunk is only usable once fully received (~5.8 s after its keyframe), and the last chunk
  lands 5.8 s after the flight ends. Detections need their own reserved share of the link.
* **Holes** ~1% of surface pixels (pruned low-confidence cells, thin objects, tree edges); the 0.5 m band is barely affordable at 10 kbit/s
  so the foreground stays at 1 m voxels.
* **No pose-correction handling**: if SLAM later moves earlier keyframes, voxels already sent stay where they were. Negligible on this
  23 s straight flight; longer flights should anchor chunks to keyframes so a correction is a 28-byte pose update.
* No packet loss / reordering handling: the context tables are carried across chunks, so a lost chunk desynchronises the decoder
  (needs retransmission or periodic context resets, `CTX_SHARE=0`).
