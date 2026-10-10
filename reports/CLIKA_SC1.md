# CLIKA_SC1 — ClikaTiledDetector / DepthModel fps on real MEVA 4K footage

Gate for this phase: measured Hz on real footage (not a guess), plus a written cadence/tiling/
scale decision for P4. Both are below. Hardware: laptop iGPU, Intel Arc B390, Vulkan backend
(`Device.parse('vulkan') -> vulkan:0`).

## What ran

- Footage: MEVA UAV drop 1 clip `2018-03-13.16-00-14.16-03-38.uav1.mp4` (3840x2160, 29.97 fps,
  CC-BY-4.0), fetched with `tools/footage/meva.py fetch 2018-03-13.16-00-14 clips/` (270 MB).
  Never committed (`tools/footage/clips/` is gitignored).
- Model: `depth-anything/Depth-Anything-V2-Small-hf`, fetched into `tools/footage/models/clika`
  via `clika_runtime.modelverse.ModelRegistry.builtin().load_depth(source, LoadOptions(device=...,
  cache_dir=..., offline=False))` — the same ClikaRT model-cache mechanism `edge/detector.py`
  already uses for the detector (`ustc-community/dfine-nano-coco`), so both land under the one
  cache dir, already gitignored (`tools/footage/models/`).
- Detector weights (`dfine-nano-coco`) were *not* in the brief's "fetch exactly two things" list,
  but its safetensors were missing from any local cache (only HF repo metadata, no weight file),
  so `ClikaTiledDetector` could not load at all without fetching them too. Treated as a
  prerequisite of running `edge/detector.py` (already referenced by its own default
  `--cache-dir`), not a third independent fetch — flagging this instead of silently expanding
  scope further.
- Benchmark script: `edge/bench_clika.py` (new, `edge/`-owned). Reads N real decoded frames once,
  times `ClikaTiledDetector.__call__` / `DepthModel.predict` over a fixed frame budget per config
  (small budget for the CPU config, which is slow by design, not pathology).

## Measured fps (exact numbers from the run)

| config | device | resolution | tile | tiles | frames | fps | ms/tile |
|---|---|---|---|---|---|---|---|
| detector, auto tile | vulkan:0 | 3840x2160 (native) | 640px | 28 | 20 | **0.508** | 70.2 |
| detector, tile=1280 | vulkan:0 | 3840x2160 (native) | 1280px | 8 | 20 | **1.759** | 71.1 |
| detector, auto tile | vulkan:0 | 1920x1080 (downscaled) | 640px | 8 | 20 | **1.927** | 64.9 |
| detector, auto tile | cpu | 3840x2160 (native) | 640px | 28 | 3 | **0.170** | 210.6 |
| depth, native input | vulkan:0 | 3840x2160 | — | — | 20 | **0.233** | — |
| depth, downscaled input | vulkan:0 | 640x360 | — | — | 5 | **5.606** | — |
| combined (detector tile=1280 4K + depth 640x360), interleaved | vulkan:0 | mixed | 1280px | 8 | 15 each | det **1.640**, depth **4.078** | — |

Brief's assumption of ~10 Hz end-to-end at 4K: **wrong by roughly 20x** at the detector's tuned
auto-tile setting (28 tiles, 0.51 fps), and still off by ~4-6x even at the fastest detector
configs measured here (~1.6-1.9 fps). ms/tile is near-constant (~65-71 ms) across every detector
config at every resolution: **fps is tile-count-bound**, confirmed directly — 28 tiles -> 8 tiles
is a 3.5x fewer-tiles ratio and the measured fps ratio is 3.46x (1.759/0.508), 1080p's 8 tiles
(resized frame, same 640px/1.5x tuning) lands within 10% of 4K's 8-tile number (1.927 vs 1.759),
i.e. the tile count, not the source resolution, sets the cost.

CPU fallback: ~3x the per-tile cost of Vulkan (210.6 ms vs ~65-71 ms) at the same tile count —
this is the silent-fallback risk the device guard (below) exists for: a cold-start Vulkan
exclusion would not error, it would just make every number above 3x worse with no log line
explaining why.

Depth model: resolution-bound even more sharply than the detector — 0.233 fps on raw 4K frames vs
5.606 fps on the same frames downscaled to 640x360 (24x). The brief's "roughly 2-5 Hz" assumption
only holds if the depth model is fed a downscaled frame, not the native 4K frame; fed native 4K it
is nowhere near that range. Run alongside the detector (interleaved, one call each per outer loop
step), both held close to their solo numbers (detector 1.64 vs 1.76 fps solo, depth 4.08 vs 5.61
fps solo) — some contention (~7-27% slower each) but neither starves the other on this iGPU.

## Chosen operating point (for P4's cadence design)

- **Tile size: leave it on auto (`--tile 0`, the existing 1.5x-magnification tuning), but run the
  detector on a 1080p-downscaled copy of the frame, not native 4K.** This gives 8 tiles — same
  count, same per-tile magnification as the already-tuned 640px/1.5x setting (so it is not an
  untested new tiling parameter) — at 1.93 fps, the fastest config measured without changing what
  the detector actually sees per tile. Rejected `--tile 1280` on native 4K (1.76 fps, similar
  speed) because it changes the tile-to-network-input magnification away from the tuned 1.5x
  ratio, which is exactly the parameter the repo's own battlefield findings (`tools/footage/
  README.md`) say trades vehicle recall for tile count — untested here (no labels fetched this
  phase, by design: scope was fps only) and not worth risking for an equivalent fps.
- **Depth: feed it a downscaled frame (e.g. 640px wide), not the native 4K frame it would
  otherwise receive.** 24x faster (0.23 -> 5.6 fps) for depth, whose output is a coarse range/
  obstacle map, not a small-object detector — downscaling costs it far less than it costs the
  detector.
- **Cadence for P4: design around ~2 Hz detection and ~4 Hz depth on this hardware, not ~10 Hz.**
  At 30 fps source video that is roughly every 15th frame for detection, every 7-8th for depth,
  run concurrently (measured: both survive concurrent execution on the one iGPU within ~30% of
  their solo rates, no starvation).
- **Explicit caveat, not swept under the rug:** this operating point was chosen for speed only.
  Downscaling to 1080p before tiling shrinks small/distant targets below where the 1.5x-into-640
  tuning put them for native 4K — the same recall-vs-tile-size tradeoff the repo's docs already
  warn about, just reached by shrinking the source instead of growing the tile. Recall impact is
  not measured here (no ground truth fetched this phase); P4/a later phase should run `track.py
  detect` + `audit.py` on this clip at both native 4K and 1080p-downscaled to quantify it before
  locking the 1080p choice in for production cadence.

## Vulkan cold-start guard

`edge/detector.py`: `ClikaTiledDetector.__init__` now logs the device ClikaRT actually loaded onto
(`check_device()`, also used standalone for the depth model in the benchmark script) and raises
`RuntimeError` if `--device vulkan` was requested but the model landed on CPU, unless
`--allow-cpu-fallback` is passed. Verified both branches directly (mismatch raises, flag allows,
match passes) and via `edge/bench_clika.py`'s runs, which logged `requested device='vulkan' actual
device='vulkan:0'` on every Vulkan config above.

The guard **did not fire** during any benchmark run in this phase (no CPU fallback was observed —
7 earlier manual loads and all of the benchmark's Vulkan configs loaded cleanly on `vulkan:0`); the
one-time cold-start error described in the brief did not reproduce here either. The guard is cheap
insurance for the next cold boot, not evidence the transient is fixed.
