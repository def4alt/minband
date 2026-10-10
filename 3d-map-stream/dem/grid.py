"""2.5D elevation grid helpers shared by the DEM experiments.

Ground frame: RANSAC plane through the scene (o, e1, e2, n), n pointing towards the cameras.
A DEM is a float32 [rows, cols] height grid (NaN = never observed) plus a uint8 colour grid.
"""
import numpy as np
from PIL import Image


def fit_ground(pts, cams=None, iters=400, thresh=None, seed=0):
    """RANSAC plane -> (o, e1, e2, n). thresh defaults to 1% of the scene extent."""
    rng = np.random.default_rng(seed)
    P = pts[rng.choice(len(pts), min(len(pts), 50000), replace=False)]
    if thresh is None:
        thresh = 0.01 * np.linalg.norm(np.percentile(P, 95, 0) - np.percentile(P, 5, 0))
    best, best_n = None, -1
    for _ in range(iters):
        a, b, c = P[rng.choice(len(P), 3, replace=False)]
        n = np.cross(b - a, c - a); nn = np.linalg.norm(n)
        if nn < 1e-12: continue
        n /= nn
        k = np.abs((P - a) @ n) < thresh
        if k.sum() > best_n: best_n, best = k.sum(), k
    Q = P[best]; o = Q.mean(0)
    n = np.linalg.svd(Q - o, full_matrices=False)[2][2]  # full U would be N x N
    if cams is not None and len(cams):
        if np.median((cams - o) @ n) < 0: n = -n
    elif np.median((P - o) @ n) < 0:  # most off-plane structure (roofs, trees) sticks up
        n = -n
    e1 = np.cross(n, [0, 0, 1.0]) if abs(n[2]) < 0.9 else np.cross(n, [1.0, 0, 0])
    e1 /= np.linalg.norm(e1); e2 = np.cross(n, e1)
    return o, e1, e2, n


def to_ground(pts, frame):
    o, e1, e2, n = frame
    d = pts - o
    return np.stack([d @ e1, d @ e2, d @ n], -1)


def bounds_of(xyh, lo=1, hi=99, margin=0.02):
    a = np.percentile(xyh[:, :2], lo, 0); b = np.percentile(xyh[:, :2], hi, 0)
    m = (b - a) * margin
    return a - m, b + m


def rasterize(xyh, cols, bmin, cell, shape, reduce="max"):
    """Top-surface raster of one point set -> (H [r,c] NaN-filled, C [r,c,3] uint8, count)."""
    r = ((xyh[:, 1] - bmin[1]) / cell).astype(np.int64)
    c = ((xyh[:, 0] - bmin[0]) / cell).astype(np.int64)
    k = (r >= 0) & (r < shape[0]) & (c >= 0) & (c < shape[1])
    r, c, h, col = r[k], c[k], xyh[k, 2], cols[k]
    idx = r * shape[1] + c
    H = np.full(shape[0] * shape[1], -np.inf, np.float32)
    if reduce == "max":
        np.maximum.at(H, idx, h)
    else:
        raise ValueError(reduce)
    # colour of the highest point per cell: sort by height, last write wins
    o = np.argsort(h)
    C = np.zeros((shape[0] * shape[1], 3), np.uint8); C[idx[o]] = col[o]
    cnt = np.bincount(idx, minlength=shape[0] * shape[1])
    H[cnt == 0] = np.nan
    return H.reshape(shape), C.reshape(shape + (3,)), cnt.reshape(shape)


def fill_small_holes(H, iters=2):
    """Fill NaN cells that have >= 3 valid 8-neighbours with their median (cosmetic, a couple of passes)."""
    H = H.copy()
    for _ in range(iters):
        pad = np.pad(H, 1, constant_values=np.nan)
        nb = np.stack([pad[1 + dy:pad.shape[0] - 1 + dy, 1 + dx:pad.shape[1] - 1 + dx]
                       for dy in (-1, 0, 1) for dx in (-1, 0, 1) if dy or dx])
        valid = np.isfinite(nb).sum(0)
        fill = np.isnan(H) & (valid >= 3)
        with np.errstate(all="ignore"):
            H[fill] = np.nanmedian(nb[:, fill], axis=0)
    return H


