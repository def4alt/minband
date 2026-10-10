import sys, numpy as np, open3d as o3d
ply = sys.argv[1]; traj = ply[:-4] + ".txt"
pcd = o3d.io.read_point_cloud(ply)
print(ply, len(pcd.points), "points")
geoms = [pcd]
try:
    t = np.loadtxt(traj)[:, 1:4]
    ls = o3d.geometry.LineSet(o3d.utility.Vector3dVector(t),
                              o3d.utility.Vector2iVector([[i, i + 1] for i in range(len(t) - 1)]))
    ls.paint_uniform_color([1, 0, 0]); geoms.append(ls)
except Exception as e:
    print("no trajectory:", e)
o3d.visualization.draw_geometries(geoms, window_name=ply, width=1400, height=900)
