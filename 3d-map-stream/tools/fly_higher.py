"""Render the SLAM point cloud along the original camera path and a raised copy.

Usage: python3 fly_higher.py houses_fast.ply --raise-m 20 --m-per-unit 40
Up = ground-plane normal (RANSAC). The raised camera looks at the same ground
point the original camera was looking at, so the scene stays framed.
"""
import argparse, os, subprocess, time, numpy as np, open3d as o3d
from scipy.spatial.transform import Rotation, Slerp

ap = argparse.ArgumentParser()
ap.add_argument("ply")
ap.add_argument("--raise-m", type=float, default=20.0)
ap.add_argument("--m-per-unit", type=float, default=40.0, help="scale guess (SLAM is not metric)")
ap.add_argument("--extend", type=float, default=0.0, help="keep flying N s past the last pose")
ap.add_argument("--single", action="store_true", help="only render the raised view")
ap.add_argument("--fps", type=int, default=10)
ap.add_argument("--w", type=int, default=640)
ap.add_argument("--h", type=int, default=360)
ap.add_argument("--frames", type=int, default=0, help="render only first N (preview)")
ap.add_argument("--voxel", type=float, default=0, help="draw points as squares of this size (SLAM units)")
ap.add_argument("--hfov", type=float, default=70.0)
ap.add_argument("--out", default="flythrough_higher.mp4")
a = ap.parse_args()

pcd = o3d.io.read_point_cloud(a.ply)
traj = np.loadtxt(a.ply[:-4] + ".txt")
ts, pos, quat = traj[:, 0], traj[:, 1:4], traj[:, 4:8]  # TUM: t x y z qx qy qz qw
gaps = np.diff(ts)  # squash long stand-still gaps (e.g. waiting before takeoff)
ts = ts[0] + np.concatenate([[0], np.cumsum(np.minimum(gaps, 3 * np.median(gaps)))])

plane, _ = pcd.voxel_down_sample(0.05).segment_plane(0.05, 3, 2000)
n, d0 = np.array(plane[:3]), plane[3]
n /= np.linalg.norm(plane[:3]); d0 /= np.linalg.norm(plane[:3])
if pos[0] @ n + d0 > 0:  # make 'up' point from ground towards the cameras
    n, d0 = -n, -d0
up = -n
lift = a.raise_m / a.m_per_unit
print(f"camera height ~{-(pos[0]@n+d0)*a.m_per_unit:.0f} m (guess), raising by {lift:.3f} units")

N = int((ts[-1] - ts[0] + a.extend) * a.fps)
tq = ts[0] + np.arange(N) / a.fps
tc = np.minimum(tq, ts[-1])
rots = Slerp(ts, Rotation.from_quat(quat))(tc).as_matrix()
P = np.stack([np.interp(tc, ts, pos[:, k]) for k in range(3)], 1)
vel = (pos[-1] - pos[-2]) / (ts[-1] - ts[-2])  # extrapolate at last speed/heading
P += np.maximum(tq - ts[-1], 0)[:, None] * vel


def look_at(c, target):
    z = target - c; z /= np.linalg.norm(z)
    x = np.cross(-up, z); x /= np.linalg.norm(x)
    y = np.cross(z, x)
    return np.stack([x, y, z], 1)  # camera-to-world rotation (OpenCV axes)


fx = a.w / 2 / np.tan(np.radians(a.hfov) / 2)
pts = np.asarray(pcd.voxel_down_sample(0.01).points)
cols = (np.asarray(pcd.voxel_down_sample(0.01).colors) * 255).astype(np.uint8)
SKY = np.array([184, 209, 237], np.uint8)


def render(R, c):
    # numpy splat renderer: project, sort far->near, 2x2 points, nearest wins
    q = (pts - c) @ R
    m = q[:, 2] > 0.05
    q, col = q[m], cols[m]
    u = (fx * q[:, 0] / q[:, 2] + a.w / 2).astype(np.int32)
    v = (fx * q[:, 1] / q[:, 2] + a.h / 2).astype(np.int32)
    k = (u >= 0) & (u < a.w - 1) & (v >= 0) & (v < a.h - 1)
    u, v, z, col = u[k], v[k], q[k, 2], col[k]
    o = np.argsort(-z)
    u, v, z, col = u[o], v[o], z[o], col[o]
    img = np.empty((a.h, a.w, 3), np.uint8); img[:] = SKY
    if not a.voxel:
        for du in (0, 1):
            for dv in (0, 1):
                img[v + dv, u + du] = col
        return img
    # voxel splats: square size = projected voxel size; far->near order keeps occlusion
    size = np.clip(np.ceil(fx * a.voxel / z), 1, 24).astype(np.int32)
    for du in range(size.max()):
        for dv in range(size.max()):
            k = size > max(du, dv)
            img[np.minimum(v[k] + dv, a.h - 1), np.minimum(u[k] + du, a.w - 1)] = col[k]
    return img


ff = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                       "-s", f"{a.w*(1 if a.single else 2)}x{a.h}", "-r", str(a.fps), "-i", "-", "-c:v", "libx264",
                       "-pix_fmt", "yuv420p", "-crf", "20", a.out], stdin=subprocess.PIPE)
for i in range(a.frames or N):
    R, c = rots[i], P[i]
    s = -(c @ n + d0) / (R[:, 2] @ n)  # where the original view ray hits the ground
    target = c + max(s, 0.5) * R[:, 2]
    c2 = c + lift * up
    high = render(look_at(c2, target), c2)
    frame = high if a.single else np.hstack([render(R, c), high])
    ff.stdin.write(np.ascontiguousarray(frame).tobytes())
ff.stdin.close(); ff.wait()
print("wrote", a.out, a.frames or N, "frames, +%.0f m, %.1f s path + %.0f s extension" % (a.raise_m, ts[-1] - ts[0], a.extend))
