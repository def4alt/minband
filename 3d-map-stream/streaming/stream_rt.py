"""stream_rt: REAL-TIME version of stream_best_pkt (stream_best coder, ~PKT-byte records, persistent contexts).
BIT-IDENTICAL to stream_best_pkt for the same PKT / env (same records, same bytes, same budget-fit decisions); only
the implementation changed so the whole planning slot fits the live budget (home: 0.2-0.4 s per 1 s slot instead of
2-17 s; decoder 6 ms per ~500 B record instead of 35 ms; dec.map() 4 ms instead of O(map)).

  * NO State.copy per trial / per record: the adaptive context tables (tb 13 MB + tf 34 MB) are shared; every table
    update is JOURNALLED (ctx, fctx, y of each coded octree level) and rolled back (-1) after a rejected trial or a
    re-cut record; the chosen trial is re-applied (+1) once -> exact (integer counts in float), no 47 MB copies.
  * known-voxel context: per-band OCCUPANCY PYRAMIDS occ[band][level] (bool grid, one bit per received voxel per
    level, journalled too) replace kn_grid's dense rebuild over all received voxels per octree level per trial; the
    flags (2 = equal/finer received voxel inside, 1 = inside a coarser received voxel, 0 = none / outside the record
    box) are looked up on the fly at the candidate cells only -> O(candidates) instead of O(map) + O(box).
  * numba kernels (identical arithmetic, same table-update schedule per 512-symbol chunk): one kernel per octree
    level on the encoder (candidates, contexts, probabilities, table updates, next parents), per-octant kernels on
    the decoder (the symbols of an octant are needed before the next one), greedy colour choice + colour range coder
    (freq tables as int64 arrays), colour decoder, zeff projection (fused, parallel; verified against the numpy path
    at the first call, numpy fallback otherwise), band assignment + per-voxel accumulation of select() in one pass.
    Without numba the same code runs as plain Python/numpy (slow but identical bytes).
  * parent_ctx / dec.map(): per-band sorted key arrays maintained incrementally; map() caches the densified points
    and colours per band and hides covered coarse voxels with the pyramids -> O(record) per call, same output.
  * glibc mallopt keeps freed buffers in the process (fresh pages cost ~10x on a swap-full box).
Not supported: PKT_PAIR (measurement only; use stream_best_pkt), ZM=sched, CTX_SHARE=0, len(LEVELS) != 4 with numba.
Env: PKT (default 600), PKT_MINVOX, PKT_RECUT, PKT_ORDER, CONF, LAM, MAXT, FILL, TIE, CTX_KNOWN, CTX_PCOL as before;
RT_NUMBA=0 disables numba; RT_NBTHREADS (zeff threads, default 12); RT_PROF=1 prints per-slot phase times.
API: make_encoder / make_decoder as live_eval.py; enc.prepare(traj) optional early setup; split_records / header_len
as stream_best_rec; enc.stats[k] = dict(pkt_bytes, nrec, nvox, hdr, ntrials).  Verify: perf_rt.py, debug_rt.py."""
import os, sys, math, struct, numpy as np, constriction
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stream_best as SB, stream_best_rec as SR
from stream_best import LEVELS, NS, SPL, MAXT, FILL, TIE, NCC, NF, NCTX, H, NB, ED, FINE, CAP, INC, LIMIT, WV, PLANE, keyf, M21, MINCNT, CONF
import lod_common as L, codec_lod_bands as LB
try:  # keep freed large buffers inside the process (no mmap/munmap per temporary): fresh pages are ~10x slower on a swap-full box
    import ctypes; _libc = ctypes.CDLL("libc.so.6"); _libc.mallopt(-3, 32 * 1024 * 1024); _libc.mallopt(-1, 1 << 30); _libc.mallopt(-2, 64 * 1024 * 1024)
except Exception: pass
geo = SB.geo; CHUNK = geo.CHUNK; ALPHA = float(geo.ALPHA)
KNOWN = SB.KNOWN; PCOL = SB.PCOL
ALPHA_MIN = float(os.environ.get("RT_ALPHA_MIN", 0))  # resolution floor (live: keep the content rate under the link budget; 0 = off)
PKT = float(os.environ.get("PKT", 600)); MINVOX = int(os.environ.get("PKT_MINVOX", 8)); RECUT = float(os.environ.get("PKT_RECUT", 0.4))
ORDER = os.environ.get("PKT_ORDER", "block"); NTHR = int(os.environ.get("RT_THREADS", 8))
header_len = SR.header_len; split_records = SR.split_records
def _lex(u): return np.lexsort(u.T[::-1])
def _sort(v): return v[np.lexsort(v.T[::-1])]
PROF = {}  # phase -> seconds (env RT_PROF=1 prints per slot)
import time as _time
def _tic(): return _time.perf_counter()
def _toc(name, t): PROF[name] = PROF.get(name, 0.0) + _time.perf_counter() - t

# ---------------- kernels (numba if available; same source runs in plain Python) ----------------
try:
    if os.environ.get("RT_NUMBA", "1") != "1": raise ImportError("disabled")
    import numba
    numba.config.THREADING_LAYER = "workqueue"
    try: numba.set_num_threads(max(1, min(int(os.environ.get("RT_NBTHREADS", 12)), numba.config.NUMBA_NUM_THREADS)))
    except Exception: pass
    jit = numba.njit(cache=False, nogil=True); pjit = numba.njit(cache=False, nogil=True, parallel=True); prange = numba.prange
    HAVE_NUMBA = True
except Exception:
    HAVE_NUMBA = False
    def jit(f): return f
    pjit = jit; prange = range

NBo = np.array(NB, np.int64); EDo = np.array(ED, np.int64); FINEo = np.array(FINE, np.int64)
HMUL = np.uint64(0x9E3779B97F4A7C15); H42 = np.uint64(42)
NCC_ = int(NCC); NS_ = int(NS); NF_ = int(NF); CAP_ = int(CAP); INC_ = int(INC); LIMIT_ = int(LIMIT)

@jit
def k_ctx(K, KN, cc, k, ctx, fctx):
    """stream_best._ctx for all cells cc (padded coords) of octant k -> ctx (coarse) and fctx (hashed fine)."""
    n = cc.shape[0]
    for i in range(n):
        x = cc[i, 0]; y = cc[i, 1]; z = cc[i, 2]
        s = 0
        for j in range(6): s = s * 3 + K[x + NBo[j, 0], y + NBo[j, 1], z + NBo[j, 2]]
        n1 = 0; n2 = 0
        for j in range(12):
            v = K[x + EDo[j, 0], y + EDo[j, 1], z + EDo[j, 2]]
            if v == 1: n1 += 1
            elif v == 2: n2 += 1
        co = ((k * 729 + s) * (CAP_ + 1) + min(n1, CAP_)) * (CAP_ + 1) + min(n2, CAP_)
        kn = KN[x, y, z]; nb2 = 0
        for j in range(6):
            if KN[x + NBo[j, 0], y + NBo[j, 1], z + NBo[j, 2]] == 2: nb2 += 1
        c = co * NF_ + kn * 3 + min(nb2, 2); ctx[i] = c
        f = c
        for j in range(14): f = f * 3 + K[x + FINEo[j, 0], y + FINEo[j, 1], z + FINEo[j, 2]]
        fctx[i] = np.int64((np.uint64(f) * HMUL) >> H42)

def ctx_np(K, KN, cc, k):
    """vectorised fallback (= stream_best._ctx)."""
    return SB._ctx(K, KN, cc, k)

def CTX(K, KN, cc, k):
    if HAVE_NUMBA:
        ctx = np.empty(len(cc), np.int64); fctx = np.empty(len(cc), np.int64); k_ctx(K, KN, cc, k, ctx, fctx); return ctx, fctx
    return ctx_np(K, KN, cc, k)

@jit
def k_enc_probs(tb, tf, ctx, fctx, y, p1):
    """encoder: probabilities for all symbols of an octant, chunk by chunk (tables updated after each chunk of CHUNK)."""
    n = ctx.shape[0]; a = 0
    while a < n:
        e = min(a + 512, n)
        for i in range(a, e):
            c = ctx[i]; f = fctx[i]
            pc = (tb[c, 1] + 0.4) / (tb[c, 0] + tb[c, 1] + 0.8)
            p1[i] = (np.float64(tf[f, 1]) + 6.0 * pc) / (np.float64(tf[f, 0]) + np.float64(tf[f, 1]) + 6.0)
        for i in range(a, e):
            tb[ctx[i], y[i]] += 1; tf[fctx[i], y[i]] += 1
        a = e

