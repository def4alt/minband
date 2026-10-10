"""DEM from an existing point cloud (e.g. the MASt3R-SLAM map) for side-by-side comparison.

usage: python dem_from_cloud.py MAP.ply [--traj MAP.txt] [--cells 600] --out DIR
Heights are reported in units of the median camera altitude above the ground plane (scale-free).
"""
import argparse, os, json, time, numpy as np
import open3d as o3d
import grid as G

ap = argparse.ArgumentParser()
ap.add_argument("ply"); ap.add_argument("--traj", default=None); ap.add_argument("--cells", type=int, default=600)
ap.add_argument("--out", required=True)
a = ap.parse_args()
os.makedirs(a.out, exist_ok=True)
t0 = time.time()
pc = o3d.io.read_point_cloud(a.ply)
P = np.asarray(pc.points); C = (np.asarray(pc.colors) * 255).astype(np.uint8)
traj = np.loadtxt(a.traj or a.ply[:-4] + ".txt").reshape(-1, 8)
cams = traj[:, 1:4]
frame = G.fit_ground(P, cams)
xyh = G.to_ground(P, frame)
alt = float(np.median(G.to_ground(cams, frame)[:, 2]))
xyh[:, 2] /= alt  # heights in camera altitudes
bmin, bmax = G.bounds_of(xyh)
cell = float(max(bmax - bmin) / a.cells)
shape = (int(np.ceil((bmax[1] - bmin[1]) / cell)), int(np.ceil((bmax[0] - bmin[0]) / cell)))
H, Cg, cnt = G.rasterize(np.c_[xyh[:, :2], xyh[:, 2]], C, bmin, cell, shape)
H = G.fill_small_holes(H)
np.save(os.path.join(a.out, "dem.npy"), H); np.save(os.path.join(a.out, "dem_rgb.npy"), Cg)
G.save(os.path.join(a.out, "ortho.png"), G.ortho_image(Cg, H))
G.save(os.path.join(a.out, "relief.png"), G.relief_image(H, cell))
G.save(os.path.join(a.out, "oblique.png"), G.render_oblique(H, Cg, cell, zscale=1.0))
meta = dict(source=a.ply, points=len(P), cell=cell, shape=shape, camera_altitude_units=alt,
            coverage=float(np.isfinite(H).mean()), seconds=time.time() - t0,
            frame=[x.tolist() for x in frame], bmin=bmin.tolist())
json.dump(meta, open(os.path.join(a.out, "meta.json"), "w"), indent=1)
print(json.dumps({k: v for k, v in meta.items() if k not in ("frame",)}, indent=1))
