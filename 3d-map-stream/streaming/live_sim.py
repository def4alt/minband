"""Simulate LIVE streaming of the LOD map with refinement.
Per keyframe k (causal: only poses 0..k known): desired band per point = snap(alpha * dist to known path);
send voxels of that band not yet sent (new areas AND refinements of areas sent coarser before);
alpha bisected so the chunk fits RATE x keyframe interval. Receiver keeps the finest band per footprint.
usage: RATE_KBIT=10 python live_sim.py"""
import os, sys, time, struct, numpy as np
sys.path.insert(0, "."); import lod_common as L, codec_lod_bands as LB
Q = L.Q; RATE = float(os.environ.get("RATE_KBIT", 10)) * 1e3 / 8
levels = np.array([float(x) for x in os.environ.get("LEVELS", "0.5,1.0,2.0,4.0").split(",")]); spl = float(os.environ.get("SPL", 1.0))
MINCNT = int(os.environ.get("MINCNT", 2))
P, C, traj = Q.load_ref(); K = len(traj); ts = traj[:, 0]
m0 = np.linalg.norm(P - traj[0, 1:4], axis=1) < np.percentile(np.linalg.norm(P - traj[0, 1:4], axis=1), 50)
R = L.ground_frame(P[m0], traj); Q3 = P @ R.T; o = Q3.min(0) - 50 / L.M   # frame fixed at keyframe 0 (+margin)
vb = [vm / L.M for vm in levels]
keyf = lambda u: (u[:, 0].astype(np.int64) << 42) | (u[:, 1].astype(np.int64) << 21) | u[:, 2].astype(np.int64)
sent = [dict() for _ in levels]   # per band: key -> (vox, rgb)

def chunk(alpha, z):
    want = alpha * z; band = np.abs(np.log(want)[:, None] - np.log(levels)[None]).argmin(1)
    out, newv = b"", []
    for bi, v in enumerate(vb):
        m = band == bi
        if not m.any(): newv.append(None); continue
        u, col, cnt, _ = L.voxelize(Q3[m], C[m], v, o); u, col = u[cnt >= MINCNT], col[cnt >= MINCNT]
        if len(u) == 0: newv.append(None); continue
        kk = keyf(u); fresh = np.array([k not in sent[bi] for k in kk]); u, col, kk = u[fresh], col[fresh], kk[fresh]
        if len(u) == 0: newv.append(None); continue
        s = L.cb.encode(u.astype(np.int32), col); out += struct.pack("<BI", bi, len(s)) + s; newv.append((u, col, kk))
    return out, newv

tot = 0; print(f"{'kf':>2} {'dt s':>5} {'bytes':>6} {'budget':>6} {'alpha':>6} {'enc s':>5}  new voxels per band {list(levels)}")
for k in range(K):
    dt = (ts[k + 1] - ts[k]) if k + 1 < K else np.median(np.diff(ts)); budget = RATE * dt
    sub = traj[: k + 1] if k > 0 else np.vstack([traj[0], traj[0] + [1e-3, 0, 0, 0, 0, 0, 0, 0]])
    z = LB.zeff(P, sub, "min"); far = ~np.isfinite(z); z[far] = np.linalg.norm(P[far] - traj[k, 1:4], axis=1) * L.M
    t0 = time.time(); lo, hi = np.log(0.004), np.log(0.2); best = None
    for _ in range(7):
        mid = (lo + hi) / 2; b, nv = chunk(np.exp(mid), z)
        if len(b) <= budget: best, hi = (b, nv), mid
        else: lo = mid
    if best is None: best = chunk(np.exp(hi), z)
    b, nv = best; tot += len(b)
    for bi, x in enumerate(nv):
        if x is not None:
            for u, c, kk in zip(*x): sent[bi][kk] = (u, c)
    print(f"{k:2d} {dt:5.1f} {len(b):6d} {budget:6.0f} {np.exp(hi):6.4f} {time.time()-t0:5.1f}  {[0 if x is None else len(x[2]) for x in nv]}")
print(f"total {tot/1e3:.1f} KB = {8*tot/(ts[-1]-ts[0])/1e3:.1f} kbit/s over {ts[-1]-ts[0]:.1f} s")

# receiver: finest band wins inside its footprint
bands = []
for bi, v in enumerate(vb):
    if sent[bi]: u = np.array([x[0] for x in sent[bi].values()]); c = np.array([x[1] for x in sent[bi].values()]); bands.append([bi, v, u, c])
for i, (bi, v, u, c) in enumerate(bands):
    for bj, vf, uf, cf in bands:
        if vf < v:
            r = int(round(v / vf)); cov = set(keyf(uf // r).tolist()); keep = np.array([k not in cov for k in keyf(u).tolist()]); u, c = u[keep], c[keep]
    bands[i] = [bi, v, u, c]
vmin = min(b[1] for b in bands); pts, cols = [], []
for bi, v, u, c in bands:
    kk = int(np.ceil(v / vmin - 1e-3)); g = (np.arange(kk) + 0.5) / kk
    flat = np.stack(np.meshgrid(g, [0.5], g, indexing="ij"), -1).reshape(-1, 3)
    pts.append(((u[:, None, :] + flat[None]) * v + o).reshape(-1, 3)); cols.append(np.repeat(c, len(flat), 0))
    print(f"band {levels[bi]} m: {len(u):,} voxels kept on receiver")
p2, c2, splat = np.concatenate(pts) @ R, np.concatenate(cols), vmin * spl
Rs, Ps = Q.views(traj); ps, ss, hs = [], [], []
for R_, c_ in zip(Rs, Ps):
    ri, rf = Q.render(P, C, R_, c_, Q.REF_SPLAT); ci, cf = Q.render(p2, c2, R_, c_, splat)
    mse = ((Q.half(ri) - Q.half(ci)) ** 2).mean(); ps.append(10 * np.log10(255 ** 2 / max(mse, 1e-9)))
    ss.append(Q.ssim(Q.half(ri), Q.half(ci))); hs.append((rf & ~cf).sum() / max(rf.sum(), 1))
print(f"LIVE final map: PSNR {np.mean(ps):.2f} dB | SSIM {np.mean(ss):.3f} | holes {100*np.mean(hs):.1f}% | {len(p2):,} pts")