@jit
def k_probs(tb, tf, cx, fx, p1):
    for i in range(cx.shape[0]):
        c = cx[i]; f = fx[i]
        pc = (tb[c, 1] + 0.4) / (tb[c, 0] + tb[c, 1] + 0.8)
        p1[i] = (np.float64(tf[f, 1]) + 6.0 * pc) / (np.float64(tf[f, 0]) + np.float64(tf[f, 1]) + 6.0)

@jit
def k_upd(tb, tf, cx, fx, yy, d):
    for i in range(cx.shape[0]):
        tb[cx[i], yy[i]] += d; tf[fx[i], yy[i]] += d

@jit
def _cell_kn(Gs, ss, use, o0, o1, o2, e0, e1, e2, lx, ly, lz):
    """known flag of the box-local cell (lx, ly, lz) at level (bi, sh): 0 outside the box (as the zero-padded KN of
    stream_best), else 2 if a received voxel of an equal/finer band is inside, 1 if inside a received coarser voxel."""
    if lx < 0 or ly < 0 or lz < 0 or lx >= e0 or ly >= e1 or lz >= e2: return 0
    f = 0
    for j in range(4):
        if not use[j]: continue
        s = ss[j]; G = Gs[j]
        if s > 0: a = (o0 + lx) >> s; b = (o1 + ly) >> s; c = (o2 + lz) >> s
        else: a = o0 + lx; b = o1 + ly; c = o2 + lz
        if a < G.shape[0] and b < G.shape[1] and c < G.shape[2] and G[a, b, c]:
            if s <= 0: return 2
            f = 1
    return f

@jit
def _cell_kn_m(kns, Gs, ss, use, o0, o1, o2, e0, e1, e2, lx, ly, lz):
    """_cell_kn memoised in the flat scratch kns (-1 = not computed yet; reset by k_kn_reset after the level)."""
    if lx < 0 or ly < 0 or lz < 0 or lx >= e0 or ly >= e1 or lz >= e2: return 0
    idx = ((lx + 2) * (e1 + 4) + ly + 2) * (e2 + 4) + lz + 2; v = kns[idx]
    if v >= 0: return v
    v = _cell_kn(Gs, ss, use, o0, o1, o2, e0, e1, e2, lx, ly, lz); kns[idx] = v; return v

@jit
def k_kn_reset(kns, cc, e0, e1, e2):
    for i in range(cc.shape[0]):
        x = cc[i, 0] - 2; y = cc[i, 1] - 2; z = cc[i, 2] - 2
        kns[((x + 2) * (e1 + 4) + y + 2) * (e2 + 4) + z + 2] = -1
        for j in range(6):
            a = x + NBo[j, 0]; b = y + NBo[j, 1]; c = z + NBo[j, 2]
            if a >= 0 and b >= 0 and c >= 0 and a < e0 and b < e1 and c < e2: kns[((a + 2) * (e1 + 4) + b + 2) * (e2 + 4) + c + 2] = -1

@jit
def _cell_ctx(K, kns, Gs, ss, use, o0, o1, o2, e0, e1, e2, x, y, z, k):
    """K: flat int8 scratch of the padded level grid (e+4), non-zero only at this level's candidates."""
    s1 = (e1 + 4) * (e2 + 4); s2 = e2 + 4; b = x * s1 + y * s2 + z; s = 0
    for j in range(6): s = s * 3 + K[b + NBo[j, 0] * s1 + NBo[j, 1] * s2 + NBo[j, 2]]
    n1 = 0; n2 = 0
    for j in range(12):
        v = K[b + EDo[j, 0] * s1 + EDo[j, 1] * s2 + EDo[j, 2]]
        if v == 1: n1 += 1
        elif v == 2: n2 += 1
    co = ((k * 729 + s) * (CAP_ + 1) + min(n1, CAP_)) * (CAP_ + 1) + min(n2, CAP_)
    kn = _cell_kn_m(kns, Gs, ss, use, o0, o1, o2, e0, e1, e2, x - 2, y - 2, z - 2); nb2 = 0
    for j in range(6):
        if _cell_kn_m(kns, Gs, ss, use, o0, o1, o2, e0, e1, e2, x - 2 + NBo[j, 0], y - 2 + NBo[j, 1], z - 2 + NBo[j, 2]) == 2: nb2 += 1
    c = co * NF_ + kn * 3 + min(nb2, 2)
    f = c
    for j in range(14): f = f * 3 + K[b + FINEo[j, 0] * s1 + FINEo[j, 1] * s2 + FINEo[j, 2]]
    return c, np.int64((np.uint64(f) * HMUL) >> H42)

@jit
def k_ctx2(K, kns, Gs, ss, use, o0, o1, o2, e0, e1, e2, cc, k, ctx, fctx):
    for i in range(cc.shape[0]):
        c, f = _cell_ctx(K, kns, Gs, ss, use, o0, o1, o2, e0, e1, e2, cc[i, 0], cc[i, 1], cc[i, 2], k); ctx[i] = c; fctx[i] = f

@jit
def k_level_cands(P, e0, e1, e2, K):
    """children of the parents P at this level, octant by octant (as stream_best.walk's cands): cc (padded coords,
    all octants concatenated), the octant boundaries cnt[0..8]; K (flat scratch) set to 2 at every candidate."""
    m = P.shape[0]; cnt = np.zeros(9, np.int64); s1 = (e1 + 4) * (e2 + 4); s2 = e2 + 4
    for k in range(8):
        bx = (k >> 2) & 1; by = (k >> 1) & 1; bz = k & 1; c = 0
        for i in range(m):
            if 2 * P[i, 0] + bx < e0 and 2 * P[i, 1] + by < e1 and 2 * P[i, 2] + bz < e2: c += 1
        cnt[k + 1] = cnt[k] + c
    cc = np.empty((cnt[8], 3), np.int64)
    for k in range(8):
        bx = (k >> 2) & 1; by = (k >> 1) & 1; bz = k & 1; q = cnt[k]
        for i in range(m):
            x = 2 * P[i, 0] + bx; y = 2 * P[i, 1] + by; z = 2 * P[i, 2] + bz
            if x < e0 and y < e1 and z < e2:
                cc[q, 0] = x + 2; cc[q, 1] = y + 2; cc[q, 2] = z + 2; K[(x + 2) * s1 + (y + 2) * s2 + z + 2] = 2; q += 1
    return cc, cnt

@jit
def k_parents_from(cc, y, e1, e2):
    """candidates with y == 1 in lexicographic order (= _sort(np.argwhere(K == 1) - 2))."""
    n = 0
    for i in range(y.shape[0]):
        if y[i] == 1: n += 1
    keys = np.empty(n, np.int64); idx = np.empty(n, np.int64); q = 0
    for i in range(y.shape[0]):
        if y[i] == 1: keys[q] = (cc[i, 0] * (e1 + 4) + cc[i, 1]) * (e2 + 4) + cc[i, 2]; idx[q] = i; q += 1
    o = np.argsort(keys); P = np.empty((n, 3), np.int64)
    for j in range(n):
        i = idx[o[j]]; P[j, 0] = cc[i, 0] - 2; P[j, 1] = cc[i, 1] - 2; P[j, 2] = cc[i, 2] - 2
    return P

@jit
def k_setK(K, cc, a0, a1, y, e1, e2):
    s1 = (e1 + 4) * (e2 + 4); s2 = e2 + 4
    for i in range(a0, a1): K[cc[i, 0] * s1 + cc[i, 1] * s2 + cc[i, 2]] = y[i - a0]

@jit
def k_clearK(K, cc, e1, e2):
    s1 = (e1 + 4) * (e2 + 4); s2 = e2 + 4
    for i in range(cc.shape[0]): K[cc[i, 0] * s1 + cc[i, 1] * s2 + cc[i, 2]] = 0

