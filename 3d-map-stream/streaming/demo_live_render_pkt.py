"""Demo video with RECORD-level arrival: LEFT = full reconstruction, RIGHT = what the operator has received.
Same planning as live_eval_pkt (--slot s planning slots with a token bucket); each chunk is sent as its records back to
back at rate B/s and the right map is refreshed as soon as a record has fully arrived (several times per second).
usage: python demo_live_render_pkt.py stream_x.py out.mp4 [rate_Bps=2500] [slot_s=1]"""
import sys, os, subprocess, importlib.util, numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import quality as Q, live_eval as LE
from PIL import Image, ImageDraw, ImageFont
streamer, out = sys.argv[1], sys.argv[2]; RATE = float(sys.argv[3]) if len(sys.argv) > 3 else 2500.0; SLOT = float(sys.argv[4]) if len(sys.argv) > 4 else 1.0; FPS = 10; EXT = 5.0
W, H, FX, SKY = Q.W, Q.H, Q.FX, Q.SKY
P, C, traj = Q.load_ref(); ts = traj[:, 0]
s = importlib.util.spec_from_file_location("st", streamer); m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
split = getattr(m, "split_records", None) or (lambda b, first: [b])
enc, dec = m.make_encoder(RATE, P.copy(), C.copy()), m.make_decoder()
nslot = int(np.ceil((ts[-1] - ts[0] + float(np.median(np.diff(ts)))) / SLOT)); bucket = 0.0
snaps, arrive, nbytes, t_done = [], [], [], ts[0]
for k in range(nslot):
    tk = ts[0] + k * SLOT; nkf = max(1, int(np.searchsorted(ts, tk, side="right"))); bucket += RATE * SLOT
    b = enc.update(k, traj[:nkf].copy(), bucket / RATE); bucket -= len(b)
    for r in split(b, k == 0):
        dec.apply(r); p2, c2, sp = dec.map()
        start = max(tk, t_done); t_done = start + len(r) / RATE
        snaps.append((np.asarray(p2, float), np.asarray(c2, np.uint8), sp)); arrive.append((start, t_done)); nbytes.append(len(r))
    print(f"slot {k}: {len(b)} B in {len(split(b, k == 0))} records, done at t={t_done-ts[0]:.2f} s", flush=True)
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
    d.text((6, 4), "FULL RECONSTRUCTION (7.8 MB)", fill=(255, 255, 255), font=font); KB_S = RATE * 8 / 1000
    cur = [k for k in range(K) if arrive[k][0] <= t < arrive[k][1]]
    msg = f"LIVE over {KB_S:g} kbit/s: {len(got)}/{K} packets received, {kb:.1f} KB   t = {t-ts[0]:4.1f} s"
    d.text((W + 6, 4), msg, fill=(255, 255, 255), font=font)
    if cur:
        k = cur[0]; f = (t - arrive[k][0]) / (arrive[k][1] - arrive[k][0])
        d.rectangle([W + 6, H - 14, 2 * W - 6, H - 6], outline=(255, 255, 255)); d.rectangle([W + 6, H - 14, W + 6 + int(f * (W - 12)), H - 6], fill=(80, 220, 80))
        d.text((W + 6, H - 32), f"receiving packet {k} ({nbytes[k]} B)", fill=(255, 255, 255), font=font)
    ff.stdin.write(np.asarray(im).tobytes())
ff.stdin.close(); ff.wait(); print("wrote", out, N, "frames")
