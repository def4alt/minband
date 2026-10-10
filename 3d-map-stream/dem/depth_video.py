"""Debug video for Depth Anything 3: how far out does the depth stay right?

Per frame (one view, no poses): DA3-Small and DA3Metric-Large depth, each scaled per frame to the SLAM map points it
sees (median ratio) and then to metres with the metres per SLAM unit of a metric fuse.py run; the panel title gives
DA3Metric's own metric error (README formula: focal * output / 300). The SLAM map points projected with the frame's
pose are the triangulated reference: predicted vs SLAM depth (log-log, y = x is right) and the depth down the centre
column. Contours on the frame are DA3-Small iso-distances; grey is DA3Metric's sky mask.

usage: python depth_video.py --frames runs/dem/houses/frames_full --slam runs/dem/houses/orbslam3 \
         --m-per-unit-from runs/dem/houses/orb_moge/meta.json --fx 642 --cx 480 --cy 269 --out runs/dem/houses/da3_depth.mp4
Run in the Depth-Anything-3 venv (KMP_DUPLICATE_LIB_OK=TRUE, see README). --cache keeps the depths in OUT.npz (~1 GB).
"""
import argparse, os, json, subprocess, time, numpy as np
from PIL import Image
from scipy.spatial.transform import Rotation

ap = argparse.ArgumentParser()
ap.add_argument("--frames", required=True); ap.add_argument("--slam", required=True); ap.add_argument("--out", required=True)
ap.add_argument("--m-per-unit-from", required=True, help="fuse.py meta.json of a metric run (MoGe2-Aerial): m_per_unit_median")
ap.add_argument("--fx", type=float, required=True); ap.add_argument("--cx", type=float, required=True); ap.add_argument("--cy", type=float, required=True)
ap.add_argument("--process-res", type=int, default=952); ap.add_argument("--fps", type=float, default=10)
ap.add_argument("--dmin", type=float, default=None); ap.add_argument("--dmax", type=float, default=None)
ap.add_argument("--stats-only", action="store_true", help="print the depth-bin table, no video")
ap.add_argument("--cache", action="store_true", help="keep/reuse the per-frame depths in OUT.npz (re-render without the models)")
a = ap.parse_args()

files = [l.split() for l in open(os.path.join(a.frames, "rgb.txt")) if not l.startswith("#")]
K = np.array([[a.fx, 0, a.cx], [0, a.fx, a.cy], [0, 0, 1.0]])
mpu = json.load(open(a.m_per_unit_from))["m_per_unit_median"]
traj = {round(r[0], 3): r for r in np.loadtxt(os.path.join(a.slam, "CameraTrajectory.txt")).reshape(-1, 8)}
mp = np.loadtxt(os.path.join(a.slam, "mappoints.txt")).reshape(-1, 3)

# --- depths (cached) ---
cache = os.path.splitext(a.out)[0] + ".npz"
if a.cache and os.path.exists(cache):
    c = np.load(cache); Ds, Dm, Cs, Sky, t_s, t_m = c["Ds"], c["Dm"], c["Cs"], c["Sky"], float(c["t_s"]), float(c["t_m"])
else:
    from depth import DA3, sync
    small, metric = DA3("da3small", a.process_res, K=K), DA3("da3metric", a.process_res, K=K)
    Ds, Dm, Cs, Sky, ts, tm = [], [], [], [], [], []
    for i, (t, f) in enumerate(files):
        img = np.asarray(Image.open(os.path.join(a.frames, f)).convert("RGB"))
        t0 = time.time(); Ds.append(small._run([img])[0]); sync(); ts.append(time.time() - t0); Cs.append(small.last_conf[0])
        t0 = time.time(); Dm.append(metric._run([img])[0]); sync(); tm.append(time.time() - t0)
        Sky.append(metric.last_sky[0] if metric.last_sky is not None else np.zeros(img.shape[:2], bool))
        if i % 20 == 0:
            print(f"{i}/{len(files)} small {np.median(ts)*1000:.0f} ms metric {np.median(tm)*1000:.0f} ms", flush=True)
    Ds, Dm, Cs = (np.stack(x).astype(np.float16) for x in (Ds, Dm, Cs)); Sky = np.stack(Sky).astype(bool)
    t_s, t_m = float(np.median(ts)), float(np.median(tm))
    if a.cache:
        np.savez(cache, Ds=Ds, Dm=Dm, Cs=Cs, Sky=Sky, t_s=t_s, t_m=t_m)
N, H, W = Ds.shape