@jit
def k_level_enc(P, e0, e1, e2, vox, sh, K, OCC, kns, Gs, ss, use, o0, o1, o2, tb, tf):
    """encoder: one whole octree level = stream_best.walk's body for level sh: candidates of all octants, contexts,
    chunk-wise probabilities with the adaptive table updates in coding order, occupancy symbols, next parents.
    K / OCC / kns are flat scratches (zero / False / -1 outside this call): everything is O(candidates + voxels)."""
    cc, cnt = k_level_cands(P, e0, e1, e2, K); N = cnt[8]; s1 = (e1 + 4) * (e2 + 4); s2 = e2 + 4
    for i in range(vox.shape[0]): OCC[((vox[i, 0] >> sh) + 2) * s1 + ((vox[i, 1] >> sh) + 2) * s2 + (vox[i, 2] >> sh) + 2] = True
    ctx = np.empty(N, np.int64); fctx = np.empty(N, np.int64); y = np.empty(N, np.int32); p1 = np.empty(N)
    for k in range(8):
        a0 = cnt[k]; a1 = cnt[k + 1]
        if a1 == a0: continue
        for i in range(a0, a1):
            c, f = _cell_ctx(K, kns, Gs, ss, use, o0, o1, o2, e0, e1, e2, cc[i, 0], cc[i, 1], cc[i, 2], k); ctx[i] = c; fctx[i] = f
            y[i] = 1 if OCC[cc[i, 0] * s1 + cc[i, 1] * s2 + cc[i, 2]] else 0
        a = a0
        while a < a1:
            b = min(a + 512, a1)
            for i in range(a, b):
                c = ctx[i]; f = fctx[i]
                pc = (tb[c, 1] + 0.4) / (tb[c, 0] + tb[c, 1] + 0.8)
                p1[i] = (np.float64(tf[f, 1]) + 6.0 * pc) / (np.float64(tf[f, 0]) + np.float64(tf[f, 1]) + 6.0)
            for i in range(a, b):
                tb[ctx[i], y[i]] += 1; tf[fctx[i], y[i]] += 1
            a = b
        for i in range(a0, a1): K[cc[i, 0] * s1 + cc[i, 1] * s2 + cc[i, 2]] = y[i]
    k_kn_reset(kns, cc, e0, e1, e2); k_clearK(K, cc, e1, e2)
    for i in range(vox.shape[0]): OCC[((vox[i, 0] >> sh) + 2) * s1 + ((vox[i, 1] >> sh) + 2) * s2 + (vox[i, 2] >> sh) + 2] = False
    return k_parents_from(cc, y, e1, e2), ctx, fctx, y, p1

@jit
def k_band(lw, mids, logL, band):
    """band = argmin_j |lw - logL[j]| (first minimum) via the midpoints; exact ties / near-ties use the original rule."""
    nm = mids.shape[0]
    for i in range(lw.shape[0]):
        x = lw[i]; b = 0
        for j in range(nm):
            if mids[j] < x: b += 1
        near = False
        if b > 0 and abs(x - mids[b - 1]) < 1e-9: near = True
        if b < nm and abs(x - mids[b]) < 1e-9: near = True
        if near:
            b = 0; bd = abs(x - logL[0])
            for j in range(1, logL.shape[0]):
                d = abs(x - logL[j])
                if d < bd: bd = d; b = j
        band[i] = b

@jit
def k_accum(band, inv0, inv1, inv2, inv3, ztrue, CfT, cnt0, cnt1, cnt2, cnt3, zs0, zs1, zs2, zs3, cs0, cs1, cs2, cs3):
    """one pass: per band, per voxel point count, sum of ztrue and colour sums (same sequential order as np.bincount)."""
    for i in range(band.shape[0]):
        b = band[i]
        if b == 0: v = inv0[i]; cnt0[v] += 1; zs0[v] += ztrue[i]; cs0[v, 0] += CfT[0, i]; cs0[v, 1] += CfT[1, i]; cs0[v, 2] += CfT[2, i]
        elif b == 1: v = inv1[i]; cnt1[v] += 1; zs1[v] += ztrue[i]; cs1[v, 0] += CfT[0, i]; cs1[v, 1] += CfT[1, i]; cs1[v, 2] += CfT[2, i]
        elif b == 2: v = inv2[i]; cnt2[v] += 1; zs2[v] += ztrue[i]; cs2[v, 0] += CfT[0, i]; cs2[v, 1] += CfT[1, i]; cs2[v, 2] += CfT[2, i]
        else: v = inv3[i]; cnt3[v] += 1; zs3[v] += ztrue[i]; cs3[v, 0] += CfT[0, i]; cs3[v, 1] += CfT[1, i]; cs3[v, 2] += CfT[2, i]

@jit
def k_greedy(P, zf, bounds, pcl, cand, Dc, freq, tot, prev, full, syms, ctxs):
    """stream_best.greedy_band inner loops (plane vote + greedy RD choice + adaptive update). full has N+1 entries
    (sentinel NS at the end); writes syms/ctxs (N) and full[:N]; returns the new prev."""
    cnt = np.zeros(NS_ + 1); res = np.empty(full.shape[0] - 1, np.int64)
    for b in range(bounds.shape[0] - 1):
        a = bounds[b]; e = bounds[b + 1]
        for i in range(a, e):
            for j in range(NS_ + 1): cnt[j] = 0.0
            for j in range(9): cnt[full[P[i, j]]] += WV[j]
            cnt[NS_] = 1e-3
            m = 0; bv = cnt[0]
            for j in range(1, NS_ + 1):
                if cnt[j] > bv: bv = cnt[j]; m = j
            if zf[i]: c = m * (NS_ + 1) + prev + pcl[i]
            else: c = m * (NS_ + 1) + NS_ + pcl[i]
            best = cand[i, 0]; bj = Dc[i, 0] - math.log2(freq[c, best])
            for kq in range(1, 3):
                s = cand[i, kq]; jv = Dc[i, kq] - math.log2(freq[c, s])
                if jv < bj: bj = jv; best = s
            freq[c, best] += INC_; t = tot[c] + INC_
            if t > LIMIT_:
                t = 0
                for j in range(NS_): freq[c, j] = (freq[c, j] + 1) >> 1; t += freq[c, j]
            tot[c] = t
            res[i] = best; ctxs[i] = c; syms[i] = best; prev = best
        for i in range(a, e): full[i] = res[i]
    return prev

@jit
def k_rc_encode(syms, ctxs, freq, tot, out):
    """stream_best.rc_encode (carry-less range coder) on int64 freq tables; returns the number of bytes in out."""
    low = 0; rng = 0xFFFFFFFF; cache = 0; csize = 1; no = 0
    for q in range(syms.shape[0]):
        s = syms[q]; c = ctxs[q]; t = tot[c]; r = rng // t
        cum = 0
        for j in range(s): cum += freq[c, j]
        low += r * cum; rng = r * freq[c, s]
        while rng < 0x1000000:
            rng <<= 8
            if low < 0xFF000000 or low > 0xFFFFFFFF:
                carry = low >> 32; temp = cache
                while True:
                    out[no] = (temp + carry) & 0xFF; no += 1; temp = 0xFF; csize -= 1
                    if csize == 0: break
                cache = (low >> 24) & 0xFF
            csize += 1; low = (low & 0x00FFFFFF) << 8
        freq[c, s] += INC_; t = tot[c] + INC_
        if t > LIMIT_:
            t = 0
            for j in range(NS_): freq[c, j] = (freq[c, j] + 1) >> 1; t += freq[c, j]
        tot[c] = t
    for q in range(5):
        if low < 0xFF000000 or low > 0xFFFFFFFF:
            carry = low >> 32; temp = cache
            while True:
                out[no] = (temp + carry) & 0xFF; no += 1; temp = 0xFF; csize -= 1
                if csize == 0: break
            cache = (low >> 24) & 0xFF
        csize += 1; low = (low & 0x00FFFFFF) << 8
    return no

@jit
def k_rc_decode(b, P, zf, bounds, pcv, freq, tot, prev, full):
    """stream_best.RcDec.band for one record (fresh decoder on bytes b, carried freq/tot); returns new prev."""
    lb = b.shape[0]; pos = 5; rng = 0xFFFFFFFF
    code = 0
    for i in range(5): code = (code << 8) | b[i]
    cnt = np.zeros(NS_ + 1); res = np.empty(full.shape[0] - 1, np.int64)
    for q in range(bounds.shape[0] - 1):
        a = bounds[q]; e = bounds[q + 1]
        for i in range(a, e):
            for j in range(NS_ + 1): cnt[j] = 0.0
            for j in range(9): cnt[full[P[i, j]]] += WV[j]
            cnt[NS_] = 1e-3
            m = 0; bv = cnt[0]
            for j in range(1, NS_ + 1):
                if cnt[j] > bv: bv = cnt[j]; m = j
            if zf[i]: c = m * (NS_ + 1) + pcv[i] + prev
            else: c = m * (NS_ + 1) + pcv[i] + NS_
            t = tot[c]; r = rng // t; v = code // r
            if v >= t: v = t - 1
            cum = 0; s = 0
            while cum + freq[c, s] <= v: cum += freq[c, s]; s += 1
            code -= r * cum; rng = r * freq[c, s]
            while rng < 0x1000000:
                rng <<= 8
                nb = np.int64(0)
                if pos < lb: nb = np.int64(b[pos])
                code = ((code << 8) | nb) & 0xFFFFFFFFFF; pos += 1
            freq[c, s] += INC_; t = tot[c] + INC_
            if t > LIMIT_:
                t = 0
                for j in range(NS_): freq[c, j] = (freq[c, j] + 1) >> 1; t += freq[c, j]
            tot[c] = t
            res[i] = s; prev = s
        for i in range(a, e): full[i] = res[i]
    return prev