def hillshade(H, cell, az=315, alt=45, z=1.0):
    Hf = np.where(np.isfinite(H), H, np.nanmedian(H))
    gy, gx = np.gradient(Hf * z, cell)
    slope = np.arctan(np.hypot(gx, gy)); aspect = np.arctan2(-gx, gy)
    az, alt = np.radians(az), np.radians(alt)
    s = np.sin(alt) * np.cos(slope) + np.cos(alt) * np.sin(slope) * np.cos(az - aspect)
    return np.clip(s, 0, 1)


def relief_image(H, cell, vmin=None, vmax=None, cmap="terrain"):
    import matplotlib
    vmin = np.nanpercentile(H, 2) if vmin is None else vmin
    vmax = np.nanpercentile(H, 98) if vmax is None else vmax
    v = np.clip((H - vmin) / max(vmax - vmin, 1e-9), 0, 1)
    rgb = matplotlib.colormaps[cmap](np.nan_to_num(v))[..., :3]
    hs = hillshade(H, cell)[..., None]
    img = rgb * (0.45 + 0.55 * hs)
    img[~np.isfinite(H)] = 1.0
    return (img * 255).astype(np.uint8)


def ortho_image(C, H):
    img = C.copy(); img[~np.isfinite(H)] = 255
    return img


def render_oblique(H, C, cell, w=960, h=540, elev_deg=35, azim_deg=200, hfov=60, zscale=1.0, dist=1.25, sky=(184, 209, 237)):
    """Numpy splat render of a DEM (each cell = coloured square) from a virtual camera orbiting the grid centre."""
    rows, cols = H.shape
    rr, cc = np.nonzero(np.isfinite(H))
    X = np.stack([(cc + 0.5) * cell, (rr + 0.5) * cell, H[rr, cc] * zscale], -1)
    col = C[rr, cc]
    ctr = np.array([cols * cell / 2, rows * cell / 2, np.nanmedian(H)])
    R_ = max(rows, cols) * cell * dist
    el, az = np.radians(elev_deg), np.radians(azim_deg)
    cam = ctr + R_ * np.array([np.cos(el) * np.cos(az), np.cos(el) * np.sin(az), np.sin(el)])
    f = ctr - cam; f /= np.linalg.norm(f)
    r = np.cross(f, [0, 0, 1.0]); r /= np.linalg.norm(r); u = np.cross(r, f)
    Rm = np.stack([r, -u, f], 1)  # world -> cam columns (x right, y down, z forward)
    q = (X - cam) @ Rm
    fx = w / 2 / np.tan(np.radians(hfov) / 2)
    m = q[:, 2] > 1e-6
    q, col = q[m], col[m]
    uu = (fx * q[:, 0] / q[:, 2] + w / 2); vv = (fx * q[:, 1] / q[:, 2] + h / 2)
    size = np.clip(np.ceil(fx * cell * 1.5 / q[:, 2]), 1, 12).astype(np.int32)
    k = (uu >= 0) & (uu < w) & (vv >= 0) & (vv < h)
    uu, vv, z, col, size = uu[k].astype(np.int32), vv[k].astype(np.int32), q[k, 2], col[k], size[k]
    o = np.argsort(-z)
    uu, vv, col, size = uu[o], vv[o], col[o], size[o]
    img = np.empty((h, w, 3), np.uint8); img[:] = sky
    for du in range(size.max() if len(size) else 1):
        for dv in range(size.max() if len(size) else 1):
            k = size > max(du, dv)
            img[np.minimum(vv[k] + dv, h - 1), np.minimum(uu[k] + du, w - 1)] = col[k]
    return img


def save(path, img):
    Image.fromarray(img).save(path)
