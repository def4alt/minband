"""Run a codec round-trip and write the decoded map as .ply (+ trajectory) for rendering."""
import sys, shutil, importlib.util, numpy as np, open3d as o3d
d = np.load("input_0.5m.npz")
spec = importlib.util.spec_from_file_location("c", sys.argv[1]); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
blob = m.encode(d["vox"].copy(), d["rgb"].copy()); vox, rgb = m.decode(blob)
pts = (np.asarray(vox) + 0.5) * float(d["voxel"]) + d["origin"]
pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(pts)); pc.colors = o3d.utility.Vector3dVector(np.asarray(rgb) / 255.0)
out = sys.argv[2]; o3d.io.write_point_cloud(out + ".ply", pc); shutil.copy("../maps/source/houses_7fps.txt", out + ".txt")
print(out, len(blob), "bytes")