def warmup():
    """compile / load the kernels on tiny inputs (first call to numba costs seconds; cached on disk afterwards)."""
    K = np.zeros((6, 6, 6), np.int8); KN = np.zeros((6, 6, 6), np.int8); cc = np.array([[2, 2, 2], [3, 2, 2]], np.int64)
    ctx, fctx = CTX(K, KN, cc, 0)
    tb = np.zeros((NCTX, 2)); tf = np.zeros((H, 2), np.float32); y = np.zeros(2, np.int32); p1 = np.empty(2)
    k_enc_probs(tb, tf, ctx, fctx, y, p1); k_probs(tb, tf, ctx, fctx, p1); k_upd(tb, tf, ctx, fctx, y, -1)
    col = ColState(); P = np.full((2, 9), -1, np.int64); zf = np.zeros(2, bool); bounds = np.array([0, 1, 2], np.int64)
    full = np.full(3, NS, np.int64); syms = np.empty(2, np.int64); ctxs = np.empty(2, np.int64)
    k_greedy(P, zf, bounds, np.zeros(2, np.int64), np.zeros((2, 3), np.int64), np.zeros((2, 3)), col.freq, col.tot, NS, full, syms, ctxs)
    out = np.empty(64, np.uint8); n = k_rc_encode(syms, ctxs, ColState().freq, ColState().tot, out)
    k_zeff(np.zeros((4, 3)), np.tile(np.eye(3), (2, 1, 1)), np.zeros((2, 3)), 640.0, 360.0, 457.0, np.empty(4))
    P0 = np.zeros((1, 3), np.int64); vox = np.array([[0, 0, 1], [1, 1, 1]], np.int64)
    G = np.zeros((2, 2, 2), np.bool_); Gs = (G, G, G, G); ss = np.array([1, 0, -1, 2], np.int64); use = np.array([True, True, False, True])
    kns = np.full(6 * 6 * 6, -1, np.int8); K2 = np.zeros(6 * 6 * 6, np.int8); OC = np.zeros(6 * 6 * 6, np.bool_)
    Pn, ctx, fctx, y, p1 = k_level_enc(P0, 2, 2, 2, vox, 0, K2, OC, kns, Gs, ss, use, 0, 0, 0, tb, tf); k_upd(tb, tf, ctx, fctx, y, -1)
    cc2, cnt2 = k_level_cands(P0, 2, 2, 2, K2); k_setK(K2, cc2, 0, 1, np.zeros(1, np.int32), 2, 2); k_parents_from(cc2, np.zeros(len(cc2), np.int32), 2, 2)
    k_ctx2(K2, kns, Gs, ss, use, 0, 0, 0, 2, 2, 2, cc2[0:1], 0, ctx[:1], fctx[:1]); k_kn_reset(kns, cc2, 2, 2, 2); k_clearK(K2, cc2, 2, 2)
    lw = np.zeros(3); k_band(lw, np.array([-0.5, 0.5]), np.array([-1.0, 0.0, 1.0]), np.zeros(3, np.int64))
    z1 = np.zeros(3, np.int64); c1 = np.zeros(2, np.int64); f1 = np.zeros(2); s1 = np.zeros((2, 3))
    k_accum(np.array([0, 1, 3], np.int64), z1, z1, z1, z1, np.zeros(3), np.zeros((3, 3)), c1, c1.copy(), c1.copy(), c1.copy(), f1, f1.copy(), f1.copy(), f1.copy(), s1, s1.copy(), s1.copy(), s1.copy())
    bb = np.concatenate([out[:n], np.zeros(5, np.uint8)]); bb.setflags(write=False)  # the decoder gets a read-only frombuffer view
    k_rc_decode(bb, P, zf, bounds, np.zeros(2, np.int64), ColState().freq, ColState().tot, NS, np.full(3, NS, np.int64))
    k_rc_decode(bb.copy(), P, zf, bounds, np.zeros(2, np.int64), ColState().freq, ColState().tot, NS, np.full(3, NS, np.int64))

# ---------------- state (identical on both sides) with journal + persistent known grids ----------------
class ColState:
    def __init__(self): self.freq = np.ones((NCC * (NS + 1), NS), np.int64); self.tot = np.full(NCC * (NS + 1), NS, np.int64); self.prev = NS
    def copy(self):
        s = ColState.__new__(ColState); s.freq = self.freq.copy(); s.tot = self.tot.copy(); s.prev = self.prev; return s

class State:
    """tables + colour model + received voxels, with (a) a journal so trials / re-cut records roll back exactly and
    (b) per-band occupancy pyramids occ[bj][l] (bool grid at LEVELS[bj]*2^l) from which the known-flag box of any
    octree level is sliced in O(box): flag 2 = some received voxel of an equal/finer band inside the cell
    (occ[bj][sh - (bj - bi)]), flag 1 = inside a received coarser voxel (occ[bj][0] upsampled)."""
    def __init__(self, nb, dims=None):
        self.nb = nb; self.tb = np.zeros((NCTX, 2)); self.tf = np.zeros((H, 2), np.float32); self.col = ColState()
        self.known = [np.zeros((0, 3), np.int64) for _ in range(nb)]; self.cidx = [np.zeros(0, np.int64) for _ in range(nb)]
        self.skeys = [np.zeros(0, np.int64) for _ in range(nb)]; self.scidx = [np.zeros(0, np.int64) for _ in range(nb)]; self.sord = [np.zeros(0, np.int64) for _ in range(nb)]
        self.base = [0] * nb; self.log = []; self.glog = []; self.occ = [dict() for _ in range(nb)]; self.dims = dims  # dims[bi] = extent of band bi (encoder) or None (grow)
        n = int(max(np.prod(d + 4) for d in dims)) if dims is not None else 1 << 20
        self.kns = np.full(n, -1, np.int8); self.K = np.zeros(n, np.int8); self.OCC = np.zeros(n, np.bool_)  # flat level scratches (memo / octree grid / occupancy)
    def scratch(self, e):
        n = int(np.prod(e + 4))
        if n > len(self.kns): n = max(n, 2 * len(self.kns)); self.kns = np.full(n, -1, np.int8); self.K = np.zeros(n, np.int8); self.OCC = np.zeros(n, np.bool_)
        return self.kns, self.K, self.OCC
    # -- journal --
    # m[8] = the occupancy levels existing at the mark: a level created lazily after it (filled from the then-current,
    # possibly uncommitted known voxels, no journal) is deleted on rollback and rebuilt on demand from the restored known
    # set, so lazy level creation mid-trial can never leave an incomplete grid (encoder/decoder known-flag desync).
    def mark(self): return (len(self.log), self.col.copy(), list(self.known), list(self.cidx), list(self.skeys), list(self.scidx), list(self.sord), len(self.glog), [set(o) for o in self.occ])
    def rollback(self, m):
        n = m[0]
        for ctx, fctx, y in reversed(self.log[n:]): k_upd(self.tb, self.tf, ctx, fctx, y, -1)
        for bj, l, c in reversed(self.glog[m[7]:]):
            G = self.occ[bj].get(l)
            if G is not None: G[c[:, 0], c[:, 1], c[:, 2]] = False
        for bj in range(self.nb):
            for l in [l for l in self.occ[bj] if l not in m[8][bj]]: del self.occ[bj][l]
        del self.log[n:]; del self.glog[m[7]:]; self.col = m[1]; self.known = list(m[2]); self.cidx = list(m[3]); self.skeys = list(m[4]); self.scidx = list(m[5]); self.sord = list(m[6])
    def capture(self, m): return (list(self.log[m[0]:]), self.col.copy(), list(self.known), list(self.cidx), list(self.skeys), list(self.scidx), list(self.sord), list(self.glog[m[7]:]))
    def replay(self, d):
        for ctx, fctx, y in d[0]: k_upd(self.tb, self.tf, ctx, fctx, y, 1)
        for bj, l, c in d[7]:
            G = self.occ[bj].get(l)  # a level missing now is rebuilt lazily from the replayed known set
            if G is not None: G[c[:, 0], c[:, 1], c[:, 2]] = True
        self.log.extend(d[0]); self.glog.extend(d[7]); self.col = d[1]; self.known = list(d[2]); self.cidx = list(d[3]); self.skeys = list(d[4]); self.scidx = list(d[5]); self.sord = list(d[6])
    def commit(self):
        """everything in known is now final: journal dropped."""
        self.base = [len(k) for k in self.known]; self.log = []; self.glog = []
    # -- occupancy pyramids --
    def level(self, bj, l):
        G = self.occ[bj].get(l)
        if G is None:
            U = self.known[bj]
            if self.dims is not None: d = ((self.dims[bj] - 1) >> l) + 1
            else: d = ((U.max(0) >> l) + 5) if len(U) else np.array([8, 8, 8], np.int64)
            G = np.zeros(tuple(int(x) for x in d), np.bool_); self.occ[bj][l] = G
            if len(U): c = U >> l; G[c[:, 0], c[:, 1], c[:, 2]] = True  # all known voxels, no journal (see mark/rollback)
        return G
    def _occ_add(self, bj, u, l):
        if len(u) == 0: return
        G = self.occ[bj][l]; c = u >> l
        if self.dims is None:
            need = c.max(0) + 1; d = np.array(G.shape, np.int64)
            if (need > d).any():
                nd = np.maximum(d, need + (need >> 2) + 4); G2 = np.zeros(tuple(int(x) for x in nd), np.bool_); G2[:d[0], :d[1], :d[2]] = G; G = self.occ[bj][l] = G2
        prev = G[c[:, 0], c[:, 1], c[:, 2]]; G[c[:, 0], c[:, 1], c[:, 2]] = True
        nz = ~prev
        if nz.any(): self.glog.append((bj, l, c[nz]))
    # -- voxels --
    def add_geometry(self, bi, u):
        self.known[bi] = np.concatenate([self.known[bi], u])
        for l in list(self.occ[bi]): self._occ_add(bi, u, l)
    def add_colour(self, bi, u, idx):
        n0 = len(self.cidx[bi]); self.cidx[bi] = np.concatenate([self.cidx[bi], idx]); kk = keyf(u); p = np.searchsorted(self.skeys[bi], kk)
        self.skeys[bi] = np.insert(self.skeys[bi], p, kk); self.scidx[bi] = np.insert(self.scidx[bi], p, idx); self.sord[bi] = np.insert(self.sord[bi], p, np.arange(n0, n0 + len(u)))
    def knspec(self, bi, sh):
        """occupancy levels + shifts for the on-the-fly known flags of level (bi, sh) (kernel path)."""
        Gs = []; ss = np.zeros(4, np.int64); use = np.zeros(4, np.bool_)
        for bj in range(4):
            s_ = bj - bi - sh; ss[bj] = s_; use[bj] = KNOWN and len(self.known[bj]) > 0; Gs.append(self.level(bj, 0 if s_ > 0 else -s_))
        return tuple(Gs), ss, use
    def kn(self, bi, sh, e, org):
        """= stream_best_pkt.kn_grid_off(bi, sh, e, known, org) (org = 0 -> stream_best.kn_grid), O(box)."""
        KN = np.zeros(tuple(e + 4), np.int8)
        if not KNOWN: return KN
        t = _tic(); osh = org >> sh
        V = KN[2:2 + e[0], 2:2 + e[1], 2:2 + e[2]]
        for bj in range(self.nb):  # coarser bands first: flag 1, then equal/finer bands overwrite with 2
            s = bj - bi - sh
            if s <= 0 or len(self.known[bj]) == 0: continue
            G = self.level(bj, 0); d = np.array(G.shape, np.int64); ax = []; val = []
            for q in range(3):
                ix = np.arange(osh[q], osh[q] + e[q]) >> s; ax.append(np.minimum(ix, d[q] - 1)); val.append(ix < d[q])
            sub = G[np.ix_(ax[0], ax[1], ax[2])]
            if not (val[0].all() and val[1].all() and val[2].all()): sub = sub & val[0][:, None, None] & val[1][None, :, None] & val[2][None, None, :]  # beyond the grid: not covered
            V[sub] = 1
        for bj in range(self.nb):
            s = bj - bi - sh
            if s > 0 or len(self.known[bj]) == 0: continue
            G = self.level(bj, -s); d = np.array(G.shape, np.int64); lo = np.maximum(osh, 0); hi = np.minimum(osh + e, d)
            if (hi > lo).all():
                sub = G[lo[0]:hi[0], lo[1]:hi[1], lo[2]:hi[2]]
                V[lo[0] - osh[0]:hi[0] - osh[0], lo[1] - osh[1]:hi[1] - osh[1], lo[2] - osh[2]:hi[2] - osh[2]][sub] = 2
        _toc("kn", t); return KN

