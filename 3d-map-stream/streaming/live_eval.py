"""LIVE streaming harness (the benchmark for the live map stream).
usage: python live_eval.py stream_x.py [--rate 10] [--views DIR] [--json out.json]

A streamer module must define:
  make_encoder(rate_Bps, P, C) -> enc;  enc.update(k, traj_upto_k, dt_slot) -> bytes
      chunk for transmission slot k (duration dt_slot seconds); MUST be <= rate_Bps*dt_slot bytes.
      P float64[N,3] world points, C uint8[N,3]: the SLAM map known to the sender (here the whole cloud,
      which is visible from keyframe 0 anyway).  CAUSAL RULE: at update k only traj[:k+1] is passed; the
      encoder must not use any other pose information (no future path, no +5 s extension).
  make_decoder() -> dec;  dec.apply(chunk_bytes);  dec.map() -> (pts float[M,3] world, rgb uint8[M,3], splat float)
      The decoder may use ONLY the chunk bytes (no files, no globals from the encoder). Densification is free.
Metrics:
  per slot: bytes, budget use, encode seconds
  LIVE view: after chunks 0..k are applied, render from the pose at t_k + dt_k (what the operator sees when the
             next keyframe arrives) and compare with the reference render -> PSNR / SSIM / holes, averaged over slots
  FINAL map: the 12 standard views of quality.py (incl. +5 s extension) on the map after all chunks.
"""
import sys, os, time, json, argparse, importlib.util, numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import quality as Q
from scipy.spatial.transform import Rotation, Slerp

def pose_at(traj, t):
    ts, pos, q = traj[:, 0], traj[:, 1:4], traj[:, 4:8]
    tc = min(max(t, ts[0]), ts[-1]); R = Slerp(ts, Rotation.from_quat(q))([tc]).as_matrix()[0]
    c = np.array([np.interp(tc, ts, pos[:, k]) for k in range(3)])
    if t > ts[-1]: c = c + (pos[-1] - pos[-2]) / (ts[-1] - ts[-2]) * (t - ts[-1])
    return R, c