def slam_points(t):
    """Map points in front of the frame's camera, inside the image: (u, v, depth in metres) or None."""
    r = traj.get(round(float(t), 3))
    if r is None:
        return None
    R = Rotation.from_quat(r[4:8]).as_matrix(); Pc = (mp - r[1:4]) @ R
    z = Pc[:, 2]; ok = z > 1e-6
    u = a.fx * Pc[ok, 0] / z[ok] + a.cx; v = a.fx * Pc[ok, 1] / z[ok] + a.cy
    k = (u >= 0) & (u < W - 1) & (v >= 0) & (v < H - 1)
    return u[k], v[k], z[ok][k] * mpu


# per frame and model: scale to the SLAM points in view (median ratio, robust), in metres; frames before SLAM
# initialises take the median scale
scale = np.full((2, N), np.nan); pts = []
for i, (t, _) in enumerate(files):
    p = slam_points(t); pts.append(p)
    if p is not None and len(p[0]) > 30:
        for j, D in enumerate((Ds, Dm)):
            scale[j, i] = np.median(p[2] / D[i][p[1].astype(int), p[0].astype(int)].astype(np.float32))
scale = np.where(np.isfinite(scale), scale, np.nanmedian(scale, 1, keepdims=True))
vis = ~Sky
sub = (Ds[:, ::8, ::8].astype(np.float32) * scale[0][:, None, None])[vis[:, ::8, ::8]]
dmin = a.dmin or float(np.percentile(sub, 0.5)); dmax = a.dmax or float(np.percentile(sub, 99.5))

# --- how far out the shape holds: predicted / SLAM depth per SLAM-depth bin, over all frames (after the per-frame scale)
rows = []
for i, p in enumerate(pts):
    if p is not None and len(p[0]):
        uu, vv = p[0].astype(int), p[1].astype(int)
        rows.append(np.stack([p[2], Ds[i][vv, uu].astype(np.float32) * scale[0, i], Dm[i][vv, uu].astype(np.float32) * scale[1, i]], 1))
rows = np.concatenate(rows); edges = np.geomspace(rows[:, 0].min(), rows[:, 0].max(), 9)
stats = []
print(f"{'SLAM depth, m':>16} {'n':>6}  {'DA3-Small pred/SLAM p10 p50 p90':>34}  {'DA3Metric pred/SLAM p10 p50 p90':>34}")
for lo, hi in zip(edges[:-1], edges[1:]):
    k = (rows[:, 0] >= lo) & (rows[:, 0] < hi)
    if k.sum() < 20:
        continue
    q = [np.percentile(rows[k, j] / rows[k, 0], [10, 50, 90]) for j in (1, 2)]
    stats.append(dict(lo=float(lo), hi=float(hi), n=int(k.sum()), small=q[0].tolist(), metric=q[1].tolist()))
    print(f"{lo:7.0f} - {hi:6.0f} {k.sum():6d}  {q[0][0]:10.2f} {q[0][1]:6.2f} {q[0][2]:6.2f}            {q[1][0]:10.2f} {q[1][1]:6.2f} {q[1][2]:6.2f}")
json.dump(dict(bins=stats, m_per_unit=mpu, metric_own_scale_short_by=float(np.median(scale[1])), small_ms=t_s * 1000,
               metric_ms=t_m * 1000), open(os.path.splitext(a.out)[0] + "_bins.json", "w"), indent=1)
if a.stats_only:
    raise SystemExit

# --- render: 2x2 at the frame size, piped to ffmpeg ---
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LogNorm
from matplotlib.ticker import FixedLocator, NullLocator, FuncFormatter
levels = [l for l in (25, 50, 100, 200, 300, 500, 1000, 2000, 3000, 5000, 10000) if dmin < l < dmax]
norm = LogNorm(dmin, dmax); cmap = plt.get_cmap("turbo").copy(); cmap.set_bad("0.25")
dpi = 100; fig = plt.figure(figsize=(2 * W / dpi, 2 * H / dpi + 0.4), dpi=dpi)
ff = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{2*W}x{2*H + 40}",
                       "-r", str(a.fps), "-i", "-", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", a.out],
                      stdin=subprocess.PIPE)
yy = np.arange(H); cu = W // 2


def metres(axis):  # log axis with the contour levels as plain-number ticks, no minor labels
    axis.set_major_locator(FixedLocator(levels)); axis.set_minor_locator(NullLocator())
    axis.set_major_formatter(FuncFormatter(lambda x, _: f"{x:.0f}"))

