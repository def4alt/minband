"""perf_decoder_rt: dump the stream_best_rec records of a live_eval run (--rate 20 --slot 1) and compare decoder_rt
(incremental Decoder) with the original stream_best_rec.Decoder: bit-exact map after every record + timings.

usage (home, conda mast3r-slam):
  python perf_decoder_rt.py dump  [--rate 20] [--slot 1] [--out results_live/records_rec20_s1.pkl]   # encoder run, ~2-4 min
  python perf_decoder_rt.py check [--in results_live/records_rec20_s1.pkl] [--every 1] [--repeat 1]   # both decoders
The pickle holds the chunks in slot order; split_records() of stream_best_rec gives the wire records.
Python 3.11 compatible."""
import sys, os, time, pickle, argparse, importlib.util, resource, numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
CLOCK = [time.perf_counter]
def now(): return CLOCK[0]()
def rss_mb(): return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0

def load(path):
    s = importlib.util.spec_from_file_location(os.path.basename(path)[:-3], path); m = importlib.util.module_from_spec(s); s.loader.exec_module(m); return m

def dump(a):
    import quality as Q
    m = load(os.path.join(HERE, "stream_best_rec.py")); rate = a.rate * 1e3 / 8
    P, C, traj = Q.load_ref(); ts = traj[:, 0]
    nslot = int(np.ceil((ts[-1] - ts[0] + float(np.median(np.diff(ts)))) / a.slot))
    slots = [(ts[0] + i * a.slot, a.slot, max(1, int(np.searchsorted(ts, ts[0] + i * a.slot, side="right")))) for i in range(nslot)]
    enc = m.make_encoder(rate, P.copy(), C.copy()); bucket = 0.0; chunks = []; encs = []
    for k, (tk, dt, nkf) in enumerate(slots):
        bucket += rate * dt; dt_eff = bucket / rate
        t1 = time.perf_counter(); chunk = enc.update(k, traj[:nkf].copy(), dt_eff); tenc = time.perf_counter() - t1; bucket -= len(chunk)
        chunks.append(chunk); encs.append(tenc)
        print("slot %d: %d B / %.0f enc %.2f s records %s" % (k, len(chunk), rate * dt_eff, tenc, [len(r) for r in m.split_records(chunk, k == 0)]), flush=True)
    pickle.dump(dict(rate=a.rate, slot=a.slot, chunks=chunks, enc_s=encs), open(a.out, "wb"))
    print("saved %s: %d chunks, %d B, %d records" % (a.out, len(chunks), sum(map(len, chunks)), sum(len(m.split_records(c, k == 0)) for k, c in enumerate(chunks))))

def same(A, B):
    pa, ca, sa = A; pb, cb, sb = B
    pa, pb = np.asarray(pa), np.asarray(pb); ca, cb = np.asarray(ca), np.asarray(cb)
    return pa.shape == pb.shape and ca.shape == cb.shape and np.array_equal(pa, pb) and np.array_equal(ca, cb) and float(sa) == float(sb) and pa.dtype == pb.dtype and ca.dtype == cb.dtype

