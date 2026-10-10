"""Shared helpers for codec_lod_* (LOD / visibility selection on top of compress_study codec_combo)."""
import importlib.util, pathlib, numpy as np, open3d as o3d
_here = pathlib.Path(__file__).parent
def _load(name, path):
    s = importlib.util.spec_from_file_location(name, str(path)); m = importlib.util.module_from_spec(s); s.loader.exec_module(m); return m
cb = _load("cb", _here.parent / "compress_study" / "codec_combo.py")
Q = _load("quality_mod", _here / "quality.py")
M = 40.0

def ground_frame(P, traj):
    o3d.utility.random.seed(0); pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(P))
    (a, b, c, d), _ = pc.voxel_down_sample(0.05).segment_plane(0.05, 3, 2000)
    n = np.array([a, b, c]); n /= np.linalg.norm(n)
    if traj[0, 1:4] @ n + d > 0: n = -n
    x = np.cross(n, [0, 0, 1.0]); x /= np.linalg.norm(x); return np.stack([x, n, np.cross(x, n)])

def voxelize(Q3, rgb, v, o):
    key = np.floor((Q3 - o) / v).astype(np.int64)
    u, inv, cnt = np.unique(key, axis=0, return_inverse=True, return_counts=True); inv = inv.ravel()
    col = np.zeros((len(u), 3)); np.add.at(col, inv, rgb.astype(float))
    return u, np.round(col / cnt[:, None]).astype(np.uint8), cnt, inv

def poses(traj, n=48, extend=5.0):
    return Q.views(traj, n, extend)

def visibility(pts, splat, Rs, Ps, sizes=None):
    """Emulate quality.render with ids; return per-point count of winning pixels summed over poses."""
    W, H, FX = Q.W, Q.H, Q.FX
    cover = np.zeros(len(pts), np.int64)
    for R, c in zip(Rs, Ps):
        q = (pts - c) @ R; idx = np.nonzero(q[:, 2] > 0.05)[0]; q = q[idx]
        u = (FX * q[:, 0] / q[:, 2] + W / 2).astype(np.int32); v = (FX * q[:, 1] / q[:, 2] + H / 2).astype(np.int32)
        k = (u >= 0) & (u < W) & (v >= 0) & (v < H); u, v, z, idx = u[k], v[k], q[k, 2], idx[k]
        o = np.argsort(-z); u, v, z, idx = u[o], v[o], z[o], idx[o]
        sp = splat if np.isscalar(splat) else splat[idx]
        size = np.clip(np.ceil(FX * sp / z), 2, 40).astype(np.int32)
        img = np.full((H, W), -1, np.int64)
        for du in range(size.max()):
            for dv in range(size.max()):
                kk = size > max(du, dv); uu = np.minimum(u[kk] + du, W - 1); vv = np.minimum(v[kk] + dv, H - 1)
                img[vv, uu] = idx[kk]
        w = img[img >= 0]; cover += np.bincount(w, minlength=len(pts))
    return cover
