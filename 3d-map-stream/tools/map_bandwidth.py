"""Estimate bytes to stream the SLAM map: full, per keyframe, per second."""
import sys, lzma, zlib, numpy as np, open3d as o3d
ply = sys.argv[1]; M = float(sys.argv[2]) if len(sys.argv) > 2 else 40.0  # metres per SLAM unit (guess)
p = o3d.io.read_point_cloud(ply)
P, C = np.asarray(p.points), (np.asarray(p.colors) * 255).astype(np.uint8)
t = np.loadtxt(ply[:-4] + ".txt"); kfs = len(t); dur = t[-1, 0] - t[0, 0]
print(f"{ply}: {len(P):,} pts, {kfs} keyframes, {dur:.1f} s flight, ~{M:.0f} m/unit\n")
print(f"{'encoding':38s} {'points':>9s} {'total':>9s} {'per KF':>9s} {'per sec':>10s}")
def row(name, n, b):
    print(f"{name:38s} {n:9,d} {b/1e6:8.2f}M {b/kfs/1e3:8.0f}K {b/dur/1e3*8:7.0f} kbit/s")
row("raw float32 xyz + rgb (ply-like)", len(P), len(P) * 15)
for vox_m in (0.25, 0.5, 1.0, 2.0):
    v = vox_m / M
    key = np.floor((P - P.min(0)) / v).astype(np.int64)
    uniq, idx, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
    inv = inv.ravel()
    col = np.zeros((len(uniq), 3)); np.add.at(col, inv, C); col /= np.bincount(inv)[:, None]
    # voxel ids fit in 16-bit per axis; sort (Morton-ish by lexsort) so deltas compress
    o = np.lexsort(uniq.T[::-1]); q = uniq[o].astype(np.uint16); c = col[o].astype(np.uint8)
    rgb565 = ((c[:, 0] >> 3).astype(np.uint16) << 11) | ((c[:, 1] >> 2).astype(np.uint16) << 5) | (c[:, 2] >> 3)
    raw = q.tobytes() + rgb565.tobytes()
    dq = np.diff(q.astype(np.int32), axis=0, prepend=0).astype(np.int16)
    comp = lzma.compress(dq.T.tobytes() + rgb565.tobytes(), preset=9)
    row(f"{vox_m:>4} m voxels, 16-bit xyz + rgb565", len(uniq), len(raw))
    row(f"{vox_m:>4} m voxels, delta + lzma", len(uniq), len(comp))
    geo = lzma.compress(dq.T.tobytes(), preset=9)
    row(f"{vox_m:>4} m voxels, geometry only (no colour)", len(uniq), len(geo))
pose = kfs * 7 * 4
print(f"\nkeyframe pose updates (7 floats each, all KFs re-sent per optimisation): {pose} B -> negligible")
