"""Demo video (stream_best_rec record format; copy of demo_live_render.py using the streamer's split_records): LEFT = full reconstruction, RIGHT = what the operator has received over a 10 kbit/s link.
Chunk k is transmitted from keyframe time t_k (back-to-back if the previous one is still sending) at 1250 B/s;
the right map only contains chunks that have fully arrived.  usage: python demo_live_render_rec.py stream_best_rec.py out.mp4"""
import sys, os, subprocess, importlib.util, numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import quality as Q, live_eval as LE
from PIL import Image, ImageDraw, ImageFont
streamer, out = sys.argv[1], sys.argv[2]; RATE = 1250.0; FPS = 10; EXT = 5.0
W, H, FX, SKY = Q.W, Q.H, Q.FX, Q.SKY
P, C, traj = Q.load_ref(); ts = traj[:, 0]; K = len(traj)
s = importlib.util.spec_from_file_location("st", streamer); m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
enc, dec = m.make_encoder(RATE, P.copy(), C.copy()), m.make_decoder()
snaps, arrive, nbytes, t_done = [], [], [], ts[0]
def records(b, first):  # split a chunk into its records (stream_best_rec format: header rides with the first one)
    return m.split_records(b, first)
for k in range(K):
    dt = (ts[k + 1] - ts[k]) if k + 1 < K else float(np.median(np.diff(ts)))
    b = enc.update(k, traj[: k + 1].copy(), dt)
    for r in records(b, k == 0):  # receiver applies every record as soon as it has fully arrived
        dec.apply(r); p2, c2, sp = dec.map()
        start = max(ts[k], t_done); t_done = start + len(r) / RATE
        snaps.append((np.asarray(p2, float), np.asarray(c2, np.uint8), sp)); arrive.append((start, t_done)); nbytes.append(len(r))
        print(f"chunk {k} record: {len(r)} B, sent {start-ts[0]:.1f}-{t_done-ts[0]:.1f} s", flush=True)
K = len(snaps)

def render(pts, cols, R, c, splat):
    q = (pts - c) @ R; k = q[:, 2] > 0.05; q, col = q[k], cols[k]
    u = (FX * q[:, 0] / q[:, 2] + W / 2).astype(np.int32); v = (FX * q[:, 1] / q[:, 2] + H / 2).astype(np.int32)
    k = (u >= 0) & (u < W - 1) & (v >= 0) & (v < H - 1); u, v, z, col = u[k], v[k], q[k, 2], col[k]
    o = np.argsort(-z); u, v, z, col = u[o], v[o], z[o], col[o]
    img = np.empty((H, W, 3), np.uint8); img[:] = SKY
    size = np.clip(np.ceil(FX * splat / z), 2, 24).astype(np.int32) if splat else np.full(len(z), 2, np.int32)
    for du in range(size.max()):
        for dv in range(size.max()):
            kk = size > max(du, dv); img[np.minimum(v[kk] + dv, H - 1), np.minimum(u[kk] + du, W - 1)] = col[kk]
    return img

try: font = ImageFont.load_default(size=15)
except TypeError: font = ImageFont.load_default()
N = int((ts[-1] - ts[0] + EXT) * FPS)
ff = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{2*W}x{H}", "-r", str(FPS),
                       "-i", "-", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", out], stdin=subprocess.PIPE)
for i in range(N):
    t = ts[0] + i / FPS; R, c = LE.pose_at(traj, t)
    left = render(P, C, R, c, 0)
    got = [k for k in range(K) if arrive[k][1] <= t]
    right = (lambda p_, c_, s_: render(p_, c_, R, c, s_))(*snaps[got[-1]]) if got else np.tile(SKY, (H, W, 1))
    im = Image.fromarray(np.hstack([left, right])); d = ImageDraw.Draw(im)
    d.rectangle([0, 0, 2 * W, 22], fill=(0, 0, 0)); kb = sum(nbytes[k] for k in got) / 1e3
    d.text((6, 4), "FULL RECONSTRUCTION (7.8 MB)", fill=(255, 255, 255), font=font)
    cur = [k for k in range(K) if arrive[k][0] <= t < arrive[k][1]]
    msg = f"LIVE over 10 kbit/s: {len(got)}/{K} packets received, {kb:.1f} KB   t = {t-ts[0]:4.1f} s"
    d.text((W + 6, 4), msg, fill=(255, 255, 255), font=font)
    if cur:  # transmission progress bar of the chunk in flight
        k = cur[0]; f = (t - arrive[k][0]) / (arrive[k][1] - arrive[k][0])
        d.rectangle([W + 6, H - 14, 2 * W - 6, H - 6], outline=(255, 255, 255)); d.rectangle([W + 6, H - 14, W + 6 + int(f * (W - 12)), H - 6], fill=(80, 220, 80))
        d.text((W + 6, H - 32), f"receiving packet {k} ({nbytes[k]} B)", fill=(255, 255, 255), font=font)
    ff.stdin.write(np.asarray(im).tobytes())
ff.stdin.close(); ff.wait(); print("wrote", out, N, "frames")
