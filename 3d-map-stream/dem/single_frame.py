"""SLAM-free elevation map from ONE frame with a metric depth model (MoGe2-Aerial): back-project with the known
intrinsics, RANSAC ground plane, rasterize heights in metres. This is the 'direct elevation predictor' setting.

usage: python single_frame.py FRAME.jpg --out DIR [--cells 400]
"""
import argparse, os, json, time, numpy as np
from PIL import Image
import grid as G
from depth import MogeAerial, sync

ap = argparse.ArgumentParser()
ap.add_argument("frame"); ap.add_argument("--out", required=True); ap.add_argument("--cells", type=int, default=400)
ap.add_argument("--fx", type=float, default=548); ap.add_argument("--cx", type=float, default=480); ap.add_argument("--cy", type=float, default=270)
ap.add_argument("--max-depth-m", type=float, default=250.0)
a = ap.parse_args()
os.makedirs(a.out, exist_ok=True)
img = np.asarray(Image.open(a.frame).convert("RGB"))
net = MogeAerial()
t0 = time.time(); net(img); sync(); t_depth = time.time() - t0
Z = net.last_depth
vv, uu = np.mgrid[0:Z.shape[0]:2, 0:Z.shape[1]:2]
z = Z[vv, uu]; m = np.isfinite(z) & (z < a.max_depth_m)
X = np.stack([(uu[m] - a.cx) / a.fx * z[m], (vv[m] - a.cy) / a.fx * z[m], z[m]], -1)
fr = G.fit_ground(X, cams=np.zeros((1, 3)))  # camera at the origin is above the ground
xyh = G.to_ground(X, fr)
alt_m = float(G.to_ground(np.zeros((1, 3)), fr)[0, 2])
bmin, bmax = G.bounds_of(xyh)
cell = float(max(bmax - bmin) / a.cells)
shape = (int(np.ceil((bmax[1] - bmin[1]) / cell)), int(np.ceil((bmax[0] - bmin[0]) / cell)))
H, C, _ = G.rasterize(xyh, img[vv[m], uu[m]], bmin, cell, shape)
H = G.fill_small_holes(H)
np.save(os.path.join(a.out, "dem.npy"), H); np.save(os.path.join(a.out, "dem_rgb.npy"), C)
G.save(os.path.join(a.out, "ortho.png"), G.ortho_image(C, H))
G.save(os.path.join(a.out, "relief.png"), G.relief_image(H, cell))
G.save(os.path.join(a.out, "oblique.png"), G.render_oblique(H, C, cell))
meta = dict(frame=a.frame, depth_ms_median=t_depth * 1000, cell=cell, cell_m=cell, units="metres", shape=shape,
            camera_altitude_m=alt_m, coverage=float(np.isfinite(H).mean()), median_frame_spread_alt=0.0,
            tilt_below_horizon_deg=float(np.degrees(np.arcsin(abs(fr[3] @ np.array([0, 0, 1.0]))))))
json.dump(meta, open(os.path.join(a.out, "meta.json"), "w"), indent=1)
print(json.dumps(meta, indent=1))