# ---------------- geometry walk (= stream_best.walk, journalled, kernels) ----------------
def walk(ext, D, coder, st, bi, org, vox=None):
    """= stream_best.walk (same symbols, same probabilities, same table-update schedule); journals every coded octant."""
    if not (HAVE_NUMBA and st.nb == 4): return walk_np(ext, D, coder, st, lambda sh, e: st.kn(bi, sh, e, org), vox)
    fam = constriction.stream.model.Bernoulli(perfect=False); tb, tf = st.tb, st.tf; log = st.log
    P = np.zeros((1, 3), np.int64); t = _tic()
    for Lv in range(1, D + 1):
        sh = D - Lv; e = ((ext - 1) >> sh) + 1; e0, e1, e2 = int(e[0]), int(e[1]), int(e[2]); osh = org >> sh; o0, o1, o2 = int(osh[0]), int(osh[1]), int(osh[2])
        Gs, ss, use = st.knspec(bi, sh); kns, K, OCC = st.scratch(e)
        if vox is not None:  # encoder: whole level in one kernel, one range-coder call (symbol-sequential either way)
            P, ctx, fctx, y, p1 = k_level_enc(P, e0, e1, e2, vox, sh, K, OCC, kns, Gs, ss, use, o0, o1, o2, tb, tf)
            if len(y): coder(fam, p1, y); log.append((ctx, fctx, y))
            continue
        cc, cnt = k_level_cands(P, e0, e1, e2, K); yall = np.empty(int(cnt[8]), np.int32)
        for k in range(8):
            a0, a1 = int(cnt[k]), int(cnt[k + 1])
            if a1 == a0: continue
            ck = cc[a0:a1]; ctx = np.empty(a1 - a0, np.int64); fctx = np.empty(a1 - a0, np.int64); k_ctx2(K, kns, Gs, ss, use, o0, o1, o2, e0, e1, e2, ck, k, ctx, fctx)
            y = np.empty(a1 - a0, np.int32)
            for a in range(0, a1 - a0, CHUNK):
                cx = ctx[a:a + CHUNK]; fx = fctx[a:a + CHUNK]; p1 = np.empty(len(cx)); k_probs(tb, tf, cx, fx, p1)
                yy = coder(fam, p1, None); y[a:a + CHUNK] = yy; k_upd(tb, tf, cx, fx, yy, 1)
            log.append((ctx, fctx, y)); k_setK(K, cc, a0, a1, y, e1, e2); yall[a0:a1] = y
        k_kn_reset(kns, cc, e0, e1, e2); k_clearK(K, cc, e1, e2); P = k_parents_from(cc, yall, e1, e2)
    _toc("octree", t); return P

def walk_np(ext, D, coder, st, knfn, vox=None):
    """numpy fallback (no numba): stream_best.walk with the journal."""
    fam = constriction.stream.model.Bernoulli(perfect=False); tb, tf = st.tb, st.tf; log = st.log
    P = np.zeros((1, 3), np.int64)
    for Lv in range(1, D + 1):
        sh = D - Lv; e = ((ext - 1) >> sh) + 1
        K = np.zeros(tuple(e + 4), np.int8); KN = knfn(sh, e)
        if vox is not None:
            occ = np.zeros(tuple(e + 4), bool); c = vox >> sh; occ[c[:, 0] + 2, c[:, 1] + 2, c[:, 2] + 2] = True
        cands = []
        for k in range(8):
            b = np.array([(k >> 2) & 1, (k >> 1) & 1, k & 1]); cc = 2 * P + b
            cc = cc[(cc < e).all(1)] + 2; cands.append(cc); K[cc[:, 0], cc[:, 1], cc[:, 2]] = 2
        for k in range(8):
            cc = cands[k]
            if len(cc) == 0: continue
            ctx, fctx = ctx_np(K, KN, cc, k); n = len(cc)
            y = occ[cc[:, 0], cc[:, 1], cc[:, 2]].astype(np.int32) if vox is not None else np.empty(n, np.int32)
            for a in range(0, n, CHUNK):
                cx = ctx[a:a + CHUNK]; fx = fctx[a:a + CHUNK]
                pc = (tb[cx, 1] + 0.4) / (tb[cx, 0] + tb[cx, 1] + 0.8)
                p1 = (tf[fx, 1].astype(np.float64) + ALPHA * pc) / (tf[fx, 0].astype(np.float64) + tf[fx, 1] + ALPHA)
                yy = coder(fam, p1, y[a:a + CHUNK] if vox is not None else None); y[a:a + CHUNK] = yy
                np.add.at(tb, (cx, yy), 1); np.add.at(tf, (fx, yy), 1)
            log.append((ctx, fctx, y)); K[cc[:, 0], cc[:, 1], cc[:, 2]] = y
        P = _sort(np.argwhere(K == 1) - 2)
    return P

