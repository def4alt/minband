"""Encode a SLAM map as voxels + delta + lzma, then decode it back to a .ply (what the receiver sees)."""
import sys, lzma, shutil, numpy as np, open3d as o3d
# usage: voxel_codec.py map.ply VOXEL_M [M_PER_UNIT] [565|332|none]
ply, vox_m = sys.argv[1], float(sys.argv[2])
M = float(sys.argv[3]) if len(sys.argv) > 3 else 40.0
mode = sys.argv[4] if len(sys.argv) > 4 else "565"
p = o3d.io.read_point_cloud(ply)
P, C = np.asarray(p.points), (np.asarray(p.colors) * 255).astype(np.uint8)
v = vox_m / M; origin = P.min(0)
key = np.floor((P - origin) / v).astype(np.int64)
uniq, inv = np.unique(key, axis=0, return_inverse=True); inv = inv.ravel()
col = np.zeros((len(uniq), 3)); np.add.at(col, inv, C); col = (col / np.bincount(inv)[:, None]).astype(np.uint8)
o = np.lexsort(uniq.T[::-1]); q = uniq[o].astype(np.uint16); c = col[o]
if mode == "565":
    colbytes = (((c[:, 0] >> 3).astype(np.uint16) << 11) | ((c[:, 1] >> 2).astype(np.uint16) << 5) | (c[:, 2] >> 3)).tobytes()
elif mode == "332":
    colbytes = ((c[:, 0] >> 5) << 5 | (c[:, 1] >> 5) << 2 | (c[:, 2] >> 6)).astype(np.uint8).tobytes()
else:
    colbytes = b""
dq = np.diff(q.astype(np.int32), axis=0, prepend=0).astype(np.int16)
header = np.array([*origin, v], np.float32).tobytes() + np.uint32(len(q)).tobytes()
blob = header + lzma.compress(dq.T.tobytes() + colbytes, preset=9)
print(f"encoded {len(q):,} voxels -> {len(blob)/1e3:.0f} KB")
# ---- receiver side ----
ox, oy, oz, vv = np.frombuffer(blob[:16], np.float32); n = int(np.frombuffer(blob[16:20], np.uint32)[0])
raw = lzma.decompress(blob[20:])
dq2 = np.frombuffer(raw[:n * 6], np.int16).reshape(3, n).T.astype(np.int32)
q2 = np.cumsum(dq2, axis=0)
pts = (q2 + 0.5) * vv + np.array([ox, oy, oz])
out = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(pts))
if mode == "565":
    c565 = np.frombuffer(raw[n * 6:], np.uint16).astype(np.uint32)
    rgb = np.stack([(c565 >> 11) << 3, ((c565 >> 5) & 63) << 2, (c565 & 31) << 3], 1) / 255.0
elif mode == "332":
    c8 = np.frombuffer(raw[n * 6:], np.uint8).astype(np.uint32)
    rgb = np.stack([(c8 >> 5) * 36, ((c8 >> 2) & 7) * 36, (c8 & 3) * 85], 1) / 255.0
else:  # no colour sent: receiver shades by surface normals (+ slight height tint)
    (a_, b_, c_, d_), _ = out.segment_plane(2 * vv, 3, 1000)
    up = np.array([a_, b_, c_]); h = pts @ up + d_
    if np.median(h) < 0: up, h = -up, -h
    out.estimate_normals(o3d.geometry.KDTreeSearchParamKNN(16))
    out.orient_normals_to_align_with_direction(up)
    nr = np.asarray(out.normals)
    light = up + 0.6 * np.cross(up, [1.0, 0, 0]); light /= np.linalg.norm(light)
    shade = 0.25 + 0.75 * np.clip(nr @ light, 0, 1)
    h = np.clip((h - np.percentile(h, 2)) / (np.percentile(h, 98) - np.percentile(h, 2) + 1e-9), 0, 1)
    rgb = shade[:, None] * np.stack([0.75 + 0.2 * h, 0.78 + 0.1 * h, 0.85 - 0.25 * h], 1)
out.colors = o3d.utility.Vector3dVector(np.clip(rgb, 0, 1))
name = ply[:-4] + f"_vox{vox_m:g}m" + ("" if mode == "565" else f"_{mode}")
o3d.io.write_point_cloud(name + ".ply", out); shutil.copy(ply[:-4] + ".txt", name + ".txt")
open(name + ".bin", "wb").write(blob)
print("decoded ->", name + ".ply", "| wire file:", name + ".bin")
