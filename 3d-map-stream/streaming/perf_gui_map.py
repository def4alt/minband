#!/usr/bin/env python
"""Parity + timing test of map_receiver_gui.IncrementalMap against Decoder.map(), record by record.
usage: python perf_gui_map.py [--streamer stream_best_rec.py] [--rate 2500] [--chunks 0] [--draw points]
                              [--records cache.bin]   (encodes once, caches the records framed <I len><payload>)
Prints per record: dec.apply ms, ingest ms, dec.map ms, point count, parity (same multiset of (xyz, rgb) rows)."""
import os, sys, time, struct, argparse, importlib.util, numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
from map_receiver_gui import IncrementalMap


def load(path):
    s = importlib.util.spec_from_file_location("streamer", path); m = importlib.util.module_from_spec(s); s.loader.exec_module(m); return m


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--streamer", default=os.path.join(HERE, "stream_best_rec.py")); ap.add_argument("--rate", type=float, default=2500.0)
    ap.add_argument("--chunks", type=int, default=0); ap.add_argument("--records", default=None); ap.add_argument("--draw", default="points"); a = ap.parse_args()
    m = load(a.streamer)
    if a.records and os.path.exists(a.records):
        b = open(a.records, "rb").read(); recs, p = [], 0
        while p < len(b): n = struct.unpack("<I", b[p:p + 4])[0]; recs.append(b[p + 4:p + 4 + n]); p += 4 + n
        print(f"loaded {len(recs)} records from {a.records}")
    else:
        import quality as Q
        P, C, traj = Q.load_ref(); ts = traj[:, 0]; K = len(traj) if a.chunks <= 0 else min(a.chunks, len(traj))
        enc = m.make_encoder(a.rate, P.copy(), C.copy()); recs = []
        for k in range(K):
            dt = (ts[k + 1] - ts[k]) if k + 1 < len(ts) else float(np.median(np.diff(ts)))
            t1 = time.time(); chunk = enc.update(k, traj[:k + 1].copy(), dt); te = time.time() - t1
            rr = m.split_records(chunk, k == 0) if hasattr(m, "split_records") else [chunk]
            print(f"slot {k}: {len(chunk)} B enc {te:.1f}s -> {len(rr)} records {[len(r) for r in rr]}", flush=True); recs += rr
        if a.records:
            with open(a.records, "wb") as f:
                for r in recs: f.write(struct.pack("<I", len(r)) + r)
    dec = m.make_decoder(); imap = IncrementalMap(a.draw); ok_all = True; T = dict(apply=[], ingest=[], map=[])
    for i, r in enumerate(recs):
        t1 = time.time(); dec.apply(r); t2 = time.time(); evs = imap.ingest(dec); t3 = time.time(); p2, c2, sp = dec.map(); t4 = time.time()
        T["apply"].append(t2 - t1); T["ingest"].append(t3 - t2); T["map"].append(t4 - t3)
        p1, c1, sp1 = imap.full_points()
        A = np.concatenate([np.round(np.asarray(p1, float), 5), np.asarray(c1, float)], 1); B = np.concatenate([np.round(np.asarray(p2, float), 5), np.asarray(c2, float)], 1)
        same = len(A) == len(B) and abs(sp1 - sp) < 1e-9 and np.array_equal(A[np.lexsort(A.T[::-1])], B[np.lexsort(B.T[::-1])])
        ok_all &= same; nv = sum(len(e["verts"]) for e in evs); nd = sum(len(e["dead"]) for e in evs)
        print(f"rec {i:3d} {len(r):6d} B band {r[0] if i else 'hdr'}: apply {1e3*(t2-t1):6.1f} ms  ingest {1e3*(t3-t2):5.1f} ms ({nv} vox, {nd} covered, {len(evs)} ev)  dec.map {1e3*(t4-t3):6.1f} ms  pts {len(p2):7d}  vox {imap.nvox()}  parity {'OK' if same else 'MISMATCH'}", flush=True)
    print(f"PARITY {'OK' if ok_all else 'FAILED'} over {len(recs)} records | apply mean {1e3*np.mean(T['apply']):.1f} max {1e3*np.max(T['apply']):.1f} ms"
          f" | ingest mean {1e3*np.mean(T['ingest']):.1f} max {1e3*np.max(T['ingest']):.1f} ms | dec.map mean {1e3*np.mean(T['map']):.1f} max {1e3*np.max(T['map']):.1f} ms")
    # vertex budget of the draw modes (what the GPU viewer has to render)
    for draw in ("points", "hybrid", "quads"):
        im2 = IncrementalMap(draw); d2 = m.make_decoder(); nvert = 0
        for r in recs: d2.apply(r); im2.ingest(d2)
        nvert = sum(int(im2.alive[b].sum()) * (4 if im2._kind(b) == "mesh" else int(np.ceil(im2.v[b] / im2.vmin - 1e-3)) ** 2) for b in range(im2.nb))
        print(f"draw={draw:7s}: {nvert:8,d} vertices for {im2.npts():,} map points")
    return 0 if ok_all else 1


if __name__ == "__main__": sys.exit(main())
