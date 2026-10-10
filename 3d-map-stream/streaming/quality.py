"""Render-based quality benchmark for map codecs at a bit budget.

A codec .py defines:
    encode(P float64[N,3] world pts, rgb uint8[N,3], traj float64[K,8] TUM keyframes) -> bytes
    decode(bytes) -> (pts float[M,3] world, rgb uint8[M,3], splat float)   # splat = point size in world units
Anything sent must be inside the bytes (decoder gets nothing else). Decoder may densify freely.
Score: render 12 views along the flight (+5 s extension, like the demo video) at 640x360 and
compare against the uncompressed reconstruction: PSNR (higher better), SSIM, hole% = pixels
that are background in the candidate but surface in the reference.
Budget: 20 kbit/s * flight duration (23.1 s) = 57.8 KB.  Also report time.
usage: python3 quality.py codec.py [--save-views DIR]
"""
import sys, time, importlib.util, argparse, numpy as np, open3d as o3d
from scipy.spatial.transform import Rotation, Slerp
W, H, HFOV = 640, 360, 70.0
FX = W / 2 / np.tan(np.radians(HFOV) / 2)
SKY = np.array([184, 209, 237], np.uint8)
REF_SPLAT = 0.25 / 40.0  # reference drawn hole-free with 0.25 m splats

def half(img):  # compare at half resolution: tolerant to pixel-level splat jitter
    i = img.astype(float); return (i[0::2, 0::2] + i[1::2, 0::2] + i[0::2, 1::2] + i[1::2, 1::2]) / 4

def load_ref():
    p = o3d.io.read_point_cloud("houses_7fps.ply")
    return np.asarray(p.points), (np.asarray(p.colors) * 255).round().astype(np.uint8), np.loadtxt("houses_7fps.txt")

def views(traj, n=12, extend=5.0):
    ts, pos, q = traj[:, 0], traj[:, 1:4], traj[:, 4:8]
    tq = np.linspace(ts[0], ts[-1] + extend, n); tc = np.minimum(tq, ts[-1])
    R = Slerp(ts, Rotation.from_quat(q))(tc).as_matrix()
    P = np.stack([np.interp(tc, ts, pos[:, k]) for k in range(3)], 1)
    vel = (pos[-1] - pos[-2]) / (ts[-1] - ts[-2]); P += np.maximum(tq - ts[-1], 0)[:, None] * vel
    return R, P

def render(pts, cols, R, c, splat):
    q = (pts - c) @ R; m = q[:, 2] > 0.05; q, col = q[m], cols[m]
    u = (FX * q[:, 0] / q[:, 2] + W / 2).astype(np.int32); v = (FX * q[:, 1] / q[:, 2] + H / 2).astype(np.int32)
    k = (u >= 0) & (u < W) & (v >= 0) & (v < H); u, v, z, col = u[k], v[k], q[k, 2], col[k]
    o = np.argsort(-z); u, v, z, col = u[o], v[o], z[o], col[o]
    img = np.empty((H, W, 3), np.uint8); img[:] = SKY; fg = np.zeros((H, W), bool)
    size = np.clip(np.ceil(FX * splat / z), 2, 40).astype(np.int32) if splat > 0 else np.full(len(z), 2, np.int32)
    for du in range(size.max()):
        for dv in range(size.max()):
            kk = size > max(du, dv); uu = np.minimum(u[kk] + du, W - 1); vv = np.minimum(v[kk] + dv, H - 1)
            img[vv, uu] = col[kk]; fg[vv, uu] = True
    return img, fg

def ssim(a, b):
    from scipy.ndimage import uniform_filter
    a = a.astype(float).mean(2); b = b.astype(float).mean(2); C1, C2 = 6.5, 58.5
    ma, mb = uniform_filter(a, 7), uniform_filter(b, 7)
    va = uniform_filter(a * a, 7) - ma ** 2; vb = uniform_filter(b * b, 7) - mb ** 2; cv = uniform_filter(a * b, 7) - ma * mb
    return (((2 * ma * mb + C1) * (2 * cv + C2)) / ((ma ** 2 + mb ** 2 + C1) * (va + vb + C2))).mean()

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("codec"); ap.add_argument("--save-views"); a = ap.parse_args()
    P, C, traj = load_ref(); dur = traj[-1, 0] - traj[0, 0]; budget = 20e3 * dur / 8
    s = importlib.util.spec_from_file_location("codec", a.codec); m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
    t0 = time.time(); blob = m.encode(P.copy(), C.copy(), traj.copy()); t1 = time.time()
    p2, c2, splat = m.decode(blob); t2 = time.time()
    p2, c2 = np.asarray(p2, float), np.asarray(c2, np.uint8)
    Rs, Ps = views(traj); ps, ss, hs = [], [], []
    for i, (R, c) in enumerate(zip(Rs, Ps)):
        ri, rf = render(P, C, R, c, REF_SPLAT); ci, cf = render(p2, c2, R, c, splat)
        mse = ((half(ri) - half(ci)) ** 2).mean(); ps.append(10 * np.log10(255 ** 2 / max(mse, 1e-9)))
        ss.append(ssim(half(ri), half(ci))); hs.append((rf & ~cf).sum() / max(rf.sum(), 1))
        if a.save_views:
            import os; from PIL import Image; os.makedirs(a.save_views, exist_ok=True)
            Image.fromarray(np.vstack([ri, ci])).save(f"{a.save_views}/v{i:02d}.png")
    kb = len(blob) / 1e3; rate = 8 * len(blob) / dur / 1e3
    print(f"{a.codec}: {kb:.1f} KB = {rate:.1f} kbit/s {'(within 20k budget)' if len(blob) <= budget else '(OVER BUDGET)'} | "
          f"PSNR {np.mean(ps):.2f} dB | SSIM {np.mean(ss):.3f} | holes {100*np.mean(hs):.1f}% | {len(p2):,} pts | enc {t1-t0:.1f}s dec {t2-t1:.1f}s")
