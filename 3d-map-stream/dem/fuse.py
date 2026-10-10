"""Light DEM pipeline: ORB-SLAM3 keyframe poses + Depth Anything V2 relative depth -> 2.5D elevation grid.

Per keyframe: predict relative inverse depth d(u,v); fit 1/z = a*d + b to the ORB-SLAM3 map points that keyframe
observes (robust, trimmed LS); back-project, rasterize the top surface into a ground-plane grid; median over keyframes.

usage: python fuse.py --frames runs/dem/frames_clip30 --slam runs/dem/orbslam3/clip30 --out runs/dem/vo_da_clip30
       [--model small] [--cells 600] [--stride 2] [--fx 548 --fy 548 --cx 480 --cy 270]
Heights are in units of the median camera altitude above the ground plane (scale-free, same as dem_from_cloud.py).
"""
import argparse, os, json, time, numpy as np
from PIL import Image
from scipy.spatial.transform import Rotation
import grid as G

ap = argparse.ArgumentParser()
ap.add_argument("--frames", required=True); ap.add_argument("--slam", required=True); ap.add_argument("--out", required=True)
ap.add_argument("--model", default="small"); ap.add_argument("--cells", type=int, default=600); ap.add_argument("--stride", type=int, default=2)
ap.add_argument("--fx", type=float, default=548); ap.add_argument("--fy", type=float, default=548)
ap.add_argument("--cx", type=float, default=480); ap.add_argument("--cy", type=float, default=270)
ap.add_argument("--fit", default="affine", choices=["affine", "affine_uv"],
                help="affine: 1/z = a*d + b; affine_uv: + c*u + e*v (removes image-space tilt of the relative depth)")
ap.add_argument("--max-depth-x", type=float, default=4.0, help="drop pixels farther than this x the median observed depth")
a = ap.parse_args()
os.makedirs(a.out, exist_ok=True)

# --- SLAM outputs ---
kft = np.loadtxt(os.path.join(a.slam, "KeyFrameTrajectory.txt")).reshape(-1, 8)
mp = np.loadtxt(os.path.join(a.slam, "mappoints.txt")).reshape(-1, 3)
obs = np.loadtxt(os.path.join(a.slam, "kf_observations.txt"), comments="#").reshape(-1, 5)  # kf_id t row u v
frames = {float(t): p for t, p in (l.split() for l in open(os.path.join(a.frames, "rgb.txt")) if not l.startswith("#"))}
ftimes = np.array(sorted(frames))
cams = kft[:, 1:4]
gframe = G.fit_ground(mp, cams)
alt = float(np.median(G.to_ground(cams, gframe)[:, 2]))

from depth import load as load_depth, sync
net = load_depth(a.model)

# --- per keyframe: depth, scale fit, back-projection ---
per_frame, stats, all_xyh = [], [], []
t_depth, t_fit = [], []
for t, x, y, z, qx, qy, qz, qw in kft:
    o = obs[np.abs(obs[:, 1] - t) < 1e-4]
    if len(o) < 30:
        continue
    fi = ftimes[np.argmin(np.abs(ftimes - t))]
    img = np.asarray(Image.open(os.path.join(a.frames, frames[fi])).convert("RGB"))
    H_, W_ = img.shape[:2]
    t0 = time.time(); d = net(img); sync(); t_depth.append(time.time() - t0)
    t0 = time.time()
    R = Rotation.from_quat([qx, qy, qz, qw]).as_matrix(); c = np.array([x, y, z])
    Pc = (mp[o[:, 2].astype(int)] - c) @ R  # world -> camera
    u, v = o[:, 3], o[:, 4]
    k = (Pc[:, 2] > 0) & (u >= 0) & (u < W_ - 1) & (v >= 0) & (v < H_ - 1)
    zs, ds = Pc[k, 2], d[v[k].astype(int), u[k].astype(int)]
    # robust fit of inverse depth: 1/z = a*d + b (trim the worst 20% twice)
    un, vn = u[k] / W_ - 0.5, v[k] / H_ - 0.5
    feats = (lambda d_, u_, v_: np.stack([d_, np.ones_like(d_)] + ([u_, v_] if a.fit == "affine_uv" else []), -1))
    F = feats(ds, un, vn)
    keep = np.ones(len(zs), bool)
    for _ in range(3):
        coef = np.linalg.lstsq(F[keep], 1 / zs[keep], rcond=None)[0]
        res = np.abs((F @ coef) * zs - 1)  # relative inverse-depth error
        keep = res <= np.quantile(res, 0.8)
    sa, sb = coef[0], coef[1]
    pred_z = 1 / np.maximum(F @ coef, 1e-9)
    relerr = np.median(np.abs(pred_z - zs) / zs)
    # dense back-projection
    vv, uu = np.mgrid[0:H_:a.stride, 0:W_:a.stride]
    disp = feats(d[vv, uu], uu / W_ - 0.5, vv / H_ - 0.5) @ coef
    zmax = a.max_depth_x * np.median(zs)
    m = (disp > 1 / zmax)
    Z = 1 / disp[m]
    Xc = np.stack([(uu[m] - a.cx) / a.fx * Z, (vv[m] - a.cy) / a.fy * Z, Z], -1)
    Xw = Xc @ R.T + c
    xyh = G.to_ground(Xw, gframe); xyh[:, 2] /= alt
    per_frame.append((xyh, img[vv[m], uu[m]]))
    all_xyh.append(xyh[::20])
    t_fit.append(time.time() - t0)
    st = dict(t=float(t), n_obs=int(k.sum()), relerr_depth=float(relerr), a=float(sa), b=float(sb))
    if getattr(net, "last_depth", None) is not None:  # metric model: metres per SLAM unit from the map points
        zm = net.last_depth[v[k].astype(int), u[k].astype(int)]
        ok = np.isfinite(zm)
        st["m_per_unit"] = float(np.median(zm[ok] / zs[ok]))
    stats.append(st)

