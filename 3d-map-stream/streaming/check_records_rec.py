"""Record-level decodability check for stream_best_rec: encode the 5 chunks, split them into per-band records,
feed a FRESH decoder record by record in transmission order (coarse band first) and assert the final map is identical
to a whole-chunk decode.  Prints record sizes and the time of the first image at 1250 B/s.
usage: python check_records_rec.py [stream_best_rec.py]"""
import sys, os, time, importlib.util, numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import quality as Q
RATE = 1250.0
streamer = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "stream_best_rec.py")
s = importlib.util.spec_from_file_location("st", streamer); m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
P, C, traj = Q.load_ref(); ts = traj[:, 0]; K = len(traj)
enc = m.make_encoder(RATE, P.copy(), C.copy()); chunks = []
for k in range(K):
    dt = (ts[k + 1] - ts[k]) if k + 1 < K else float(np.median(np.diff(ts)))
    t0 = time.time(); b = enc.update(k, traj[: k + 1].copy(), dt); chunks.append(b)
    print(f"chunk {k}: {len(b)} B (budget {RATE*dt:.0f}) enc {time.time()-t0:.1f}s", flush=True)

# whole-chunk decode (as live_eval does)
dA = m.make_decoder()
for b in chunks: dA.apply(b)
pA, cA, sA = dA.map()

# record-by-record decode with a fresh decoder, map() rendered after every record
dB = m.make_decoder(); t_done = ts[0]; first = None; nrec = 0; ok_each = True
for k, b in enumerate(chunks):
    recs = m.split_records(b, k == 0); assert b"".join(recs) == b, "split_records must be a partition of the chunk"
    for i, r in enumerate(recs):
        dB.apply(r); p2, c2, sp = dB.map(); nrec += 1
        start = max(ts[k], t_done); t_done = start + len(r) / RATE
        if first is None: first = t_done - ts[0]
        band = r[m.header_len(r) if (k == 0 and i == 0) else 0]
        print(f"  chunk {k} record {i}: band {m.LEVELS[band]:g} m, {len(r):5d} B{' (incl. %d B header)' % m.header_len(r) if (k == 0 and i == 0) else ''}, arrives t={t_done-ts[0]:5.1f} s, map {len(p2):,} pts", flush=True)
pB, cB, sB = dB.map()
same = (len(pA) == len(pB)) and np.array_equal(np.asarray(pA), np.asarray(pB)) and np.array_equal(np.asarray(cA), np.asarray(cB)) and sA == sB
# also compare the raw voxel state
same_state = all(np.array_equal(a, b) for a, b in zip(dA.st.known, dB.st.known)) and all(np.array_equal(a, b) for a, b in zip(dA.st.cidx, dB.st.cidx))
tot = sum(len(b) for b in chunks)
print(f"\n{K} chunks, {nrec} records, {tot} B total; first image after {first:.2f} s at {RATE:.0f} B/s (whole chunk 0 would need {len(chunks[0])/RATE:.2f} s)")
print(f"record-by-record decode == whole-chunk decode: map {same}, voxel state {same_state}")
assert same and same_state, "MISMATCH"
print("OK")
