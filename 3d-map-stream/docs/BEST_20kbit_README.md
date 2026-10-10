# Best map stream at 20 kbit/s: level-of-detail voxels

| File | What it is |
|---|---|
| `flythrough_lod20k.mp4` | **The video.** It's rendered from the decoded 56.6 KB map: the 23 s flight plus 5 s of continued flight. |
| `compare_reference_vs_0.7m_vs_lod.png` | The same view three times, top to bottom: the uncompressed reconstruction, 0.7 m voxels (91 KB, over budget), and this solution (56.6 KB). |
| `houses_lod20k.bin` | **What goes over the radio:** 56,805 bytes for the whole flight, about 19.6 kbit/s. |
| `houses_lod20k.ply/.txt/.splat` | The decoded map (what the receiver has), the trajectory, and the point size used for rendering. |

**What to look for in the video and image:** red roofs, the road, the pools and individual trees should stay recognisable. In the 0.7 m version they smear into vertical streaks. There are small gaps and speckle at the far horizon (about 1.3% holes).

**Scores** (`research/quality.py`, 12 views, compared against the uncompressed map):

| | Size | PSNR | SSIM | Holes |
|---|---|---|---|---|
| LOD bands (this) | 56.6 KB | 19.6 | 0.69 | 1.3% |
| Height map + aerial photo (DEM + ortho) | 54.0 KB | 19.1 | 0.64 | 0.2% |
| 0.7 m full surface | 91 KB ✗ | 16.6 | 0.47 | 0.1% |

DEM + ortho is the runner-up. Its tree sides and walls show as vertical streaks.

**How it works:**
- **Voxel size by distance:** voxels grow with distance from the flight path, in bands from 0.5 m near the drone to 2 m far away.
- **Budget fit:** the encoder picks the scale that just fits the budget.
- **Noise removal:** voxels backed by only one source point are dropped.
- **Coding:** each band is coded with the octree and 4-bit palette codec from `compress_study/codec_combo.py`.
- **Receiver:** coarse voxels are drawn as flat patches with small splats.

**Code:**
- **Codec:** `research/codec_lod_bands.py` with `research/lod_common.py`.
- **Dependency:** it needs the pip package `constriction`.
- **Encode time:** about 13 s, because of the budget search. Use a fixed scale for live, per-keyframe streaming.