# ---------------- colour (= stream_best greedy_band / rc_encode / RcDec.band, kernels) ----------------
def parent_ctx(bi, u, st):
    pc = np.zeros(len(u), np.int64)
    if not PCOL: return pc
    for bj in range(bi + 1, st.nb):
        ks = st.skeys[bj]; n = len(ks)
        if n == 0: continue
        q = keyf(u >> (bj - bi)); p = np.minimum(np.searchsorted(ks, q), n - 1); hit = (ks[p] == q) & (pc == 0)
        pc[hit] = 1 + st.scidx[bj][p[hit]]
    return pc

def colour_encode(u, col, pal64, st, pc):
    """-> (palette indices, colour byte stream); updates st.col like greedy_band + rc_encode + `st.col = pre`."""
    P, zf, bounds = SB._geom_ctx(u); N = len(u)
    x = col.astype(np.float64); Dm = np.sqrt(((x[:, None, :] - pal64[None]) ** 2).sum(2))
    cand = np.argsort(Dm, 1)[:, :3]; Dc = np.take_along_axis(Dm, cand, 1) / SB.LAM
    full = np.full(N + 1, NS, np.int64); syms = np.empty(N, np.int64); ctxs = np.empty(N, np.int64)
    pre = st.col.copy(); cs = st.col
    k_greedy(P, zf, bounds.astype(np.int64), pc * NCC, cand.astype(np.int64), Dc, cs.freq, cs.tot, cs.prev, full, syms, ctxs)
    out = np.empty(2 * N + 64, np.uint8); n = k_rc_encode(syms, ctxs, pre.freq, pre.tot, out)
    st.col = pre  # (as stream_best: prev of the pre-greedy state is kept; it is never used across records)
    return full[:N], out[:n].tobytes()

def colour_decode(cb, u, st, pc):
    P, zf, bounds = SB._geom_ctx(u); N = len(u); full = np.full(N + 1, NS, np.int64)
    b = np.frombuffer(cb + b"\0" * 5, np.uint8)
    st.col.prev = k_rc_decode(b, P, zf, bounds.astype(np.int64), pc * NCC, st.col.freq, st.col.tot, st.col.prev, full)
    return full[:N]

# ---------------- records (= stream_best_pkt) ----------------
def box(u):
    lo, hi = u.min(0), u.max(0); D = max(1, int(np.ceil(np.log2((hi - lo + 1).max()))))
    while True:
        org = (lo >> D) << D; ext = hi - org + 1
        if ext.max() <= (1 << D): return org, ext, D
        D += 1

def pack_hdr(org, ext, D, lg):
    h = struct.pack("<B3H", D, *map(int, org)) + struct.pack("<3B" if D <= 8 else "<3H", *map(int, ext - 1))
    return h + (struct.pack("<B", lg) if lg < 255 else struct.pack("<BI", 255, lg))

def unpack_hdr(pl):
    D, ox, oy, oz = struct.unpack("<B3H", pl[:7]); p = 7
    if D <= 8: ext = np.array(struct.unpack("<3B", pl[p:p + 3]), np.int64) + 1; p += 3
    else: ext = np.array(struct.unpack("<3H", pl[p:p + 6]), np.int64) + 1; p += 6
    lg = pl[p]; p += 1
    if lg == 255: lg = struct.unpack("<I", pl[p:p + 4])[0]; p += 4
    return np.array([ox, oy, oz], np.int64), ext, D, lg, p

def code_record(bi, u, col, st, pal64):
    enc = constriction.stream.queue.RangeEncoder()
    def coder(fam, p, y): enc.encode(y, fam, p); return y
    org, ext, D = box(u); t = _tic()
    walk(ext, D, coder, st, bi, org, u - org)
    st.add_geometry(bi, u); g = enc.get_compressed().tobytes(); _toc("walk", t); t = _tic()
    idx, cb = colour_encode(u, col, pal64, st, parent_ctx(bi, u, st)); st.add_colour(bi, u, idx); _toc("colour", t)
    payload = pack_hdr(org, ext, D, len(g)) + g + cb
    return struct.pack("<BI", bi, len(payload)) + payload

# ---------------- zeff (= codec_lod_bands.zeff(mode="min"), threaded, allocation-free) ----------------
try:
    from threadpoolctl import threadpool_limits
except Exception: threadpool_limits = None

class _ZBuf:
    """per-thread scratch so no page-faulting allocation happens per pose (identical arithmetic / op order)."""
    def __init__(self, n):
        self.D = np.empty((n, 3)); self.q = np.empty((n, 3)); self.zz = np.empty(n); self.u = np.empty(n); self.v = np.empty(n)
        self.k = np.empty(n, bool); self.t = np.empty(n, bool); self.zmin = np.empty(n)

def _zpart(P, poses, B=None):
    W, Hh, FX = L.Q.W, L.Q.H, L.Q.FX
    if B is None: B = _ZBuf(len(P))
    D, q, zz, u, v, k, t, zmin = B.D, B.q, B.zz, B.u, B.v, B.k, B.t, B.zmin; zmin[:] = np.inf
    for R, c in poses:
        np.subtract(P, c, out=D); np.matmul(D, R, out=q); z = q[:, 2]; np.maximum(z, 1e-6, out=zz)
        np.multiply(q[:, 0], FX, out=u); np.divide(u, zz, out=u); np.add(u, W / 2, out=u)
        np.multiply(q[:, 1], FX, out=v); np.divide(v, zz, out=v); np.add(v, Hh / 2, out=v)
        np.greater(z, 0.05, out=k); np.greater_equal(u, 0, out=t); np.logical_and(k, t, out=k); np.less(u, W, out=t); np.logical_and(k, t, out=k)
        np.greater_equal(v, 0, out=t); np.logical_and(k, t, out=k); np.less(v, Hh, out=t); np.logical_and(k, t, out=k)
        np.minimum(zmin, z, out=zmin, where=k)
    return zmin

@pjit
def k_zeff(P, Rs, Ps, W, Hh, FX, out):
    """fused per-point projection over all poses: same op order as numpy ((P-c)@R without FMA, as OpenBLAS's generic
    kernel; verified bit-identical at runtime by zeff_min on the first call, else the numpy path is used)."""
    n = P.shape[0]; npo = Rs.shape[0]
    for i in prange(n):
        zm = np.inf; x = P[i, 0]; y = P[i, 1]; w = P[i, 2]
        for p in range(npo):
            d0 = x - Ps[p, 0]; d1 = y - Ps[p, 1]; d2 = w - Ps[p, 2]
            q0 = d0 * Rs[p, 0, 0] + d1 * Rs[p, 1, 0] + d2 * Rs[p, 2, 0]
            q1 = d0 * Rs[p, 0, 1] + d1 * Rs[p, 1, 1] + d2 * Rs[p, 2, 1]
            z = d0 * Rs[p, 0, 2] + d1 * Rs[p, 1, 2] + d2 * Rs[p, 2, 2]
            zz = max(z, 1e-6)
            u = FX * q0 / zz + W / 2; v = FX * q1 / zz + Hh / 2
            if z > 0.05 and u >= 0 and u < W and v >= 0 and v < Hh and z < zm: zm = z
        out[i] = zm

_POOL = None; _ZB = {}; ZMODE = ["kernel" if HAVE_NUMBA else "numpy"]
def zeff_min(P, traj):
    global _POOL
    Rs, Ps = L.poses(traj, 48); poses = list(zip(Rs, Ps)); nt = max(1, min(NTHR, len(poses))); n = len(P)
    if ZMODE[0] in ("kernel", "check"):
        out = np.empty(n); k_zeff(P, np.ascontiguousarray(Rs), np.ascontiguousarray(Ps), float(L.Q.W), float(L.Q.H), float(L.Q.FX), out)
        if ZMODE[0] == "kernel": return out * L.M
        ZMODE[0] = "kernel"; ref = zeff_min(P, traj)
        if np.array_equal(ref, out * L.M): return ref
        ZMODE[0] = "numpy"; print("stream_rt: zeff kernel differs from numpy on this machine -> numpy path", file=sys.stderr); return ref
    if _ZB.get("n") != n: _ZB.clear(); _ZB["n"] = n; _ZB["b"] = [_ZBuf(n) for _ in range(nt)]
    if nt == 1: return _zpart(P, poses, _ZB["b"][0]).copy() * L.M
    if _POOL is None: _POOL = ThreadPoolExecutor(nt)
    ctx = threadpool_limits(limits=1, user_api="blas") if threadpool_limits is not None else None
    try: parts = list(_POOL.map(lambda i: _zpart(P, poses[i::nt], _ZB["b"][i]), range(nt)))
    finally:
        if ctx is not None: ctx.unregister()
    z = parts[0].copy()
    for q in parts[1:]: np.minimum(z, q, out=z)
    return z * L.M

