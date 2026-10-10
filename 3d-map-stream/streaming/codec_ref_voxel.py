"""Reference baselines: ground-aligned voxels + best study codec (octree geometry + greedy 4-bit colour).
Env: VOX (metres, default 0.7), TOP=1 for top-surface only. Scale guess 40 m per SLAM unit."""
import os, struct, importlib.util, pathlib, numpy as np, open3d as o3d
_s = importlib.util.spec_from_file_location("cb", str(pathlib.Path(__file__).parent.parent / "compress_study" / "codec_combo.py"))
cb = importlib.util.module_from_spec(_s); _s.loader.exec_module(cb)
M = 40.0

def encode(P, rgb, traj):
    vm = float(os.environ.get("VOX", 0.7)); top = os.environ.get("TOP") == "1"
    pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(P))
    (a, b, c, d), _ = pc.voxel_down_sample(0.05).segment_plane(0.05, 3, 2000)
    n = np.array([a, b, c]); n /= np.linalg.norm(n)
    if traj[0, 1:4] @ n + d > 0: n = -n
    x = np.cross(n, [0, 0, 1.0]); x /= np.linalg.norm(x); R = np.stack([x, n, np.cross(x, n)])
    Q = P @ R.T; v = vm / M; o = Q.min(0); key = np.floor((Q - o) / v).astype(np.int64)
    u, inv = np.unique(key, axis=0, return_inverse=True); inv = inv.ravel()
    col = np.zeros((len(u), 3)); np.add.at(col, inv, rgb.astype(float)); col = np.round(col / np.bincount(inv)[:, None]).astype(np.uint8)
    if top:
        oo = np.lexsort((u[:, 1], u[:, 2], u[:, 0])); us = u[oo]
        f = np.ones(len(us), bool); f[1:] = (us[1:, 0] != us[:-1, 0]) | (us[1:, 2] != us[:-1, 2]); u, col = us[f], col[oo][f]
    return R.astype(np.float32).tobytes() + np.array([*o, v], np.float32).tobytes() + cb.encode(u.astype(np.int32), col)

def decode(b):
    R = np.frombuffer(b[:36], np.float32).reshape(3, 3).astype(float); ox, oy, oz, v = np.frombuffer(b[36:52], np.float32)
    vox, rgb = cb.decode(b[52:])
    return ((np.asarray(vox) + 0.5) * v + np.array([ox, oy, oz])) @ R, rgb, float(v) * 1.6
