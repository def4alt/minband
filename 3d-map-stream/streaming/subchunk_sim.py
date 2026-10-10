"""Smaller packets = more frequent updates: measure overhead and the operator's view over time.
Same per-keyframe plan as stream_lod_conf (CONF pruning, budget fit), but each keyframe's new voxels are split into
packets of ~PKT bytes, ordered coarse band first and nearest-to-drone first, transmitted back to back at 1250 B/s.
The live view is scored every second (map = packets fully received by then).  usage: python subchunk_sim.py"""
import os, sys, struct, time, numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import quality as Q, live_eval as LE, lod_common as L, codec_lod_bands as LB, stream_lod_conf as S
RATE = 1250.0; PKTS = [7000, 3500, 1750, 900]
P, C, traj = Q.load_ref(); K = len(traj); ts = traj[:, 0]

# ---- planning (identical to stream_lod_conf.Encoder.update) but keep the chosen voxels per band ----
enc = S.Encoder(RATE, P, C); plans = []
for k in range(K):
    dt = (ts[k + 1] - ts[k]) if k + 1 < K else float(np.median(np.diff(ts))); budget = RATE * dt; tr = traj[: k + 1]
    if enc.R is None:
        d = np.linalg.norm(P - tr[0, 1:4], axis=1); m0 = d < np.percentile(d, 50)
        enc.R = L.ground_frame(P[m0], tr); enc.Q3 = P @ enc.R.T; enc.o = enc.Q3.min(0) - 50 / L.M
        hdr = enc.R.astype(np.float32).tobytes() + enc.o.astype(np.float32).tobytes() + struct.pack("<fB", S.SPL, len(S.LEVELS)) + S.LEVELS.astype(np.float32).tobytes()
    else: hdr = b""
    sub = tr if len(tr) > 1 else np.vstack([tr[0], tr[0] + [1e-3, 0, 0, 0, 0, 0, 0, 0]])
    z = LB.zeff(P, sub, "min"); far = ~np.isfinite(z); z[far] = np.linalg.norm(P[far] - tr[-1, 1:4], axis=1) * L.M
    def plan(alpha):
        want = alpha * z; band = np.abs(np.log(want)[:, None] - np.log(S.LEVELS)[None]).argmin(1); res = []; nbytes = 0
        for bi, v in enumerate(enc.vb):
            m = band == bi
            if not m.any(): res.append(None); continue
            u, col, cnt, inv = L.voxelize(enc.Q3[m], C[m], v, enc.o); keep = cnt >= S.MINCNT
            zs = np.zeros(len(u)); np.add.at(zs, inv, z[m]); zs /= cnt; dens = cnt * zs ** 2
            keep &= dens >= S.CONF * np.median(dens[cnt >= S.MINCNT]); u, col = u[keep], col[keep]
            kk = S.keyf(u); fresh = np.array([x not in enc.sent[bi] for x in kk], bool); u, col, kk = u[fresh], col[fresh], kk[fresh]
            if len(u) == 0: res.append(None); continue
            nbytes += len(L.cb.encode(u.astype(np.int32), col)) + 5; res.append((u, col, kk))
        return nbytes, res
    lo, hi = np.log(enc.alpha / 2), np.log(enc.alpha * 2); best = None
    for _ in range(6):
        mid = (lo + hi) / 2; nb, res = plan(np.exp(mid))
        if nb + len(hdr) <= budget: best, hi = (res, mid), mid
        else: lo = mid
    while best is None:
        hi += np.log(1.5); nb, res = plan(np.exp(hi))
        if nb + len(hdr) <= budget or hi > 0: best = (res, hi)
    res, a = best; enc.alpha = float(np.exp(a))
    for bi, x in enumerate(res):
        if x is not None: enc.sent[bi].update(x[2].tolist())
    drone = tr[-1, 1:4] @ enc.R.T; plans.append((ts[k], hdr, res, drone)); print(f"plan kf {k}: alpha {enc.alpha:.4f}", flush=True)

# ---- packetize + transmit timeline + score ----
times = np.arange(0, ts[-1] - ts[0] + 5.0 + 1e-6, 1.0); refs = {}
for t in times:
    R_, c_ = LE.pose_at(traj, ts[0] + t); refs[t] = Q.render(P, C, R_, c_, Q.REF_SPLAT) + (R_, c_)
print(f"\n{'packet':>7} {'packets':>7} {'total KB':>8} {'overhead':>8} {'first img':>9} {'PSNR@3s':>7} {'@6s':>6} {'@12s':>6} {'@18s':>6} {'mean':>6} {'final':>6}")
base_total = None
for PKT in PKTS:
    packets = []; t_done = ts[0]
    for tk, hdr, res, drone in plans:
        recs = []
        for bi in reversed(range(len(res))):  # coarse band first
            if res[bi] is None: continue
            u, col, kk = res[bi]; cen = (u + 0.5) * enc.vb[bi] + enc.o; order = np.argsort(np.linalg.norm(cen - drone, axis=1))
            u, col = u[order], col[order]
            n = max(1, int(np.ceil((len(L.cb.encode(u.astype(np.int32), col)) + 5) / PKT)))
            for g in np.array_split(np.arange(len(u)), n):
                s = L.cb.encode(u[g].astype(np.int32), col[g]); recs.append(struct.pack("<BI", bi, len(s)) + s)
        for i, r in enumerate(recs):
            b = (hdr if i == 0 else b"") + r; start = max(tk, t_done); t_done = start + len(b) / RATE; packets.append((t_done, b))
    total = sum(len(b) for _, b in packets); base_total = base_total or total
    dec = S.Decoder(); j = 0; ps = {}; first = None
    for t in times:
        while j < len(packets) and packets[j][0] <= ts[0] + t: dec.apply(packets[j][1]); j += 1
        if dec.vox is None or not any(dec.vox): ps[t] = (0.0, 1.0); continue
        if first is None: first = packets[j - 1][0] - ts[0]
        p2, c2, sp = dec.map(); ri, rf, R_, c_ = refs[t]; ci, cf = Q.render(p2, c2, R_, c_, sp)
        mse = ((Q.half(ri) - Q.half(ci)) ** 2).mean(); ps[t] = (10 * np.log10(255 ** 2 / mse), (rf & ~cf).sum() / max(rf.sum(), 1))
    g = lambda t: ps[min(times, key=lambda x: abs(x - t))][0]
    print(f"{PKT:7d} {len(packets):7d} {total/1e3:8.1f} {100*(total/base_total-1):7.1f}% {first:8.1f}s {g(3):7.2f} {g(6):6.2f} {g(12):6.2f} {g(18):6.2f} {np.mean([v[0] for v in ps.values()]):6.2f} {ps[times[-1]][0]:6.2f}", flush=True)