# ---------------- encoder ----------------
class Encoder(SB.Encoder):
    def __init__(self, rate, P, C):
        super().__init__(rate, P, C); self.bpv = [6.0] * len(LEVELS); self.stats = []; self._drone = None
        lg = np.log(LEVELS); self.mids = (lg[:-1] + lg[1:]) / 2; self.logL = lg; self.CfT = np.ascontiguousarray(self.Cf.T)
        self.st = None; warmup()
        if HAVE_NUMBA: ZMODE[0] = "check"  # first slot: verify the fused zeff kernel against the numpy path

    def _setup(self, traj):
        super()._setup(traj)
        self.st = State(len(LEVELS))  # growable occupancy grids (live: the cloud grows)
        D0 = int(np.ceil(np.log2(self.bands[0]["u"].max(0).max() + 1))) + 2
        for bi in range(len(LEVELS)):  # create every occupancy level now (zero pages are lazy; marking is incremental)
            for l in range(max(1, D0 - bi)): self.st.level(bi, l)
        if os.environ.get("RT_PROF"): print("   occupancy levels: %d, %.1f MB" % (sum(len(o) for o in self.st.occ), sum(G.size for o in self.st.occ for G in o.values()) / 1e6), flush=True)

    def prepare(self, traj):
        """optional: run the one-time setup (ground frame, band voxelisation, occupancy levels, kernel self-check) as
        soon as the first pose is known, so slot 0's update() does not pay for it."""
        if self.R is None:
            self._setup(traj)
            if ZMODE[0] == "check": zeff_min(self.cenw, traj if len(traj) > 1 else np.vstack([traj[0], traj[0] + [1e-3, 0, 0, 0, 0, 0, 0, 0]]))

    def _z(self, traj, dt):
        Pc = self.cenw; inv = self.bands[0]["inv"]
        sub = traj if len(traj) > 1 else np.vstack([traj[0], traj[0] + [1e-3, 0, 0, 0, 0, 0, 0, 0]])
        z = zeff_min(Pc, sub); far = ~np.isfinite(z); z[far] = np.linalg.norm(Pc[far] - traj[-1, 1:4], axis=1) * L.M
        return z[inv], z[inv]

    def select(self, alpha, z, ztrue):
        lw = np.log(alpha * z)
        if HAVE_NUMBA and len(LEVELS) == 4:
            band = np.empty(len(lw), np.int64); k_band(lw, self.mids, self.logL, band); Bs = self.bands
            cnt = [np.zeros(B["n"], np.int64) for B in Bs]; zs = [np.zeros(B["n"]) for B in Bs]; cs = [np.zeros((B["n"], 3)) for B in Bs]
            k_accum(band, Bs[0]["inv"], Bs[1]["inv"], Bs[2]["inv"], Bs[3]["inv"], ztrue, self.CfT, cnt[0], cnt[1], cnt[2], cnt[3], zs[0], zs[1], zs[2], zs[3], cs[0], cs[1], cs[2], cs[3])
            out = []
            for bi in range(len(LEVELS) - 1, -1, -1):
                B = Bs[bi]; c = cnt[bi]; ok = c >= MINCNT
                if not ok.any() and c.max() == 0: continue  # (band == bi).any() false  <=>  no point in the band
                keep = ok.copy()
                if CONF > 0 and ok.any():
                    dens = c * (zs[bi] / np.maximum(c, 1)) ** 2; keep &= dens >= CONF * np.median(dens[ok])
                keep &= ~B["sent"]; idx = np.nonzero(keep)[0]
                if len(idx) == 0: continue
                col = cs[bi][idx] / c[idx, None]
                out.append((bi, B["u"][idx], np.round(col).astype(np.uint8), idx))
            return out
        band = np.searchsorted(self.mids, lw)
        nbnd = len(self.mids); lo = self.mids[np.maximum(band - 1, 0)]; hi = self.mids[np.minimum(band, nbnd - 1)]
        near = (np.abs(lw - lo) < 1e-9) | (np.abs(lw - hi) < 1e-9)
        if near.any(): band[near] = np.abs(lw[near][:, None] - np.log(LEVELS)[None]).argmin(1)  # exact-tie cells: original rule
        out = []
        for bi in range(len(LEVELS) - 1, -1, -1):
            B = self.bands[bi]; m = band == bi
            if not m.any(): continue
            inv = B["inv"][m]; cnt = np.bincount(inv, minlength=B["n"]); ok = cnt >= MINCNT; keep = ok.copy()
            if CONF > 0 and ok.any():
                zs = np.bincount(inv, ztrue[m], minlength=B["n"]); dens = cnt * (zs / np.maximum(cnt, 1)) ** 2
                keep &= dens >= CONF * np.median(dens[ok])
            keep &= ~B["sent"]; idx = np.nonzero(keep)[0]
            if len(idx) == 0: continue
            col = np.stack([np.bincount(inv, self.CfT[ch][m], minlength=B["n"])[idx] for ch in range(3)], 1) / cnt[idx, None]
            out.append((bi, B["u"][idx], np.round(col).astype(np.uint8), idx))
        return out

    def order(self, bi, u, dg, g):
        d = np.linalg.norm((u + 0.5) * self.vb[bi] - dg, axis=1)
        if ORDER != "block": return np.argsort(d, kind="stable")
        m = 0
        for mm in range(1, 7):
            if len(np.unique(keyf(u >> mm))) < 8 * len(u) / g: break
            m = mm
        if m == 0: return np.argsort(d, kind="stable")
        kb = keyf(u >> m); blk, inv = np.unique(kb, return_inverse=True); inv = inv.ravel()
        bc = np.stack([blk >> 42, (blk >> 21) & M21, blk & M21], 1); db = np.linalg.norm(((bc + 0.5) * (1 << m)) * self.vb[bi] - dg, axis=1)
        rank = np.argsort(np.argsort(db, kind="stable"), kind="stable")
        return np.lexsort((u[:, 2], u[:, 1], u[:, 0], rank[inv]))

    def code_pkt(self, bands, st, limit=None, abort=None):
        """= stream_best_pkt.Encoder.code_pkt with journal marks instead of State copies."""
        out = b""; nrec = 0; kept = []; pal64 = self.pal.astype(np.float64)
        dg = (self.R @ self._drone - self.o) if self._drone is not None else None
        for bi, u, col, idx in bands:
            n = len(u)
            if dg is not None and n > MINVOX:
                o = self.order(bi, u, dg, max(MINVOX, int(PKT * 8 / self.bpv[bi]))); u, col, idx = u[o], col[o], idx[o]
            i = 0; full = True
            while i < n:
                g = max(MINVOX, int(PKT * 8 / self.bpv[bi]))
                if n - i < 1.5 * g: g = n - i
                for attempt in range(2):
                    sel = _lex(u[i:i + g]); mk = st.mark(); rec = code_record(bi, u[i:i + g][sel], col[i:i + g][sel], st, pal64)
                    bpv = 8 * len(rec) / g; self.bpv[bi] = 0.5 * self.bpv[bi] + 0.5 * bpv
                    if attempt == 0 and g < n - i and abs(len(rec) - PKT) > RECUT * PKT:
                        st.rollback(mk); g = max(MINVOX, int(PKT * 8 / bpv))
                        if n - i < 1.5 * g: g = n - i
                        continue
                    break
                if limit is not None and len(out) + len(rec) > limit:
                    st.rollback(mk); g = int((limit - len(out) - 16) * 8 / max(bpv, 1e-3) * 0.9)
                    if g >= MINVOX:
                        sel = _lex(u[i:i + g]); mk = st.mark(); rec = code_record(bi, u[i:i + g][sel], col[i:i + g][sel], st, pal64)
                        if len(out) + len(rec) <= limit: out += rec; nrec += 1; i += g
                        else: st.rollback(mk)
                    full = False; break
                out += rec; nrec += 1; i += g
                if abort is not None and len(out) > abort:
                    rest = sum(len(bb[1]) * self.bpv[bb[0]] / 8 for bb in bands if bb[0] < bi)
                    return out, nrec, kept + [(bi, idx[:i])], len(out) + (n - i) * self.bpv[bi] / 8 + rest
            kept.append((bi, idx[:i]))
            if not full: break
        return out, nrec, kept, len(out)

    def trial(self, alpha, z, ztrue, limit=None, abort=None):
        st = self.st; mk = st.mark(); t = _tic(); bands = self.select(alpha, z, ztrue); _toc("select", t); t = _tic()
        b, nrec, kept, est = self.code_pkt(bands, st, limit, abort); self._nrec = nrec; self._kept = kept; self._est = est; _toc("code", t)
        t = _tic(); d = st.capture(mk); st.rollback(mk); _toc("rollback", t)
        return b, d, bands

    def update(self, k, traj, dt):
        budget = self.rate * dt; hdr = b""; self._drone = traj[-1, 1:4].astype(float)
        if self.R is None:
            self._setup(traj)
            hdr = (self.R.astype(np.float32).tobytes() + self.o.astype(np.float32).tobytes() + struct.pack("<fB", SPL, len(LEVELS))
                   + LEVELS.astype(np.float32).tobytes() + self.pal.tobytes())
        p0 = dict(PROF); t_up = _tic(); t = _tic(); z, ztrue = self._z(traj, dt)
        if TIE > 0:
            d = np.linalg.norm(self.P - traj[-1, 1:4], axis=1); z = z * (1 + TIE * d / d.max())
        _toc("zeff", t)
        bud = max(budget - len(hdr), PKT); a = max(self.alpha, ALPHA_MIN); best = None; lo = hi = None; tr = []  # >= one packet of data after the header (a tiny first-look budget must not go <= 0)
        for it in range(MAXT):
            b, st, bands = self.trial(a, z, ztrue, abort=bud); n = self._est; tr.append((a, n)); self.ntrials = it + 1
            if n <= bud:
                if best is None or n > len(best[0]): best = (b, st, bands, a, self._nrec, self._kept)
                hi = a if hi is None else min(hi, a)
                if n > FILL * bud: break
            else: lo = a if lo is None else max(lo, a)
            if lo is not None and hi is not None and hi / lo < 1.001: break
            if len(tr) >= 2 and tr[-1][1] != tr[-2][1] and tr[-1][0] != tr[-2][0] and min(tr[-1][1], tr[-2][1]) > 0:
                g = -math.log(tr[-1][1] / tr[-2][1]) / math.log(tr[-1][0] / tr[-2][0]); self.g = min(max(g, 0.7), 30.0)
            a_new = a * (n / (0.99 * bud)) ** (1 / self.g) if n > 0 else a * 0.85
            if lo is not None and hi is not None and not (lo < a_new < hi): a_new = math.sqrt(lo * hi)
            elif lo is not None and a_new <= lo: a_new = lo * 1.05
            elif hi is not None and a_new >= hi: a_new = hi / 1.05
            if a_new < ALPHA_MIN:  # floor reached: send what is left at the floor resolution (may be < budget -> idle link)
                if a <= ALPHA_MIN: break
                a_new = ALPHA_MIN
            a = a_new
        if lo is not None and (best is None or len(best[0]) < 0.9 * bud) and tr[[x[0] for x in tr].index(lo)][1] < 2.5 * bud:
            b, st, bands = self.trial(lo, z, ztrue, limit=bud); self.ntrials += 1
            if best is None or len(b) > len(best[0]): best = (b, st, bands, lo, self._nrec, self._kept)
        while best is None:
            a *= 1.5; b, st, bands = self.trial(a, z, ztrue, limit=bud)
            if len(b) <= bud or a > 1.0: best = (b, st, bands, a, self._nrec, self._kept)
        b, d, bands, a, nrec, kept = best; self.alpha = min(a, 1.0); self.st.replay(d); self.st.commit()
        for bi, idx in kept: self.bands[bi]["sent"][idx] = True
        self.stats.append(dict(k=k, pkt_bytes=len(b), nrec=nrec, nvox=int(sum(len(ki) for _, ki in kept)), hdr=len(hdr), ntrials=self.ntrials))
        if os.environ.get("DBG"): print("   slot %d: %d trials alpha %.4f %d/%.0f B %d rec tr %s" % (k, self.ntrials, a, len(b), bud, nrec, str([(round(x, 4), int(y)) for x, y in tr])))
        if os.environ.get("RT_PROF"):
            d = {q: PROF[q] - p0.get(q, 0.0) for q in PROF}
            print("   slot %d prof: trials %d update %.3f s | zeff %.3f select %.3f code %.3f (walk %.3f [octree kernels %.3f] colour %.3f) rollback %.3f | recs %d" % (
                k, self.ntrials, _time.perf_counter() - t_up, d.get("zeff", 0), d.get("select", 0), d.get("code", 0), d.get("walk", 0), d.get("octree", 0), d.get("colour", 0), d.get("rollback", 0), nrec), flush=True)
        return hdr + b