# --- grid + median fusion ---
t0 = time.time()
bmin, bmax = G.bounds_of(np.concatenate(all_xyh))
cell = float(max(bmax - bmin) / a.cells)
shape = (int(np.ceil((bmax[1] - bmin[1]) / cell)), int(np.ceil((bmax[0] - bmin[0]) / cell)))
Hs, Cs = [], []
for xyh, col in per_frame:
    H, C, cnt = G.rasterize(xyh, col, bmin, cell, shape)
    Hs.append(H); Cs.append(np.where(np.isfinite(H)[..., None], C.astype(np.float32), np.nan))
Hs = np.stack(Hs); Cs = np.stack(Cs)
with np.errstate(all="ignore"):
    H = np.nanmedian(Hs, 0).astype(np.float32)
    C = np.nan_to_num(np.nanmedian(Cs, 0)).astype(np.uint8)
    spread = np.nanstd(Hs, 0)
H = G.fill_small_holes(H)
t_fuse = time.time() - t0

np.save(os.path.join(a.out, "dem.npy"), H); np.save(os.path.join(a.out, "dem_rgb.npy"), C)
G.save(os.path.join(a.out, "ortho.png"), G.ortho_image(C, H))
G.save(os.path.join(a.out, "relief.png"), G.relief_image(H, cell))
G.save(os.path.join(a.out, "oblique.png"), G.render_oblique(H, C, cell))
# one single-keyframe heightmap for reference (what a per-frame elevation predictor would give)
mid = len(per_frame) // 2
H1, C1, _ = G.rasterize(*per_frame[mid], bmin, cell, shape)
G.save(os.path.join(a.out, "relief_single_kf.png"), G.relief_image(H1, cell))
meta = dict(model=a.model, depth_params_M=net.params / 1e6, keyframes_used=len(per_frame), keyframes=len(kft),
            map_points=len(mp), cell=cell, shape=shape, camera_altitude_units=alt,
            coverage=float(np.isfinite(H).mean()),
            median_frame_spread_alt=float(np.nanmedian(spread)),
            depth_ms_median=float(np.median(t_depth) * 1000), fit_backproject_ms_median=float(np.median(t_fit) * 1000),
            fuse_s=t_fuse,
            **({"m_per_unit_median": float(np.median([s["m_per_unit"] for s in stats])),
                "m_per_unit_cv": float(np.std([s["m_per_unit"] for s in stats]) / np.mean([s["m_per_unit"] for s in stats])),
                "camera_altitude_m": float(alt * np.median([s["m_per_unit"] for s in stats]))}
               if stats and "m_per_unit" in stats[0] else {}), scale_fit_relerr_median=float(np.median([s["relerr_depth"] for s in stats])),
            bmin=bmin.tolist(), frame=[x.tolist() for x in gframe], per_kf=stats)
json.dump(meta, open(os.path.join(a.out, "meta.json"), "w"), indent=1)
print(json.dumps({k: v for k, v in meta.items() if k not in ("frame", "per_kf", "bmin")}, indent=1))
