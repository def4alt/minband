"""Sweep voxel size / colour / top-surface-only / range crop against a kbit/s budget."""
import numpy as np, open3d as o3d, importlib.util, constriction, sys, os
os.environ.setdefault("LAM", "3")
s = importlib.util.spec_from_file_location("cb", "codec_combo.py"); cb = importlib.util.module_from_spec(s); s.loader.exec_module(cb)
p = o3d.io.read_point_cloud("../maps/source/houses_7fps.ply"); P0, C0 = np.asarray(p.points), np.asarray(p.colors) * 255
traj = np.loadtxt("../maps/source/houses_7fps.txt"); DUR = traj[-1, 0] - traj[0, 0]; cams = traj[:, 1:4]; M = 40.0

def geo_bytes(vox):
    vox = vox[np.lexsort(vox.T[::-1])].astype(np.int64); ext = vox.max(0) + 1; D = int(np.ceil(np.log2(ext.max())))
    enc = constriction.stream.queue.RangeEncoder()
    def coder(f, pp, y): enc.encode(y, f, pp); return y
    cb.geo._walk(ext, D, coder, vox); return len(enc.get_compressed().tobytes()) + 15

def run(vox_m, colour=True, top=False, crop_m=None):
    P, C = P0, C0
    if crop_m:  # keep points within crop_m metres (horizontal-ish) of the flight path
        d = np.min(np.linalg.norm(P[:, None, :] - cams[None], axis=2), 1) * M
        P, C = P[d < crop_m], C[d < crop_m]
    v = vox_m / M; key = np.floor((P - P.min(0)) / v).astype(np.int64)
    u, inv = np.unique(key, axis=0, return_inverse=True); inv = inv.ravel()
    col = np.zeros((len(u), 3)); np.add.at(col, inv, C); col = np.round(col / np.bincount(inv)[:, None]).astype(np.uint8)
    if top:  # keep only the top voxel of each column (y is roughly 'down' in SLAM frame -> min y = top)
        o = np.lexsort((u[:, 1], u[:, 2], u[:, 0])); us = u[o]
        first = np.ones(len(us), bool); first[1:] = (us[1:, 0] != us[:-1, 0]) | (us[1:, 2] != us[:-1, 2])
        u, col = us[first], col[o][first]
    u = u.astype(np.int32)
    b = len(cb.encode(u, col)) if colour else geo_bytes(u)
    tag = f"{vox_m:>4} m {'4-bit' if colour else 'shape'}{' top-only' if top else ''}{f' crop {crop_m}m' if crop_m else ''}"
    print(f"{tag:34s} {len(u):8,d} vox {b/1e3:7.1f} KB {8*b/DUR/1e3:6.1f} kbit/s {'<= 10 OK' if 8*b/DUR/1e3 <= 10 else ''}", flush=True)

for vm in (0.5, 1.0, 1.5, 2.0):
    run(vm, True); run(vm, False)
for vm in (1.0, 1.5):
    run(vm, True, top=True); run(vm, False, top=True)
for cm in (60, 100):
    run(1.0, True, crop_m=cm); run(1.0, False, top=True, crop_m=cm)