# ---------------- decoder ----------------
class Decoder:
    def __init__(self): self.R = None; self._mc = None; warmup()
    def apply(self, b):
        p = 0
        if self.R is None:
            self.R = np.frombuffer(b[:36], np.float32).reshape(3, 3).astype(float); self.o = np.frombuffer(b[36:48], np.float32).astype(float)
            self.spl, nb = struct.unpack("<fB", b[48:53]); self.levels = np.frombuffer(b[53:53 + 4 * nb], np.float32).astype(float); p = 53 + 4 * nb
            self.pal = np.frombuffer(b[p:p + 3 * NS], np.uint8).reshape(NS, 3); p += 3 * NS
            self.st = State(nb)
        while p < len(b):
            bi, n = struct.unpack("<BI", b[p:p + 5]); p += 5; self._record(bi, b[p:p + n]); p += n
    def _record(self, bi, pl):
        org, ext, D, lg, p = unpack_hdr(pl); g = pl[p:p + lg]; cb = pl[p + lg:]
        dec = constriction.stream.queue.RangeDecoder(np.frombuffer(g, np.uint32).copy())
        def coder(fam, pr, y): return dec.decode(fam, pr).astype(np.int32)
        st = self.st; t = _tic()
        u = walk(ext, D, coder, st, bi, org) + org
        st.add_geometry(bi, u); _toc("walk", t); t = _tic(); pc = parent_ctx(bi, u, st); _toc("pctx", t); t = _tic()
        idx = colour_decode(cb, u, st, pc); _toc("colour", t); t = _tic(); st.add_colour(bi, u, idx); st.commit(); _toc("add", t)
    def map(self):
        """= stream_best.Decoder.map (same points in the same order), incremental: per-band densified points and
        colours are cached and extended per record; a voxel is hidden when a finer band has a voxel inside it
        (occupancy pyramid lookup for new voxels, sorted-key lookup for the already cached coarser voxels)."""
        st = self.st; vb = self.levels / L.M; nb = len(vb); present = [bi for bi in range(nb) if len(st.known[bi])]
        if not present: return np.zeros((0, 3)), np.zeros((0, 3), np.uint8), float(vb[0]) * self.spl
        vmin = min(vb[bi] for bi in present); t = _tic()
        if self._mc is None or self._mc["vmin"] != vmin: self._mc = dict(vmin=vmin, n=[0] * nb, dense=[None] * nb, cols=[None] * nb, keep=[None] * nb)
        mc = self._mc
        for bi in present:  # finest first: the cached coarser voxels covered by new finer ones are hidden below
            v = vb[bi]; u = st.known[bi]; n = len(u); n0 = mc["n"][bi]
            if n0 == n: continue
            kk = int(np.ceil(v / vmin - 1e-3)); g = (np.arange(kk) + 0.5) / kk; flat = np.stack(np.meshgrid(g, [0.5], g, indexing="ij"), -1).reshape(-1, 3)
            un = u[n0:]; dn = (un[:, None, :] + flat[None]) * v + self.o; cn = np.repeat(self.pal[st.cidx[bi][n0:n]], len(flat), 0).reshape(n - n0, len(flat), 3)
            kp = np.ones(n - n0, bool)
            for bf in present:
                if vb[bf] < v:
                    G = st.level(bf, bi - bf); ins = (un < np.array(G.shape, np.int64)).all(1); c = un[ins]; kp[ins] &= ~G[c[:, 0], c[:, 1], c[:, 2]]
            for bc in present:
                if vb[bc] > v and mc["n"][bc] > 0 and len(st.skeys[bc]):
                    q = keyf(un >> (bc - bi)); p = np.minimum(np.searchsorted(st.skeys[bc], q), len(st.skeys[bc]) - 1); hit = st.skeys[bc][p] == q
                    pos = st.sord[bc][p[hit]]; mc["keep"][bc][pos[pos < mc["n"][bc]]] = False
            if n0 == 0: mc["dense"][bi] = dn; mc["cols"][bi] = cn; mc["keep"][bi] = kp
            else: mc["dense"][bi] = np.concatenate([mc["dense"][bi], dn]); mc["cols"][bi] = np.concatenate([mc["cols"][bi], cn]); mc["keep"][bi] = np.concatenate([mc["keep"][bi], kp])
            mc["n"][bi] = n
        pts = np.concatenate([mc["dense"][bi][mc["keep"][bi]].reshape(-1, 3) for bi in present]); cols = np.concatenate([mc["cols"][bi][mc["keep"][bi]].reshape(-1, 3) for bi in present])
        _toc("map", t); return pts @ self.R, cols, vmin * self.spl

def make_encoder(rate, P, C): return Encoder(rate, P, C)
def make_decoder(): return Decoder()
