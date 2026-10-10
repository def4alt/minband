# Findings for the live 10 kbit/s map stream (research round 2, 2026-10-10)

Budget: 10 kbit/s = 1250 B/s -> ~7 KB per keyframe slot (~6 s). Reference: 0.7 m voxels offline 91 KB, PSNR 16.6.

## Verified
- LOD bands at 28.9 KB offline: PSNR 18.3-18.6 (bands 0.5/1/2/4 best). Live, chunked per keyframe with refinement by re-send: 33.8 KB -> 18.36 (+17 % bytes for same quality).
- CONF density pruning (codec_sel_core/codec_sel_conf, CONF=0.4-0.5): drop voxels whose point density is far below what their distance predicts (sky-line fringe, tree-edge speckle). Causal, per chunk. Live: 19.05 final-map PSNR at <=7.4 KB/slot (vs 18.36). Baseline streamer stream_lod_conf.py: LIVE 17.83 / FINAL 18.82.
- codec_lod2_cheap (agent, offline, claims +0.55 dB at same bytes): share the adaptive context tables across bands (one geo stream, one colour stream, one 16-palette) and use one colour per 2x2x2 block in bands >= 1 m with NEAREST decode (no blending). Colour is ~60 % of bytes before blocks.
- Restart overhead of separate streams: +7 % (~400 B per restart). Persisting the adaptive context tables across chunks on both sides costs ~20 B per chunk.
- Progressive refinement potential (measured entropies, not implemented): last octree level = 74-77 % of geometry bytes; colour entropy given the parent (coarse) colour 1.93 b vs 2.95 b unconditioned -> a refinement chunk ~25 % cheaper than re-sending, more than the 17 % re-send cost. Needs nested levels 0.5/1/2/4.

## Rejected (do not retry)
- 8-colour palettes: metrics fine, visually loses the red roofs. Keep 16.
- Blended/blurred colour (codec_split_blk): wins PSNR, looks mushy.
- DEM + ortho (AVIF) far field (codec_hyb_*): +0.2 dB = blur reward; roofs beyond 60 m unrecognisable; cannot be refined live.
- Far cut-off (FARCUT), time-visible / pixel-coverage importance, extension-pose weighting: useless or harmful. Min distance to the known path is the right LOD driver.
- MINCNT=3 or far-band pruning: holes 3-35 %. Occlusion culling: >97 % of voxels visible (2.5D scene).
- Dropping the far field entirely: 45 % holes.

## Open
- Encode time per slot: 5-9 s with a 6-7 step budget search while CPU is shared; needs warm start / bytes-per-voxel prediction (<3 s).
- Pose corrections from SLAM after a chunk was sent are not handled (small on this 23 s straight flight).
- Slot 0 live view is the worst (16.3 dB): the first 7 KB must cover the whole area coarse.
