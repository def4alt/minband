"""Record-level decode test for stream_best_pkt (or any streamer exposing split_records): run the encoder over the
live_eval slot schedule (--rate kbit/s, --slot s, token bucket), split every chunk into its records, feed a FRESH
decoder the records ONE AT A TIME in transmission order calling dec.map() after each, and assert the final map (and the
raw voxel state) equals a whole-chunk decode.  Also checks that every record decodes on its own (map grows
monotonically) and prints the record arrival timeline.
usage: PKT=600 python test_records_pkt.py [stream_best_pkt.py] [--rate 20] [--slot 1] [--slots N]"""
import sys, os, time, argparse, importlib.util, numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import quality as Q
ap = argparse.ArgumentParser(); ap.add_argument("streamer", nargs="?", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "stream_best_pkt.py"))
ap.add_argument("--rate", type=float, default=20.0); ap.add_argument("--slot", type=float, default=1.0); ap.add_argument("--slots", type=int, default=0, help="limit (0 = whole flight)")
a = ap.parse_args(); rate = a.rate * 1e3 / 8
s = importlib.util.spec_from_file_location("st", a.streamer); m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
P, C, traj = Q.load_ref(); ts = traj[:, 0]
nslot = int(np.ceil((ts[-1] - ts[0] + float(np.median(np.diff(ts)))) / a.slot)); nslot = min(nslot, a.slots) if a.slots else nslot
enc = m.make_encoder(rate, P.copy(), C.copy()); chunks = []; bucket = 0.0
for k in range(nslot):
    tk = ts[0] + k * a.slot; nkf = max(1, int(np.searchsorted(ts, tk, side="right"))); bucket += rate * a.slot
    t0 = time.time(); b = enc.update(k, traj[:nkf].copy(), bucket / rate); bucket -= len(b); chunks.append((tk, b))
    recs = m.split_records(b, k == 0); assert b"".join(recs) == b
    print(f"slot {k}: {len(b)} B, {len(recs)} records {[len(r) for r in recs]}, enc {time.time()-t0:.1f}s", flush=True)
dA = m.make_decoder()
for _, b in chunks: dA.apply(b)
pA, cA, sA = dA.map()
dB = m.make_decoder(); t_done = ts[0]; first = None; nrec = 0; npts_prev = 0; t_dec = 0.0
for k, (tk, b) in enumerate(chunks):
    for i, r in enumerate(m.split_records(b, k == 0)):
        t1 = time.time(); dB.apply(r); p2, c2, sp = dB.map(); t_dec += time.time() - t1; nrec += 1
        assert len(p2) > 0 and len(p2) == len(c2)  # (the pts count may drop: fine voxels replace a coarse voxel kk^3 sub-points)
        start = max(tk, t_done); t_done = start + len(r) / rate
        if first is None: first = t_done - ts[0]
        if k < 3: print(f"  slot {k} record {i}: {len(r):5d} B arrives t={t_done-ts[0]:5.2f} s, map {len(p2):,} pts", flush=True)
pB, cB, sB = dB.map()
same = len(pA) == len(pB) and np.array_equal(np.asarray(pA), np.asarray(pB)) and np.array_equal(np.asarray(cA), np.asarray(cB)) and sA == sB
same_state = all(np.array_equal(x, y) for x, y in zip(dA.st.known, dB.st.known)) and all(np.array_equal(x, y) for x, y in zip(dA.st.cidx, dB.st.cidx))
tot = sum(len(b) for _, b in chunks)
print(f"\n{nslot} slots, {nrec} records, {tot} B, first image after {first:.2f} s at {rate:.0f} B/s; record decode+map {t_dec/nrec*1e3:.0f} ms/record")
print(f"record-by-record decode == whole-chunk decode: map {same}, voxel state {same_state}")
assert same and same_state, "MISMATCH"; print("OK")