def check(a):
    m = load(os.path.join(HERE, "stream_best_rec.py")); rt = load(os.path.join(HERE, "decoder_rt.py"))
    if a.clock == "thread": CLOCK[0] = time.thread_time
    d = pickle.load(open(a.inp, "rb")); recs = []
    for k, c in enumerate(d["chunks"]): recs += m.split_records(c, k == 0)
    print("%d records, %d B total, median %d B, max %d B | clock %s | numba %s | RSS before %.0f MB" % (len(recs), sum(map(len, recs)), int(np.median([len(r) for r in recs])), max(map(len, recs)), a.clock, getattr(rt, "USE_NUMBA", None), rss_mb()))
    # ---- original decoder: apply + map after every record (timings)
    tA_apply, tA_map, mapsA = [], [], []
    dA = m.make_decoder()
    for i, r in enumerate(recs):
        t = now(); dA.apply(r); tA_apply.append(now() - t)
        t = now(); mp = dA.map(); tA_map.append(now() - t)
        mapsA.append(mp if (i % a.every == 0 or i == len(recs) - 1) else None)
    # ---- new decoder
    for rep in range(a.repeat):
        dB = rt.make_decoder(); tB_apply, tB_map, tB_new, ok_all, nnew = [], [], [], True, 0; viewer_pts = []; viewer_rgb = []
        for i, r in enumerate(recs):
            t = now(); dB.apply(r); tB_apply.append(now() - t)
            t = now(); mp = dB.map(); tB_map.append(now() - t)
            t = now(); np_, nc_ = dB.new_points_since_last(); tB_new.append(now() - t)
            if dB.generation != getattr(dB, "_chk_gen", None): viewer_pts, viewer_rgb = [], []; dB._chk_gen = dB.generation
            viewer_pts.append(np.asarray(np_)); viewer_rgb.append(np.asarray(nc_)); nnew += len(np_)
            if mapsA[i] is not None:
                ok = same(mapsA[i], mp)
                if not ok:
                    ok_all = False; pa, ca, sa = mapsA[i]; pb, cb, sb = mp
                    print("  MISMATCH after record %d (band %d, %d B): A %s pts, B %s pts, splat %g vs %g" % (i, r[m.header_len(r) if i == 0 else 0], len(r), np.asarray(pa).shape, np.asarray(pb).shape, sa, sb))
                    if np.asarray(pa).shape == np.asarray(pb).shape:
                        bad = np.nonzero(~(np.asarray(pa) == np.asarray(pb)).all(1))[0]; print("   first differing rows", bad[:5], np.asarray(pa)[bad[:2]], np.asarray(pb)[bad[:2]])
        # viewer (append-only) view equals the alive map as a set (order-free) -> check multiset of rows
        vp = np.concatenate(viewer_pts) if viewer_pts else np.zeros((0, 3)); vc = np.concatenate(viewer_rgb) if viewer_rgb else np.zeros((0, 3), np.uint8)
        pf, cf, sf = mp
        # the append-only viewer also keeps superseded coarse voxels -> it is a superset; check containment of the alive map
        key = lambda p, c: np.core.records.fromarrays([p[:, 0], p[:, 1], p[:, 2], c[:, 0], c[:, 1], c[:, 2]])
        sub = np.isin(key(np.asarray(pf), np.asarray(cf)), key(vp, vc)).all() if len(vp) else len(pf) == 0
        tA, tB, tM, tMB = np.array(tA_apply), np.array(tB_apply), np.array(tA_map), np.array(tB_map); nb = np.array([len(r) for r in recs])
        st_same = all(np.array_equal(x, y) for x, y in zip(dA.st.known, dB.known_arrays())) and all(np.array_equal(x, y) for x, y in zip(dA.st.cidx, dB.cidx_arrays()))
        print("\nrepeat %d: bit-exact map after every checked record: %s | voxel state identical: %s | append-only viewer contains final map: %s (%d viewer pts vs %d map pts)" % (rep, ok_all, st_same, sub, len(vp), len(pf)))
        print("ORIGINAL apply: mean %.1f ms  median %.1f  max %.1f  | per 500 B: %.1f ms | map(): mean %.1f ms max %.1f ms" % (1e3 * tA.mean(), 1e3 * np.median(tA), 1e3 * tA.max(), 1e3 * tA.sum() / nb.sum() * 500, 1e3 * tM.mean(), 1e3 * tM.max()))
        print("RT       apply: mean %.1f ms  median %.1f  max %.1f  | per 500 B: %.1f ms | map(): mean %.1f ms max %.1f ms | new_points_since_last: mean %.2f ms max %.2f ms" % (1e3 * tB.mean(), 1e3 * np.median(tB), 1e3 * tB.max(), 1e3 * tB.sum() / nb.sum() * 500, 1e3 * tMB.mean(), 1e3 * tMB.max(), 1e3 * np.mean(tB_new), 1e3 * np.max(tB_new)))
        big = nb >= 400
        if big.any(): print("records >= 400 B (%d): ORIGINAL apply mean %.1f max %.1f ms | RT mean %.1f max %.1f ms" % (big.sum(), 1e3 * tA[big].mean(), 1e3 * tA[big].max(), 1e3 * tB[big].mean(), 1e3 * tB[big].max()))
        print("RSS peak %.0f MB; RT grid extent E0 %s after %d growths (level-0 grid %.0f MB, exact band grids %.0f MB, scratch %.0f MB), final map %d pts, generation %d" % (rss_mb(), dB.E0, dB.n_grow, dB.KN[0][0].nbytes / 1e6, dB.EXbuf.nbytes / 1e6, dB.Kbuf.nbytes / 1e6, len(pf), dB.generation))
        if a.verbose:
            for i, r in enumerate(recs): print("  rec %3d band %d %5d B: orig %6.1f ms rt %6.1f ms | map orig %5.1f rt %5.1f ms | %d pts" % (i, r[m.header_len(r) if i == 0 else 0], len(r), 1e3 * tA[i], 1e3 * tB[i], 1e3 * tM[i], 1e3 * tMB[i], len(np.asarray(mapsA[i][0])) if mapsA[i] is not None else -1))
        if not (ok_all and st_same): sys.exit("MISMATCH")
    print("OK")

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("mode", choices=["dump", "check"]); ap.add_argument("--rate", type=float, default=20.0); ap.add_argument("--slot", type=float, default=1.0)
    ap.add_argument("--out", default=os.path.join(HERE, "results_live", "records_rec20_s1.pkl")); ap.add_argument("--in", dest="inp", default=os.path.join(HERE, "results_live", "records_rec20_s1.pkl"))
    ap.add_argument("--every", type=int, default=1); ap.add_argument("--clock", choices=["wall", "thread"], default="wall", help="thread = CPU time of the calling thread (robust to a loaded machine, excludes BLAS worker threads)"); ap.add_argument("--repeat", type=int, default=1); ap.add_argument("-v", "--verbose", action="store_true"); a = ap.parse_args()
    dump(a) if a.mode == "dump" else check(a)
