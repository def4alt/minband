# slam_results

- `00_BEST_20kbit/`: **start here.** The 20 kbit/s map stream: video, the bytes on the wire, and a comparison image.
- `videos/`: flythroughs from the full reconstruction, at +20 m and with the 5 s continuation.
  - `compression_variants/`: one video per compression setting tried (0.5, 0.7 and 1 m voxels; 16-bit, 8-bit, 4-bit and no colour; top surface only).
- `maps/source/`: the SLAM reconstructions (`.ply` point cloud plus `.txt` keyframe trajectory).
- `maps/encoded/`: compressed maps (`.bin` = bytes on the wire) and their decoded `.ply`.
- `inputs/`: the downscaled input videos used for SLAM.
- `tools/`: scripts.
  - `fly_higher.py`: renders a flythrough video.
  - `view.py`: interactive 3D viewer. Run it with `env -u WAYLAND_DISPLAY`.
  - `voxel_codec.py`: simple voxel codec.
  - `map_bandwidth.py`: estimates the size of each encoding.
- `compress_study/`: the lossless codec study and the best codec (`codec_combo.py`).
- `research/`: the 20 kbit/s research, with the render-quality benchmark `quality.py` and the LOD, DEM and split codecs.
- `old_previews/`: preview frames and logs. Safe to delete.
