"""Build a voxel variant, round-trip it through codec_combo, write decoded .ply for rendering.
usage: make_variant.py VOXEL_M [--top] [--crop M] OUT_PREFIX"""
import sys, shutil, argparse, importlib.util, numpy as np, open3d as o3d
ap = argparse.ArgumentParser(); ap.add_argument("vox_m", type=float); ap.add_argument("out")
ap.add_argument("--top", action="store_true"); ap.add_argument("--crop", type=float); a = ap.parse_args()
s = importlib.util.spec_from_file_location("cb", "codec_combo.py"); cb = importlib.util.module_from_spec(s); s.loader.exec_module(cb)
p = o3d.io.read_point_cloud("../maps/source/houses_7fps.ply"); P, C = np.asarray(p.points), np.asarray(p.colors) * 255
traj = np.loadtxt("../maps/source/houses_7fps.txt"); M = 40.0
if a.crop:
    d = np.min(np.linalg.norm(P[:, None, :] - traj[None, :, 1:4], axis=2), 1) * M; P, C = P[d < a.crop], C[d < a.crop]
# align grid with the ground plane so 'top of column' = true up (rotation goes in the header)
pc0 = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(P))
(pa, pb, pc_, pd), _ = pc0.voxel_down_sample(0.05).segment_plane(0.05, 3, 2000)
n = np.array([pa, pb, pc_]); n /= np.linalg.norm(n)
if (traj[0, 1:4] @ n + pd) > 0: n = -n            # n points down (towards ground), like SLAM +y
yax = n; xax = np.cross(yax, [0, 0, 1.0]); xax /= np.linalg.norm(xax); zax = np.cross(xax, yax)
R = np.stack([xax, yax, zax])                    # world -> ground-aligned frame
P = P @ R.T
v = a.vox_m / M; origin = P.min(0); key = np.floor((P - origin) / v).astype(np.int64)
u, inv = np.unique(key, axis=0, return_inverse=True); inv = inv.ravel()
col = np.zeros((len(u), 3)); np.add.at(col, inv, C); col = np.round(col / np.bincount(inv)[:, None]).astype(np.uint8)
if a.top:
    o = np.lexsort((u[:, 1], u[:, 2], u[:, 0])); us = u[o]
    f = np.ones(len(us), bool); f[1:] = (us[1:, 0] != us[:-1, 0]) | (us[1:, 2] != us[:-1, 2]); u, col = us[f], col[o][f]
blob = cb.encode(u.astype(np.int32), col); vox, rgb = cb.decode(blob)
pts = ((np.asarray(vox) + 0.5) * v + origin) @ R
pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(pts)); pc.colors = o3d.utility.Vector3dVector(np.asarray(rgb) / 255.0)
o3d.io.write_point_cloud(a.out + ".ply", pc); shutil.copy("../maps/source/houses_7fps.txt", a.out + ".txt"); open(a.out + ".bin", "wb").write(R.astype(np.float32).tobytes() + blob)
print(f"{a.out}: {len(u):,} voxels, {(len(blob)+36)/1e3:.1f} KB, {8*(len(blob)+36)/(traj[-1,0]-traj[0,0])/1e3:.1f} kbit/s")
