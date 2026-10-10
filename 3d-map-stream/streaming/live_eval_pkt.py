"""LIVE streaming harness with RECORD-LEVEL arrival (copy of live_eval.py, same CLI, same metrics).
usage: python live_eval_pkt.py stream_x.py [--rate 20] [--slot 1] [--views DIR] [--json out.json]

Same streamer API as live_eval.py.  If the streamer module exposes split_records(chunk_bytes, first) -> [record bytes]
(stream_best_rec / stream_best_pkt), the chunk of every planning slot is transmitted as its records back to back at the
link rate and the receiver applies EVERY record at its own arrival time:
  * TIMEAVG = operator view sampled every second from the map fully received by then (record granularity);
  * LIVE view per slot (after the whole chunk, as in live_eval.py) and FINAL map unchanged;
  * extra: records count, updates per second (records / flight seconds = sum of slot lengths), first image time,
    and the exact packetization overhead when the encoder exposes enc.stats[k]['pkt_bytes'/'band_bytes']
    (same voxels coded as ~PKT records vs one record per band).
Without split_records the chunk is one record (identical to live_eval.py)."""
import sys, os, time, json, argparse, importlib.util, numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import quality as Q
from live_eval import pose_at, score

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("streamer"); ap.add_argument("--rate", type=float, default=10.0, help="kbit/s")
    ap.add_argument("--views"); ap.add_argument("--json")
    ap.add_argument("--slot", type=float, default=0.0, help="transmission slot length in s (default 0 = one slot per keyframe)"); a = ap.parse_args()
    rate = a.rate * 1e3 / 8
    P, C, traj = Q.load_ref(); K = len(traj); ts = traj[:, 0]
    s = importlib.util.spec_from_file_location("streamer", a.streamer); m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
    split = getattr(m, "split_records", None)
    t0 = time.time(); enc = m.make_encoder(rate, P.copy(), C.copy()); dec = m.make_decoder(); tinit = time.time() - t0
    rows, live = [], []
    if a.views: os.makedirs(a.views, exist_ok=True)
    if a.slot > 0:
        nslot = int(np.ceil((ts[-1] - ts[0] + float(np.median(np.diff(ts)))) / a.slot))
        slots = [(ts[0] + i * a.slot, a.slot, max(1, int(np.searchsorted(ts, ts[0] + i * a.slot, side="right")))) for i in range(nslot)]
    else:
        slots = [(ts[k], (ts[k + 1] - ts[k]) if k + 1 < K else float(np.median(np.diff(ts))), k + 1) for k in range(K)]
    bucket, t_done, snaps, recs_all = 0.0, ts[0], [], []
    for k, (tk, dt, nkf) in enumerate(slots):
        if a.slot > 0: bucket += rate * dt; dt_eff = bucket / rate
        else: dt_eff = dt
        budget = rate * dt_eff
        t1 = time.time(); chunk = enc.update(k, traj[:nkf].copy(), dt_eff); tenc = time.time() - t1
        if a.slot > 0: bucket -= len(chunk)
        recs = split(chunk, k == 0) if split else [chunk]
        assert b"".join(recs) == chunk, "split_records must partition the chunk"
        t2 = time.time(); nmap = 0
        for i, r in enumerate(recs):  # every record is applied when it has fully arrived (back to back at the link rate)
            dec.apply(r); t_start = max(tk, t_done); t_done = t_start + len(r) / rate
            recs_all.append(dict(k=k, i=i, bytes=len(r), t_start=t_start - ts[0], t_done=t_done - ts[0]))
            last = i == len(recs) - 1
            if not last:  # only materialise the map if a 1 s grid tick falls before the next record arrives
                t_next = t_done + len(recs[i + 1]) / rate; j = np.ceil(t_done - ts[0] - 1e-9)
                if not (ts[0] + j < t_next): continue
            p2, c2, splat = dec.map(); nmap += 1; p2, c2 = np.asarray(p2, float), np.asarray(c2, np.uint8)
            snaps.append((t_done, p2, c2, splat))
        tdec = time.time() - t2
        t_view = t_done if a.slot > 0 else tk + dt
        R, c = pose_at(traj, t_view); ps, ss, hs, ri, ci = score(P, C, p2, c2, splat, R, c)
        rows.append(dict(k=k, dt=dt, bytes=len(chunk), budget=budget, use=len(chunk) / budget, enc_s=tenc, dec_s=tdec, psnr=ps, ssim=ss, holes=hs, pts=len(p2), records=len(recs), rec_bytes=[len(r) for r in recs]))
        live.append((ps, ss, hs))
        print(f"slot {k}: {len(chunk):6d} B / {budget:6.0f} ({100*len(chunk)/budget:5.1f}%) {len(recs):2d} rec enc {tenc:4.1f}s dec {tdec:4.1f}s ({nmap} maps) | live view PSNR {ps:.2f} SSIM {ss:.3f} holes {100*hs:.1f}%", flush=True)
        if a.views:
            from PIL import Image; Image.fromarray(np.vstack([ri, ci])).save(f"{a.views}/live{k:02d}.png")
    grid = np.arange(0, ts[-1] - ts[0] + 5.0 + 1e-6, 1.0); ta = []
    for t in grid:
        R, c = pose_at(traj, ts[0] + t); have = [sn for sn in snaps if sn[0] <= ts[0] + t]
        if have: ps_, ss_, hs_, _, _ = score(P, C, have[-1][1], have[-1][2], have[-1][3], R, c)
        else: ps_, ss_, hs_ = 0.0, 0.0, 1.0
        ta.append((ps_, ss_, hs_))
    TA = np.mean(ta, 0); first_img = recs_all[0]["t_done"] if recs_all else np.inf
    Rs, Ps = Q.views(traj); fin = []
    for i, (R, c) in enumerate(zip(Rs, Ps)):
        ps, ss, hs, ri, ci = score(P, C, p2, c2, splat, R, c); fin.append((ps, ss, hs))
        if a.views:
            from PIL import Image; Image.fromarray(np.vstack([ri, ci])).save(f"{a.views}/final{i:02d}.png")
    tot = sum(r["bytes"] for r in rows); over = [r["k"] for r in rows if r["bytes"] > r["budget"]]
    L, F = np.mean(live, 0), np.mean(fin, 0); flight = sum(d for _, d, _ in slots); nrec = len(recs_all)
    rb = np.array([r["bytes"] for r in recs_all]); ups = nrec / flight
    stats = getattr(enc, "stats", None); pair = None
    if stats and all("band_bytes" in r for r in stats):
        pb, bb = sum(r["pkt_bytes"] for r in stats), sum(r["band_bytes"] for r in stats); pair = 100 * (pb - bb) / bb
    line = (f"{os.path.basename(a.streamer)}{(' PKT=' + os.environ['PKT']) if 'PKT' in os.environ else ''}: {tot/1e3:.1f} KB in {len(slots)} slots of {np.mean([d for _, d, _ in slots]):.1f} s at {a.rate:g} kbit/s"
            f"{' OVER BUDGET in slots ' + str(over) if over else ' (all slots within budget)'} | max enc {max(r['enc_s'] for r in rows):.1f}s"
            f" | {nrec} records (median {np.median(rb):.0f} B) = {ups:.2f} updates/s, first image {first_img:.2f}s"
            + (f", packetization overhead {pair:+.2f}% vs one record per band" if pair is not None else "")
            + f" | LIVE PSNR {L[0]:.2f} SSIM {L[1]:.3f} holes {100*L[2]:.1f}% | FINAL PSNR {F[0]:.2f} SSIM {F[1]:.3f} holes {100*F[2]:.1f}%"
            f" | TIMEAVG PSNR {TA[0]:.2f} SSIM {TA[1]:.3f} | {len(p2):,} pts")
    print(line)
    if a.json: json.dump(dict(line=line, slots=rows, records=recs_all, live=dict(psnr=L[0], ssim=L[1], holes=L[2]), final=dict(psnr=F[0], ssim=F[1], holes=F[2]),
                              timeavg=dict(psnr=TA[0], ssim=TA[1], holes=TA[2], first_image_s=first_img, curve=[list(map(float, x)) for x in ta]),
                              n_records=nrec, updates_per_s=ups, flight_s=flight, pair_overhead_pct=pair, enc_stats=stats, pkt=os.environ.get("PKT"),
                              total_bytes=tot, over=over, init_s=tinit), open(a.json, "w"), indent=1)

if __name__ == "__main__": main()
