"""Gate test for hub/recon.py (P5b): a recorded chunk stream reconstructs with rows deliberately
dropped. Builds a synthetic keyframe (known ground-plane geometry + one "wall" obstacle at a known
range), quantizes/zeroes it exactly as edge.py does (zero-then-dmin/dmax, so quantized 0 <=> raw 0,
matching proto/DEPTH.md's "px=0 means no data" convention), chunks it, drops a few chunks on
purpose, feeds the rest into ReconServer, and checks: the ground fit (a, b) recovers the known
scale, the obstacle voxelizes near its known range, and the SSE handler emits "vox" deltas.
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shared import geom  # noqa: E402  (after sys.path insert)
from depth_chunks import encode_depth_chunk
from recon import DEPTH_H, DEPTH_W, ReconServer, VOXEL_M, ground_hit_range

HFOV = 85.0
PITCH_DEG, HEIGHT_M = 30.0, 5.0  # shallow enough that the top rows point above the horizon
WALL_BAND_ROWS = 6              # rows [0, WALL_BAND_ROWS) point above horizon at this pose
WALL_RANGE_M = 5.0
A0, B0 = 0.01, 0.0  # synthetic "1/z = a0*raw + b0": raw = 100/z


def build_keyframe():
    """-> (raw 48x64 float array, R, p). Ground pixels get raw=100/z (z from real ray/ground
    geometry); a band of "above-horizon" rows gets a flat wall at WALL_RANGE_M; everything else
    (no ground hit, not in the wall band) is zeroed -- exactly edge.py's `_zero_boxes` idea."""
    R, p = geom.pitch_height_pose(np.radians(PITCH_DEG), HEIGHT_M)
    K = geom.intrinsics(DEPTH_W, DEPTH_H, HFOV)
    raw = np.zeros((DEPTH_H, DEPTH_W), dtype=np.float64)
    for v in range(DEPTH_H):
        for u in range(DEPTH_W):
            hit = ground_hit_range(K, R, p, u, v)
            if hit is not None:
                t, _ = hit
                raw[v, u] = 100.0 / t
            elif v < WALL_BAND_ROWS:  # genuinely above the horizon at this pose: the wall
                raw[v, u] = 100.0 / WALL_RANGE_M
            # else: stays 0 -- sky/no-data
    return raw, R, p


def chunk_stream(raw, R, p, drone_id=1, kf_id=1, drop_rows=()):
    """-> list of encoded chunk bytes, skipping any row-pair starting at a row in `drop_rows`
    (the "rows deliberately dropped" the gate asks for)."""
    dmin, dmax = float(raw.min()), float(raw.max())
    rng = max(dmax - dmin, 1e-6)
    q8 = np.clip(((raw - dmin) / rng) * 255.0, 0, 255).astype(np.uint8)
    quat = geom.matrix_to_quat(R)
    chunks = []
    for row0 in range(0, DEPTH_H, 2):
        if row0 in drop_rows:
            continue
        chunks.append(encode_depth_chunk(drone_id, kf_id, row0, q8[row0:row0 + 2], quat, p, dmin, dmax))
    return chunks


def test_depth_chunk_roundtrip_via_recon():
    raw, R, p = build_keyframe()
    chunks = chunk_stream(raw, R, p)
    assert len(chunks) == DEPTH_H // 2
    assert all(len(c) <= 200 for c in chunks)
    print("test_depth_chunk_roundtrip_via_recon: OK")


def test_reconstructs_with_dropped_rows():
    raw, R, p = build_keyframe()
    # Drop a third of the chunks on purpose -- "accept partial keyframes".
    dropped = set(range(0, DEPTH_H, 6))
    chunks = chunk_stream(raw, R, p, drop_rows=dropped)
    assert len(chunks) < DEPTH_H // 2

    recon = ReconServer(hfov_deg=HFOV, accumulate=False)
    for c in chunks:
        recon.ingest(c)

    d = recon.drones[1]
    assert d.ab is not None, "ground fit never converged"
    a, b = d.ab
    # Recovered scale should be in the right ballpark of the synthetic (A0, B0) -- loose tolerance:
    # quantization (8-bit) plus the robust-fit trim plus the deliberately-missing rows all add noise.
    assert abs(a - A0) < A0 * 0.5, f"a={a} far from synthetic {A0}"

    assert len(d.voxels) > 0, "no obstacle voxels reconstructed at all"
    # At least one voxel should land near the wall's known range (world Z ~ -WALL_RANGE_M*cos(0)
    # along the camera's forward projection onto the ground -- just check voxel distance from the
    # camera position is in the right neighbourhood, not exact to the metre).
    cam = np.array(p)
    dists = [np.linalg.norm((np.array(k) + 0.5) * VOXEL_M - cam) for k in d.voxels]
    assert any(abs(dist - WALL_RANGE_M) < 3.0 for dist in dists), \
        f"no voxel near the wall's {WALL_RANGE_M} m range; got distances {sorted(dists)[:5]}"
    print(f"test_reconstructs_with_dropped_rows: OK (a={a:.4f} vs synthetic {A0}, "
          f"{len(d.voxels)} voxels, dropped {len(dropped)} row-chunks)")


def test_sse_emits_vox_deltas():
    raw, R, p = build_keyframe()
    recon = ReconServer(hfov_deg=HFOV, accumulate=False)
    for c in chunk_stream(raw, R, p):
        recon.ingest(c)
    deltas = recon.drain_deltas()
    assert deltas and any(len(add) > 0 for add in deltas), "no vox deltas drained after a full keyframe"
    # Draining again immediately should yield nothing new (deltas are drained-once).
    assert recon.drain_deltas() == []
    print("test_sse_emits_vox_deltas: OK")


if __name__ == "__main__":
    test_depth_chunk_roundtrip_via_recon()
    test_reconstructs_with_dropped_rows()
    test_sse_emits_vox_deltas()
    print("hub/test_recon.py: ALL OK")
