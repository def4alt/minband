"""Flythrough of the map AS IT ARRIVES over a 10 kbit/s link.
usage: python render_live.py stream_x.py OUTDIR/wire out.mp4
Camera path = houses_7fps.txt exactly like ../tools/fly_higher.py (--raise-m 0 --extend 5 --single): ground-plane
'up' from RANSAC, look_at the ground point of the original view ray, 640x360 @ 10 fps, numpy splat renderer.
Chunk k starts transmitting at keyframe time t_k (or when the previous chunk finished, back to back) at 1250 B/s;
a frame at time t shows only the chunks FULLY received by t. Overlay: slot number + bytes received so far."""
import sys, os, subprocess, importlib.util, numpy as np, open3d as o3d
from scipy.spatial.transform import Rotation, Slerp
from PIL import Image, ImageDraw, ImageFont
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
streamer, wire, out = sys.argv[1], sys.argv[2], sys.argv[3]
RATE = 1250.0; FPS = 10; EXT = 5.0; W, H, HFOV = 640, 360, 70.0; FX = W / 2 / np.tan(np.radians(HFOV) / 2)
SKY = np.array([184, 209, 237], np.uint8)
# --- camera path (copy of fly_higher.py) ---
pcd = o3d.io.read_point_cloud("houses_7fps.ply"); traj = np.loadtxt("houses_7fps.txt")
ts0, pos, quat = traj[:, 0], traj[:, 1:4], traj[:, 4:8]
gaps = np.diff(ts0); ts = ts0[0] + np.concatenate([[0], np.cumsum(np.minimum(gaps, 3 * np.median(gaps)))])
plane, _ = pcd.voxel_down_sample(0.05).segment_plane(0.05, 3, 2000)
n, d0 = np.array(plane[:3]), plane[3]; n /= np.linalg.norm(plane[:3]); d0 /= np.linalg.norm(plane[:3])
if pos[0] @ n + d0 > 0: n, d0 = -n, -d0
up = -n
N = int((ts[-1] - ts[0] + EXT) * FPS); tq = ts[0] + np.arange(N) / FPS; tc = np.minimum(tq, ts[-1])
rots = Slerp(ts, Rotation.from_quat(quat))(tc).as_matrix()
Pc = np.stack([np.interp(tc, ts, pos[:, k]) for k in range(3)], 1)
vel = (pos[-1] - pos[-2]) / (ts[-1] - ts[-2]); Pc += np.maximum(tq - ts[-1], 0)[:, None] * vel
def look_at(c, target):
    z = target - c; z /= np.linalg.norm(z); x = np.cross(-up, z); x /= np.linalg.norm(x); y = np.cross(z, x)
    return np.stack([x, y, z], 1)
# --- receiver: decode the wire chunks, record arrival times ---
s = importlib.util.spec_from_file_location("st", streamer); m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
dec = m.make_decoder(); snaps, arrive, nbytes, t_done = [], [], [], ts[0]
chunks = sorted(f for f in os.listdir(wire) if f.startswith("chunk") and f.endswith(".bin"))
for k, f in enumerate(chunks):
    b = open(os.path.join(wire, f), "rb").read(); dec.apply(b); p2, c2, sp = dec.map()
    start = max(ts[k], t_done); t_done = start + len(b) / RATE
    snaps.append((np.asarray(p2, float), np.asarray(c2, np.uint8), float(sp))); arrive.append((start, t_done)); nbytes.append(len(b))
    print(f"chunk {k}: {len(b)} B, on air {start-ts[0]:.1f}-{t_done-ts[0]:.1f} s", flush=True)
K = len(snaps)
def render(pts, cols, R, c, splat):  # fly_higher.py numpy splat renderer
    q = (pts - c) @ R; k = q[:, 2] > 0.05; q, col = q[k], cols[k]
    u = (FX * q[:, 0] / q[:, 2] + W / 2).astype(np.int32); v = (FX * q[:, 1] / q[:, 2] + H / 2).astype(np.int32)
    k = (u >= 0) & (u < W - 1) & (v >= 0) & (v < H - 1); u, v, z, col = u[k], v[k], q[k, 2], col[k]
    o = np.argsort(-z); u, v, z, col = u[o], v[o], z[o], col[o]
    img = np.empty((H, W, 3), np.uint8); img[:] = SKY
    size = np.clip(np.ceil(FX * splat / z), 1, 24).astype(np.int32)
    for du in range(size.max()):
        for dv in range(size.max()):
            kk = size > max(du, dv); img[np.minimum(v[kk] + dv, H - 1), np.minimum(u[kk] + du, W - 1)] = col[kk]
    return img
try: font = ImageFont.load_default(size=15)
except TypeError: font = ImageFont.load_default()
ff = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
                       "-i", "-", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", out], stdin=subprocess.PIPE)
for i in range(N):
    t = tq[i]; R, c = rots[i], Pc[i]
    sdist = -(c @ n + d0) / (R[:, 2] @ n); target = c + max(sdist, 0.5) * R[:, 2]; Rv = look_at(c, target)
    got = [k for k in range(K) if arrive[k][1] <= t]
    img = render(*snaps[got[-1]][:2], Rv, c, snaps[got[-1]][2]) if got else np.tile(SKY, (H, W, 1))
    im = Image.fromarray(np.ascontiguousarray(img)); d = ImageDraw.Draw(im)
    d.rectangle([0, 0, W, 22], fill=(0, 0, 0)); rx = sum(nbytes[k] for k in got)
    cur = [k for k in range(K) if arrive[k][0] <= t < arrive[k][1]]
    partial = int((t - arrive[cur[0]][0]) * RATE) if cur else 0
    d.text((6, 4), f"10 kbit/s live  slot {len(got)-1 if got else '-'}/{K-1} shown  received {rx + partial:,} B  t = {t-ts[0]:4.1f} s", fill=(255, 255, 255), font=font)
    if cur:
        k = cur[0]; fr = (t - arrive[k][0]) / (arrive[k][1] - arrive[k][0])
        d.rectangle([6, H - 14, W - 6, H - 6], outline=(255, 255, 255)); d.rectangle([6, H - 14, 6 + int(fr * (W - 12)), H - 6], fill=(80, 220, 80))
        d.text((6, H - 32), f"receiving slot {k} ({nbytes[k]} B)", fill=(255, 255, 255), font=font)
    ff.stdin.write(np.asarray(im).tobytes())
ff.stdin.close(); ff.wait(); print("wrote", out, N, "frames")
