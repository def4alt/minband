"""perf_rt: live timing + byte-identity check of stream_rt against stream_best_pkt (or any reference streamer).
Drives both encoders EXACTLY like live_eval.py --rate R --slot S (token bucket) for N slots, compares the chunk bytes
slot by slot, times enc.update per slot (mean / max), the decoder apply per record (split_records) and dec.map(),
and checks that a fresh stream_rt decoder fed record by record ends in the same voxel state as the reference decoder.
usage (home): python perf_rt.py [--slots 0=all] [--rate 20] [--slot 1] [--ref stream_best_pkt.py] [--noref] [--json out]"""
import sys, os, time, argparse, importlib.util, json, resource, numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import quality as Q

def load(path):
    s = importlib.util.spec_from_file_location(os.path.basename(path)[:-3], path); m = importlib.util.module_from_spec(s); s.loader.exec_module(m); return m

def rss_mb():
    for ln in open("/proc/self/status"):
        if ln.startswith("VmRSS"): return int(ln.split()[1]) / 1024.0
    return 0.0

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--slots", type=int, default=0); ap.add_argument("--rate", type=float, default=20.0); ap.add_argument("--slot", type=float, default=1.0)
    ap.add_argument("--mapcheck", action="store_true", help="compare dec.map() after every record with stream_best.Decoder.map on the same state"); ap.add_argument("--streamer", default="stream_rt.py"); ap.add_argument("--ref", default="stream_best_pkt.py"); ap.add_argument("--noref", action="store_true"); ap.add_argument("--json"); a = ap.parse_args()
    here = os.path.dirname(os.path.abspath(__file__)); rate = a.rate * 1e3 / 8
    P, C, traj = Q.load_ref(); ts = traj[:, 0]
    nslot = int(np.ceil((ts[-1] - ts[0] + float(np.median(np.diff(ts)))) / a.slot)); nslot = min(nslot, a.slots) if a.slots else nslot
    slots = [(ts[0] + i * a.slot, a.slot, max(1, int(np.searchsorted(ts, ts[0] + i * a.slot, side="right")))) for i in range(nslot)]
    m = load(os.path.join(here, a.streamer)); ref = None if a.noref else load(os.path.join(here, a.ref))
    t0 = time.perf_counter(); enc = m.make_encoder(rate, P.copy(), C.copy()); tinit = time.perf_counter() - t0
    print("%s: init %.2f s (numba %s), RSS %.0f MB" % (a.streamer, tinit, getattr(m, "HAVE_NUMBA", "?"), rss_mb()), flush=True)
    rows = []; chunks = []; bucket = 0.0
    for k, (tk, dt, nkf) in enumerate(slots):
        bucket += rate * dt; dt_eff = bucket / rate; t1 = time.perf_counter(); b = enc.update(k, traj[:nkf].copy(), dt_eff); te = time.perf_counter() - t1; bucket -= len(b); chunks.append(b)
        rows.append(dict(k=k, bytes=len(b), budget=rate * dt_eff, enc_s=te, ntrials=getattr(enc, "ntrials", 0), rss=rss_mb()))
        print("slot %2d: %5d B / %5.0f  enc %.3f s  %d trials  RSS %.0f MB" % (k, len(b), rate * dt_eff, te, rows[-1]["ntrials"], rows[-1]["rss"]), flush=True)
    E = [r["enc_s"] for r in rows]
    print("%s: %d slots, enc mean %.3f s max %.3f s (slots >0.5 s: %s), total %d B, peak RSS %.0f MB" % (a.streamer, len(rows), np.mean(E), max(E), [r["k"] for r in rows if r["enc_s"] > 0.5], sum(r["bytes"] for r in rows), resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024), flush=True)
    # decoder: record by record, timed
    dec = m.make_decoder(); rt = []; mt = []; nrec = 0; PROF = getattr(m, "PROF", {}); ph = {}; mapbad = 0
    import stream_best as SB
    for k, b in enumerate(chunks):
        for r in m.split_records(b, k == 0):
            p0 = dict(PROF); t1 = time.perf_counter(); dec.apply(r); ta = time.perf_counter() - t1; t2 = time.perf_counter(); p2, c2, sp = dec.map(); tm = time.perf_counter() - t2
            for q in PROF: ph[q] = ph.get(q, 0.0) + PROF[q] - p0.get(q, 0.0)
            rt.append((len(r), ta)); mt.append(tm); nrec += 1
            if a.mapcheck:
                pr, cr, sr = SB.Decoder.map(dec)
                if not (np.array_equal(pr, p2) and np.array_equal(cr, c2) and sr == sp): mapbad += 1; print("  map mismatch after record %d: %d vs %d pts" % (nrec, len(pr), len(p2)))
    if a.mapcheck: print("map identical to stream_best.Decoder.map after every record: %s (%d mismatches)" % (mapbad == 0, mapbad), flush=True)
    rb = np.array([x[0] for x in rt]); ra = np.array([x[1] for x in rt])
    print("decoder: %d records, apply mean %.1f ms max %.1f ms (%.1f ms per 500 B; excl. first: mean %.1f max %.1f), map mean %.1f ms max %.1f ms, %d pts" % (nrec, ra.mean() * 1e3, ra.max() * 1e3, ra.sum() / rb.sum() * 500e3, ra[1:].mean() * 1e3, ra[1:].max() * 1e3, np.mean(mt) * 1e3, max(mt) * 1e3, len(p2)), flush=True)
    if ph: print("decoder phases (ms per record): " + " ".join("%s %.1f" % (q, v / nrec * 1e3) for q, v in sorted(ph.items())), flush=True)
    res = dict(streamer=a.streamer, slots=rows, enc_mean=float(np.mean(E)), enc_max=float(max(E)), dec_apply_mean_ms=float(ra.mean() * 1e3), dec_apply_max_ms=float(ra.max() * 1e3), map_mean_ms=float(np.mean(mt) * 1e3), map_max_ms=float(max(mt) * 1e3))
    if ref is not None:
        encr = ref.make_encoder(rate, P.copy(), C.copy()); rrows = []; rchunks = []; bucket = 0.0
        def share_setup(traj, src=enc, dst=encr):  # Open3D's RANSAC ground plane is not deterministic between instances: share it
            dst.R, dst.Q3, dst.o, dst.cen, dst.cenw = src.R, src.Q3, src.o, src.cen, src.cenw
            dst.bands = [dict(u=B["u"], inv=B["inv"], n=B["n"], sent=np.zeros(B["n"], bool)) for B in src.bands]
            if hasattr(m, "State") and isinstance(dst.st, m.State): dst.st = m.State(len(m.LEVELS), [B["u"].max(0) + 1 for B in dst.bands])
        encr._setup = share_setup
        for k, (tk, dt, nkf) in enumerate(slots):
            bucket += rate * dt; dt_eff = bucket / rate; t1 = time.perf_counter(); b = encr.update(k, traj[:nkf].copy(), dt_eff); te = time.perf_counter() - t1; bucket -= len(b); rchunks.append(b)
            same = b == chunks[k]; rrows.append(dict(k=k, bytes=len(b), enc_s=te, same=bool(same)))
            print("ref slot %2d: %5d B enc %.3f s  %s" % (k, len(b), te, "IDENTICAL" if same else "DIFFERENT (%d vs %d B)" % (len(b), len(chunks[k]))), flush=True)
        ER = [r["enc_s"] for r in rrows]; nsame = sum(r["same"] for r in rrows)
        print("%s: enc mean %.3f s max %.3f s; %d/%d chunks byte-identical to %s" % (a.ref, np.mean(ER), max(ER), nsame, len(rrows), a.streamer), flush=True)
        decr = ref.make_decoder()
        for k, b in enumerate(rchunks): decr.apply(b)
        pA, cA, sA = decr.map(); p2, c2, sp = dec.map()
        same_state = all(np.array_equal(x, y) for x, y in zip(decr.st.known, dec.st.known)) and all(np.array_equal(x, y) for x, y in zip(decr.st.cidx, dec.st.cidx))
        same_map = len(pA) == len(p2) and np.array_equal(np.asarray(pA), np.asarray(p2)) and np.array_equal(np.asarray(cA), np.asarray(c2)) and sA == sp
        print("decoder state identical: %s, map identical: %s" % (same_state, same_map), flush=True)
        res.update(ref=a.ref, ref_enc_mean=float(np.mean(ER)), ref_enc_max=float(max(ER)), identical_chunks=nsame, n_chunks=len(rrows), same_state=bool(same_state), same_map=bool(same_map))
    if a.json: json.dump(res, open(a.json, "w"), indent=1)

if __name__ == "__main__": main()