def score(P, C, p2, c2, splat, R, c):
    ri, rf = Q.render(P, C, R, c, Q.REF_SPLAT); ci, cf = Q.render(p2, c2, R, c, splat)
    mse = ((Q.half(ri) - Q.half(ci)) ** 2).mean()
    return 10 * np.log10(255 ** 2 / max(mse, 1e-9)), Q.ssim(Q.half(ri), Q.half(ci)), (rf & ~cf).sum() / max(rf.sum(), 1), ri, ci

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("streamer"); ap.add_argument("--rate", type=float, default=10.0, help="kbit/s")
    ap.add_argument("--views"); ap.add_argument("--json")
    ap.add_argument("--slot", type=float, default=0.0, help="transmission slot length in s (default 0 = one slot per keyframe)"); a = ap.parse_args()
    rate = a.rate * 1e3 / 8
    P, C, traj = Q.load_ref(); K = len(traj); ts = traj[:, 0]
    s = importlib.util.spec_from_file_location("streamer", a.streamer); m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
    t0 = time.time(); enc = m.make_encoder(rate, P.copy(), C.copy()); dec = m.make_decoder(); tinit = time.time() - t0
    rows, live = [], []
    if a.views: os.makedirs(a.views, exist_ok=True)
    if a.slot > 0:  # sub-keyframe slots: slot k starts at t0 + k*slot and knows the keyframes seen by then
        nslot = int(np.ceil((ts[-1] - ts[0] + float(np.median(np.diff(ts)))) / a.slot))
        slots = [(ts[0] + i * a.slot, a.slot, max(1, int(np.searchsorted(ts, ts[0] + i * a.slot, side="right")))) for i in range(nslot)]
    else:
        slots = [(ts[k], (ts[k + 1] - ts[k]) if k + 1 < K else float(np.median(np.diff(ts))), k + 1) for k in range(K)]
    bucket, t_done, snaps = 0.0, ts[0], []
    for k, (tk, dt, nkf) in enumerate(slots):
        if a.slot > 0: bucket += rate * dt; dt_eff = bucket / rate   # token bucket: unused bytes roll over
        else: dt_eff = dt
        budget = rate * dt_eff
        t1 = time.time(); chunk = enc.update(k, traj[:nkf].copy(), dt_eff); tenc = time.time() - t1
        if a.slot > 0: bucket -= len(chunk)
        t2 = time.time(); dec.apply(chunk); p2, c2, splat = dec.map(); tdec = time.time() - t2
        p2, c2 = np.asarray(p2, float), np.asarray(c2, np.uint8)
        t_start = max(tk, t_done); t_done = t_start + len(chunk) / rate; snaps.append((t_done, p2, c2, splat))
        t_view = t_done if a.slot > 0 else tk + dt
        R, c = pose_at(traj, t_view); ps, ss, hs, ri, ci = score(P, C, p2, c2, splat, R, c)
        rows.append(dict(k=k, dt=dt, bytes=len(chunk), budget=budget, use=len(chunk) / budget, enc_s=tenc, dec_s=tdec, psnr=ps, ssim=ss, holes=hs, pts=len(p2)))
        live.append((ps, ss, hs))
        print(f"slot {k}: {len(chunk):6d} B / {budget:6.0f} ({100*len(chunk)/budget:5.1f}%) enc {tenc:4.1f}s dec {tdec:4.1f}s | live view PSNR {ps:.2f} SSIM {ss:.3f} holes {100*hs:.1f}%", flush=True)
        if a.views:
            from PIL import Image; Image.fromarray(np.vstack([ri, ci])).save(f"{a.views}/live{k:02d}.png")
    # operator experience: every second from t0 to end+5 s, the map fully received by then (empty = sky)
    grid = np.arange(0, ts[-1] - ts[0] + 5.0 + 1e-6, 1.0); ta = []
    for t in grid:
        R, c = pose_at(traj, ts[0] + t); have = [sn for sn in snaps if sn[0] <= ts[0] + t]
        if have: ps_, ss_, hs_, _, _ = score(P, C, have[-1][1], have[-1][2], have[-1][3], R, c)
        else: ps_, ss_, hs_ = 0.0, 0.0, 1.0
        ta.append((ps_, ss_, hs_))
    TA = np.mean(ta, 0); first_img = min((sn[0] for sn in snaps), default=np.inf) - ts[0]
    Rs, Ps = Q.views(traj); fin = []
    for i, (R, c) in enumerate(zip(Rs, Ps)):
        ps, ss, hs, ri, ci = score(P, C, p2, c2, splat, R, c); fin.append((ps, ss, hs))
        if a.views:
            from PIL import Image; Image.fromarray(np.vstack([ri, ci])).save(f"{a.views}/final{i:02d}.png")
    tot = sum(r["bytes"] for r in rows); over = [r["k"] for r in rows if r["bytes"] > r["budget"]]
    L, F = np.mean(live, 0), np.mean(fin, 0)
    line = (f"{os.path.basename(a.streamer)}: {tot/1e3:.1f} KB in {len(slots)} slots of {np.mean([d for _, d, _ in slots]):.1f} s at {a.rate:g} kbit/s"
            f"{' OVER BUDGET in slots ' + str(over) if over else ' (all slots within budget)'} | max enc {max(r['enc_s'] for r in rows):.1f}s"
            f" | LIVE PSNR {L[0]:.2f} SSIM {L[1]:.3f} holes {100*L[2]:.1f}% | FINAL PSNR {F[0]:.2f} SSIM {F[1]:.3f} holes {100*F[2]:.1f}%"
            f" | TIMEAVG PSNR {TA[0]:.2f} SSIM {TA[1]:.3f} first image {first_img:.1f}s | {len(p2):,} pts")
    print(line)
    if a.json: json.dump(dict(line=line, slots=rows, live=dict(psnr=L[0], ssim=L[1], holes=L[2]), final=dict(psnr=F[0], ssim=F[1], holes=F[2]), timeavg=dict(psnr=TA[0], ssim=TA[1], holes=TA[2], first_image_s=first_img), total_bytes=tot, over=over, init_s=tinit), open(a.json, "w"), indent=1)

if __name__ == "__main__": main()