for i, (t, f) in enumerate(files):
    img = np.asarray(Image.open(os.path.join(a.frames, f)).convert("RGB"))
    Dsm, Dmi = Ds[i].astype(np.float32) * scale[0, i], Dm[i].astype(np.float32) * scale[1, i]
    fig.clf()
    fig.suptitle(f"t {float(t):6.1f} s   DA3-Small {t_s*1000:.0f} ms, DA3Metric-Large {t_m*1000:.0f} ms per frame (M4, {a.process_res} px)   "
                 f"SLAM scale for Small: {'fit' if pts[i] is not None else 'median (no pose)'}", fontsize=12, y=0.995)
    ax = fig.add_axes([0, 0.5, 0.5, 0.48]); ax.imshow(img); ax.axis("off")
    cs = ax.contour(np.where(vis[i], Dsm, np.nan), levels=levels, colors="w", linewidths=0.8)
    ax.clabel(cs, fmt=lambda x: f"{x:.0f} m", fontsize=8)
    ax.set_title("frame + DA3-Small iso-distance (m)", fontsize=10, y=0.0, color="w", backgroundcolor="k")
    for j, (D, title) in enumerate([(np.where(vis[i], Dsm, np.nan), "DA3-Small depth, m (SLAM-scaled per frame)"),
                                    (np.where(vis[i], Dmi, np.nan), f"DA3Metric-Large, SLAM-scaled (its own metres x{scale[1, i]:.0f} short; grey = sky)")]):
        ax = fig.add_axes([0.5 * (j == 0), 0.02 + 0.48 * (j == 0), 0.5, 0.48] if j == 0 else [0, 0.02, 0.5, 0.48])
        im = ax.imshow(D, cmap=cmap, norm=norm); ax.axis("off")
        ax.set_title(title, fontsize=10, y=0.0, color="w", backgroundcolor="k")
    cax = fig.add_axes([0.512, 0.08, 0.01, 0.38]); cb = fig.colorbar(im, cax=cax)
    metres(cb.ax.yaxis); cb.ax.tick_params(labelsize=8); cb.ax.set_title("m", fontsize=8)
    # predicted vs SLAM depth, and the centre column
    ax = fig.add_axes([0.585, 0.08, 0.19, 0.38]); p = pts[i]
    if p is not None and len(p[0]):
        uu, vv, zz = p[0].astype(int), p[1].astype(int), p[2]
        ax.scatter(zz, Dsm[vv, uu], s=3, alpha=0.5, label="DA3-Small (scaled)")
        ax.scatter(zz, Dmi[vv, uu], s=3, alpha=0.5, label="DA3Metric-Large (scaled)")
    ax.plot([dmin, dmax], [dmin, dmax], "k--", lw=0.8); ax.set_xscale("log"); ax.set_yscale("log")
    metres(ax.xaxis); metres(ax.yaxis)
    ax.set_xlim(dmin, dmax); ax.set_ylim(dmin, dmax); ax.set_xlabel("SLAM map point depth, m"); ax.set_ylabel("predicted, m")
    ax.legend(fontsize=7, loc="upper left"); ax.set_title("map points in view", fontsize=9)
    ax = fig.add_axes([0.80, 0.08, 0.18, 0.38])
    ax.plot(Dsm[:, cu], yy, label="DA3-Small"); ax.plot(np.where(Sky[i][:, cu], np.nan, Dmi[:, cu]), yy, label="DA3Metric")
    if p is not None:
        k = np.abs(p[0] - cu) < 20
        ax.scatter(p[2][k], p[1][k], s=6, c="k", label="SLAM pts (|u-c|<20)")
    ax.set_xscale("log"); metres(ax.xaxis); ax.set_xlim(dmin, dmax); ax.set_ylim(H, 0); ax.set_xlabel("depth, m"); ax.set_ylabel("image row")
    ax.legend(fontsize=7); ax.set_title("centre column", fontsize=9)
    fig.canvas.draw(); ff.stdin.write(np.asarray(fig.canvas.buffer_rgba()).tobytes())
ff.stdin.close(); ff.wait()
print(f"{a.out}: {N} frames, depth range {dmin:.0f}-{dmax:.0f} m, m/unit {mpu:.1f}, small {t_s*1000:.0f} ms, metric {t_m*1000:.0f} ms, "
      f"DA3Metric own scale short by x{np.median(scale[1]):.1f} (p10 {np.percentile(scale[1], 10):.1f}, p90 {np.percentile(scale[1], 90):.1f})")
