"""decoder_rt: drop-in INCREMENTAL Decoder for the stream_best_rec bitstream (same bytes -> bit-identical map).
make_decoder() -> dec;  dec.apply(bytes)  (stream header + any run of <B band><I n>payload records, as stream_best_rec);
dec.map() -> (pts float64[M,3] world, rgb uint8[M,3], splat) == stream_best_rec.Decoder.map() after the same records;
dec.new_points_since_last() -> (pts, rgb) of the voxels appended since the previous call (densified, world frame) for
an append-only viewer; dec.generation increases when the finest received band changed (every band is re-densified
with another sub-point count / splat): the viewer must then clear, and that call returns the full map.

Incremental state (the original re-derived all of this from the whole map for EVERY record):
  * known-voxel context (stream_best.kn_grid): persistent dense int8 grids, one per octree level (cell equals or
    contains a received voxel) + one exact grid per band above the finest (cell is a received voxel of that band), updated
    per record; the walk looks up the 7 cells it needs (2 if occupied at that level, else 1 if inside a received coarser
    voxel, else 0; cells beyond the record extent read 0 like the original zero padding);
  * octree walk: one flat int8 scratch grid reused for every level (touched cells reset), the 32 context neighbours of
    an octant gathered at once, Horner sums as integer dot products, occupied cells taken from the decoded octants;
    context-table updates by np.unique counts (exact) instead of np.add.at;
  * parent-colour context and finest-band-wins supersession: sorted int64 keys per band (+ sorted parent keys per
    shift) maintained with np.insert / searchsorted;
  * voxels, palette indices and densified points in growable arrays; per-band compacted grid-frame caches appended
    per record (re-compacted from the first changed row only for a band whose alive mask changed); map() = one
    np.concatenate + ONE matmul with R on the whole array exactly like the original (a per-block matmul is NOT bit-equal
    on every BLAS: the laptop's OpenBLAS rounds row 0 differently depending on the total row count).
Optional numba kernels (env DEC_NUMBA=1 default when numba imports; 0 = pure numpy): octant context, chunk
probabilities + table update, colour range decoder incl. plane vote.  Same integer/float operation order -> identical
symbols; DEC_VERIFY=1 cross-checks every kernel output against the numpy path (slow, for perf_decoder_rt.py).
Context tables / colour model / palette / range coders are those of stream_best.  Honours CTX_KNOWN / CTX_PCOL.
Python 3.11 compatible.  Single-threaded (apply / map / new_points_since_last from one thread)."""
import os, sys, struct, math, numpy as np, constriction
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import stream_best as SB
from stream_best import ColState, RcDec, _geom_ctx, NS, NCC, WV, INC, LIMIT, keyf, NB, ED, FINE, CAP, NF, H, NCTX, KNOWN, PCOL
import lod_common as L
geo = SB.geo; CHUNK = geo.CHUNK; ALPHA = geo.ALPHA
OFFS32 = np.array(NB + ED + FINE, np.int64)                   # context neighbours of a cell, in the order of stream_best._ctx
NB7 = np.array([(0, 0, 0)] + NB, np.int64)                    # cells of the known-voxel flag (centre + 6 faces)
NB6 = np.array(NB, np.int64)
POW3_6 = 3 ** np.arange(5, -1, -1, dtype=np.int64); POW3_14 = 3 ** np.arange(13, -1, -1, dtype=np.int64); P3_14 = 3 ** 14
MULT = np.uint64(0x9E3779B97F4A7C15); SH42 = np.uint64(42); M21 = (1 << 21) - 1
C1 = CAP + 1; T_N1 = np.minimum(np.arange(13), CAP) * C1 * NF; T_N2 = np.minimum(np.arange(13), CAP) * NF; S_MUL = C1 * C1 * NF; K_MUL = 729 * S_MUL
VERIFY = os.environ.get("DEC_VERIFY", "0") == "1"
try:
    import numba; from numba import njit
    USE_NUMBA = os.environ.get("DEC_NUMBA", "1") == "1"
except Exception: USE_NUMBA = False
def keyshift(k, s): return (((k >> 42) >> s) << 42) | ((((k >> 21) & M21) >> s) << 21) | ((k & M21) >> s)   # == keyf(u >> s)
def lexsort3(v): return v[np.lexsort(v.T[::-1])]

# ============================================================== numba kernels (optional)
if USE_NUMBA:
    @njit(cache=False)
    def _nb_ctx(K, Sy, Sz, KN, kSy, kSz, EX, exo, exSy, exSz, exsh, nco, cc, ex, ey, ez, k, known, offs, koffs, nbd, ctx, fctx):
        for i in range(cc.shape[0]):
            x = cc[i, 0]; y = cc[i, 1]; z = cc[i, 2]; base = x * Sy + y * Sz + z
            s = 0
            for j in range(6): s = s * 3 + K[base + offs[j]]
            n1 = 0; n2 = 0
            for j in range(6, 18):
                v = K[base + offs[j]]
                if v == 1: n1 += 1
                elif v == 2: n2 += 1
            if n1 > CAP: n1 = CAP
            if n2 > CAP: n2 = CAP
            co = ((k * 729 + s) * C1 + n1) * C1 + n2
            if known:
                kb = x * kSy + y * kSz + z; nb2 = 0
                if KN[kb] != 0: kn = 2
                else:
                    kn = 0
                    for q in range(nco):
                        xx = (x - 2) >> exsh[q]; yy = (y - 2) >> exsh[q]; zz = (z - 2) >> exsh[q]
                        if EX[exo[q] + (xx + 2) * exSy[q] + (yy + 2) * exSz[q] + zz + 2] != 0: kn = 1
                for j in range(6):
                    xn = x + nbd[j, 0]; yn = y + nbd[j, 1]; zn = z + nbd[j, 2]
                    if xn - 2 < ex and yn - 2 < ey and zn - 2 < ez:
                        if KN[kb + koffs[j]] != 0: nb2 += 1
                if nb2 > 2: nb2 = 2
                c = co * NF + kn * 3 + nb2
            else: c = co * NF
            f = c
            for j in range(18, 32): f = f * 3 + K[base + offs[j]]
            ctx[i] = c; fctx[i] = np.int64((np.uint64(f) * MULT) >> SH42)

    @njit(cache=False)
    def _nb_probs(tb, tf, ctx, fctx, a, b, p1):
        for i in range(a, b):
            c = ctx[i]; fx = fctx[i]; t0 = tb[c, 0]; t1 = tb[c, 1]
            pc = (t1 + 0.4) / ((t0 + t1) + 0.8)
            f0 = np.float64(tf[fx, 0]); f1 = np.float64(tf[fx, 1])
            p1[i - a] = (f1 + ALPHA * pc) / ((f0 + f1) + ALPHA)

    @njit(cache=False)
    def _nb_upd(tb, tf, ctx, fctx, yy, a, b):
        for i in range(a, b):
            y = yy[i - a]; tb[ctx[i], y] += 1.0; tf[fctx[i], y] += np.float32(1.0)

    @njit(cache=False)
    def _nb_colour(b, pos, rng, code, freq, tot, prev, P, zf, pcv, out, wv):
        N = P.shape[0]; lb = b.shape[0]; cnt = np.zeros(NS + 1)
        for i in range(N):
            for j in range(NS + 1): cnt[j] = 0.0
            for j in range(9):
                p = P[i, j]
                if p < 0: cnt[NS] += wv[j]
                else: cnt[out[p]] += wv[j]
            cnt[NS] = 1e-3; m1 = 0; best = cnt[0]
            for j in range(1, NS + 1):
                if cnt[j] > best: best = cnt[j]; m1 = j
            c = m1 * (NS + 1) + pcv[i] + (prev if zf[i] else NS)
            t = tot[c]; r = rng // t; v = code // r
            if v >= t: v = t - 1
            cum = 0; s = 0
            while cum + freq[c, s] <= v: cum += freq[c, s]; s += 1
            code -= r * cum; rng = r * freq[c, s]
            while rng < 0x1000000:
                rng <<= 8; nxt = b[pos] if pos < lb else 0; code = ((code << 8) | nxt) & 0xFFFFFFFFFF; pos += 1
            freq[c, s] += INC; t += INC
            if t > LIMIT:
                t = 0
                for j in range(NS): freq[c, j] = (freq[c, j] + 1) >> 1; t += freq[c, j]
            tot[c] = t; out[i] = s; prev = s
        return pos, rng, code, prev

    def _warm():   # compile (or load from __pycache__) once at import, with the argument types of the real calls, so no record pays for it
        K = np.zeros(1000, np.int8); KN = np.zeros(1000, np.int8); cc = np.full((2, 3), 3, np.int64); ctx = np.zeros(2, np.int64); fctx = np.zeros(2, np.int64)
        z1 = np.zeros(1, np.int64); offs = OFFS32[:, 0] * 100 + OFFS32[:, 1] * 10 + OFFS32[:, 2]; ko = (NB7[:, 0] * 100 + NB7[:, 1] * 10 + NB7[:, 2])[1:].copy()
        for known in (True, False): _nb_ctx(K, 100, 10, KN, 100, 10, KN, z1, z1 + 100, z1 + 10, z1 + 1, 1, cc, 5, 5, 5, 0, known, offs, ko, NB6, ctx, fctx)
        tb = np.zeros((4, 2)); tf = np.zeros((4, 2), np.float32); p1 = np.zeros(2); _nb_probs(tb, tf, ctx, fctx, 0, 2, p1); _nb_upd(tb, tf, ctx, fctx, np.zeros(2, np.int32), 0, 2)
        freq = np.ones((NCC * (NS + 1), NS), np.int64); tot = np.full(NCC * (NS + 1), NS, np.int64); out = np.full(3, NS, np.int64)
        _nb_colour(np.zeros(8, np.uint8), 5, 0xFFFFFFFF, 0, freq, tot, NS, np.full((2, 9), -1, np.int64), np.zeros(2, bool), np.zeros(2, np.int64), out, WV)
    _warm()

class ColStateRT:
    """array version of stream_best.ColState for the numba colour kernel (same counts / same adaptation)."""
    def __init__(self): self.freq = np.ones((NCC * (NS + 1), NS), np.int64); self.tot = np.full(NCC * (NS + 1), NS, np.int64); self.prev = NS

class Grow:
    """append-only array with capacity doubling; view() is the filled prefix (a view, valid until the next append)."""
    def __init__(self, shape1, dtype, cap=1024): self.n = 0; self.buf = np.empty((cap,) + tuple(shape1), dtype)
    def append(self, a):
        m = len(a)
        if self.n + m > len(self.buf):
            nb = np.empty((max(2 * len(self.buf), self.n + m),) + self.buf.shape[1:], self.buf.dtype); nb[:self.n] = self.buf[:self.n]; self.buf = nb
        self.buf[self.n:self.n + m] = a; self.n += m
    def reserve(self, m):
        """append m uninitialised rows and return the view to fill."""
        if self.n + m > len(self.buf):
            nb = np.empty((max(2 * len(self.buf), self.n + m),) + self.buf.shape[1:], self.buf.dtype); nb[:self.n] = self.buf[:self.n]; self.buf = nb
        self.n += m; return self.buf[self.n - m:self.n]
    def view(self): return self.buf[:self.n]
    def clear(self): self.n = 0

class _StView:
    """read-only stand-in for stream_best.State's .known / .cidx lists (check_records_rec.py compatibility)."""
    def __init__(self, d): self._d = d
    @property
    def known(self): return self._d.known_arrays()
    @property
    def cidx(self): return self._d.cidx_arrays()

class Decoder:
    def __init__(self): self.R = None; self.generation = 0; self.vmin = None; self.numba = USE_NUMBA

    # ------------------------------------------------------------------ wire
    def apply(self, b):
        p = 0
        if self.R is None:
            self.R = np.frombuffer(b[:36], np.float32).reshape(3, 3).astype(float); self.o = np.frombuffer(b[36:48], np.float32).astype(float)
            self.spl, nb = struct.unpack("<fB", b[48:53]); self.levels = np.frombuffer(b[53:53 + 4 * nb], np.float32).astype(float); p = 53 + 4 * nb
            self.pal = np.frombuffer(b[p:p + 3 * NS], np.uint8).reshape(NS, 3); p += 3 * NS
            self._init(nb)
        while p < len(b):
            bi, n = struct.unpack("<BI", b[p:p + 5]); p += 5; self._record(bi, b[p:p + n]); p += n

    def _init(self, nb):
        self.nb = nb; self.lvl = [int(round(math.log2(self.levels[i] / self.levels[0]))) for i in range(nb)]
        for i in range(nb):
            if self.lvl[i] != i or abs(self.levels[i] - self.levels[0] * 2 ** i) > 1e-6 * self.levels[0]: raise NotImplementedError("decoder_rt needs bands level0 * 2^i (as stream_best)")
        self.tb = np.zeros((NCTX, 2)); self.tf = np.zeros((H, 2), np.float32); self.tbf = self.tb.ravel(); self.tff = self.tf.ravel()
        self.col = ColStateRT() if self.numba else ColState(); self.col_chk = ColState() if (self.numba and VERIFY) else None
        self.st = _StView(self)
        self.n = [0] * nb; self.kn = [Grow((3,), np.int64) for _ in range(nb)]; self.ci = [Grow((), np.int64) for _ in range(nb)]
        self.alive = [Grow((), bool) for _ in range(nb)]; self.alive_dirty = [-1] * nb   # first arrival position whose alive flag changed, -1 none
        self.ks = [np.zeros(0, np.int64) for _ in range(nb)]; self.kpos = [np.zeros(0, np.int64) for _ in range(nb)]
        self.ksh = [{s: np.zeros(0, np.int64) for s in range(1, nb)} for _ in range(nb)]   # sorted keys of (u >> s) per band
        self.LMAX = (nb - 1) + 24; self.E0 = np.array([64, 64, 64], np.int64); self.n_grow = 0
        self.KN = [self._newgrid(l) for l in range(self.LMAX + 1)]; self.KN_valid = [l <= nb - 1 for l in range(self.LMAX + 1)]
        self._alloc_ex(None)
        self.Kbuf = np.zeros(1 << 20, np.int8)
        self.dens = [None] * nb; self.comp = [None] * nb; self.comp_n = [0] * nb; self.kk = [1] * nb
        self._cache = None; self._vmark = [0] * nb; self._vgen = 0

    def _dims(self, l): return ((self.E0 - 1) >> l) + 5
    def _newgrid(self, l, old=None):
        d = self._dims(l); g = np.zeros(int(d[0] * d[1] * d[2]), np.int8)
        if old is not None:
            og, (oSy, oSz, od) = old; g.reshape(tuple(d))[:od[0], :od[1], :od[2]] = og.reshape(tuple(od))
        return g, (int(d[1] * d[2]), int(d[2]), d)
    def _alloc_ex(self, old):
        """exact-occupancy grids of bands 1..nb-1 (band 0 is never 'coarser'), back to back in one int8 buffer."""
        dims = [self._dims(self.lvl[b]) for b in range(self.nb)]; sizes = [0] + [int(np.prod(dims[b])) for b in range(1, self.nb)]
        off = 0; self.EXoff = np.zeros(self.nb, np.int64)
        for b in range(1, self.nb): self.EXoff[b] = off; off += sizes[b]
        self.EXbuf = np.zeros(max(off, 1), np.int8); self.EXS = [(int(d[1] * d[2]), int(d[2]), d) for d in dims]
        if old is not None:
            obuf, ooff, oS = old
            for b in range(1, self.nb):
                od = oS[b][2]; d = dims[b]
                self.EXbuf[self.EXoff[b]:self.EXoff[b] + sizes[b]].reshape(tuple(d))[:od[0], :od[1], :od[2]] = obuf[ooff[b]:ooff[b] + int(np.prod(od))].reshape(tuple(od))
    def _ensure_extent(self, bi, ext):
        need = ext << self.lvl[bi]
        if (need <= self.E0).all(): return
        self.E0 = np.maximum(self.E0, need + (need >> 3) + 8); self.n_grow += 1
        self.KN = [self._newgrid(l, self.KN[l]) for l in range(self.LMAX + 1)]; self._alloc_ex((self.EXbuf, self.EXoff, self.EXS))
    def _ensure_level(self, l):
        """KN[l] above the coarsest band is built lazily from the voxels received so far (large extents only)."""
        if self.KN_valid[l]: return
        g, (Sy, Sz, _) = self.KN[l]
        for bj in range(self.nb):
            if self.n[bj]:
                c = (self.kn[bj].view() >> (l - self.lvl[bj])) + 2; g[c[:, 0] * Sy + c[:, 1] * Sz + c[:, 2]] = 1
        self.KN_valid[l] = True

    # ------------------------------------------------------------------ one record
    def _record(self, bi, pl):
        ex_, ey, ez, D, lg = struct.unpack("<3HBI", pl[:11]); g = pl[11:11 + lg]; cb = pl[11 + lg:] + b"\0" * 5
        ext = np.array([ex_, ey, ez], np.int64); self._ensure_extent(bi, ext)
        for sh in range(D): self._ensure_level(self.lvl[bi] + sh)
        need = int(np.prod(ext + 4))
        if len(self.Kbuf) < need: self.Kbuf = np.zeros(need + (need >> 2), np.int8)
        rdec = constriction.stream.queue.RangeDecoder(np.frombuffer(g, np.uint32).copy())
        u = self._walk(bi, ext, D, rdec)
        pc = self._parent_ctx(bi, u)
        if self.numba:
            P, zf, bounds = _geom_ctx(u); out = np.full(len(u) + 1, NS, np.int64); cs = self.col
            bb = np.frombuffer(cb, np.uint8).copy()   # writable C array: same numba type as the warm-up
            _, _, _, cs.prev = _nb_colour(bb, 5, 0xFFFFFFFF, int.from_bytes(cb[:5], "big"), cs.freq, cs.tot, cs.prev, P, zf, pc * NCC, out, WV)
            idx = out[:len(u)]
            if self.col_chk is not None:
                idx2 = RcDecRT(cb, self.col_chk).band(u, pc); assert np.array_equal(idx, idx2), "numba colour kernel differs from numpy path"
        else: idx = RcDecRT(cb, self.col).band(u, pc)
        self._add(bi, u, idx)

    def _ctx_np(self, K, Sy, Sz, KN, kSy, kSz, co_, cc, e, k, offs, koffs):
        base = cc[:, 0] * Sy + cc[:, 1] * Sz + cc[:, 2]
        Kn = K[base[:, None] + offs[None, :]].astype(np.int64)                     # (n, 32): 6 faces, 12 edges, 14 fine
        s = Kn[:, :6] @ POW3_6; edv = Kn[:, 6:18]; n1 = (edv == 1).sum(1); n2 = (edv == 2).sum(1)
        ctx = s * S_MUL + T_N1[n1] + T_N2[n2] + k * K_MUL
        if KNOWN:
            kb = cc[:, 0] * kSy + cc[:, 1] * kSz + cc[:, 2]; knv = KN[kb[:, None] + koffs[None, :]] != 0
            kn = knv[:, 0].astype(np.int64) * 2
            if co_:
                v1 = np.zeros(len(cc), bool); ccu = cc - 2
                for exo, eSy, eSz, s_ in co_: c = (ccu >> s_) + 2; v1 |= self.EXbuf[exo + c[:, 0] * eSy + c[:, 1] * eSz + c[:, 2]] != 0
                kn = np.maximum(kn, v1.astype(np.int64))
            knv[(cc[:, None, :] + NB7[None] - 2 >= e).any(-1)] = False
            ctx += kn * 3 + np.minimum(knv[:, 1:].sum(1), 2)
        f = ctx * P3_14 + Kn[:, 18:] @ POW3_14
        return ctx, ((f.astype(np.uint64) * MULT) >> SH42).astype(np.int64)

    def _walk(self, bi, ext, D, rdec):
        fam = constriction.stream.model.Bernoulli(perfect=False); tb, tf, tbf, tff = self.tb, self.tf, self.tbf, self.tff; K = self.Kbuf; lvl = self.lvl[bi]
        P = np.zeros((1, 3), np.int64); nbm = self.numba; e_ = [0, 0, 0]
        B8 = np.array([[(k >> 2) & 1, (k >> 1) & 1, k & 1] for k in range(8)], np.int64)
        for Lv in range(1, D + 1):
            sh = D - Lv; e = ((ext - 1) >> sh) + 1; dims = e + 4; Sy = int(dims[1] * dims[2]); Sz = int(dims[2]); l = lvl + sh
            offs = OFFS32[:, 0] * Sy + OFFS32[:, 1] * Sz + OFFS32[:, 2]
            KN, (kSy, kSz, _) = self.KN[l]; koffs = NB7[:, 0] * kSy + NB7[:, 1] * kSz + NB7[:, 2]; koffs6 = koffs[1:].copy()
            ex_, ey, ez = int(e[0]), int(e[1]), int(e[2])
            co_ = [(int(self.EXoff[bj]), self.EXS[bj][0], self.EXS[bj][1], self.lvl[bj] - l) for bj in range(1, self.nb) if self.lvl[bj] > l and self.n[bj] > 0]
            nco = len(co_); exo, exSy, exSz, exsh = [np.array([c[i] for c in co_], np.int64) for i in range(4)]
            cc8 = (2 * P)[:, None, :] + B8[None]; ok8 = (cc8 < e).all(-1)
            cands, flats, ys = [], [], []
            for k in range(8):
                cc = cc8[ok8[:, k], k] + 2; fi = cc[:, 0] * Sy + cc[:, 1] * Sz + cc[:, 2]; cands.append(cc); flats.append(fi); K[fi] = 2
            for k in range(8):
                cc = cands[k]; fi = flats[k]; n = len(cc)
                if n == 0: ys.append(None); continue
                if nbm:
                    ctx = np.empty(n, np.int64); fctx = np.empty(n, np.int64)
                    _nb_ctx(K, Sy, Sz, KN, kSy, kSz, self.EXbuf, exo, exSy, exSz, exsh, nco, cc, ex_, ey, ez, k, KNOWN, offs, koffs6, NB6, ctx, fctx)
                    if VERIFY:
                        c2, f2 = self._ctx_np(K, Sy, Sz, KN, kSy, kSz, co_, cc, e, k, offs, koffs); assert np.array_equal(ctx, c2) and np.array_equal(fctx, f2), "numba ctx differs"
                else: ctx, fctx = self._ctx_np(K, Sy, Sz, KN, kSy, kSz, co_, cc, e, k, offs, koffs)
                y = np.empty(n, np.int32)
                for a in range(0, n, CHUNK):
                    b = min(a + CHUNK, n)
                    if nbm:
                        p1 = np.empty(b - a); _nb_probs(tb, tf, ctx, fctx, a, b, p1)
                        if VERIFY:
                            cx = ctx[a:b]; fx = fctx[a:b]; t = tb[cx]; pc = (t[:, 1] + 0.4) / (t[:, 0] + t[:, 1] + 0.8); tt = tf[fx]
                            p2 = (tt[:, 1].astype(np.float64) + ALPHA * pc) / (tt[:, 0].astype(np.float64) + tt[:, 1] + ALPHA); assert np.array_equal(p1, p2), "numba probs differ"
                        yy = rdec.decode(fam, p1).astype(np.int32); y[a:b] = yy; _nb_upd(tb, tf, ctx, fctx, yy, a, b)
                    else:
                        cx = ctx[a:b]; fx = fctx[a:b]
                        t = tb[cx]; pc = (t[:, 1] + 0.4) / (t[:, 0] + t[:, 1] + 0.8)
                        tt = tf[fx]; p1 = (tt[:, 1].astype(np.float64) + ALPHA * pc) / (tt[:, 0].astype(np.float64) + tt[:, 1] + ALPHA)
                        yy = rdec.decode(fam, p1).astype(np.int32); y[a:b] = yy
                        u_, c_ = np.unique(cx * 2 + yy, return_counts=True); tbf[u_] += c_                       # == np.add.at(tb, (cx, yy), 1)
                        u_, c_ = np.unique(fx * 2 + yy, return_counts=True); tff[u_] += c_.astype(np.float32)     # == np.add.at(tf, (fx, yy), 1)
                K[fi] = y; ys.append(y)
            occ_cells = [cands[k][ys[k] == 1] for k in range(8) if ys[k] is not None]
            P = lexsort3(np.concatenate(occ_cells) - 2) if occ_cells else np.zeros((0, 3), np.int64)
            K[np.concatenate(flats)] = 0
        return P

    def _parent_ctx(self, bi, u):
        pc = np.zeros(len(u), np.int64)
        if not PCOL: return pc
        for bj in range(bi + 1, self.nb):
            n = self.n[bj]
            if n == 0: continue
            ks = self.ks[bj]; q = keyf(u >> (bj - bi)); p = np.minimum(np.searchsorted(ks, q), n - 1); hit = (ks[p] == q) & (pc == 0)
            pc[hit] = 1 + self.ci[bj].view()[self.kpos[bj][p[hit]]]
        return pc

    def _add(self, bi, u, idx):
        nold = self.n[bi]; m = len(u); lvl = self.lvl[bi]
        self.kn[bi].append(u); self.ci[bi].append(idx); self.n[bi] = nold + m
        # known-voxel grids: occupied at this and every coarser level; exact cell of the band (bands >= 1)
        for l in range(lvl, self.LMAX + 1):
            if not self.KN_valid[l]: break
            g, (Sy, Sz, _) = self.KN[l]; c = (u >> (l - lvl)) + 2; g[c[:, 0] * Sy + c[:, 1] * Sz + c[:, 2]] = 1
        if bi > 0: eSy, eSz, _ = self.EXS[bi]; c = u + 2; self.EXbuf[self.EXoff[bi] + c[:, 0] * eSy + c[:, 1] * eSz + c[:, 2]] = 1
        # sorted keys
        newk = keyf(u); ins = np.searchsorted(self.ks[bi], newk)
        self.ks[bi] = np.insert(self.ks[bi], ins, newk); self.kpos[bi] = np.insert(self.kpos[bi], ins, np.arange(nold, nold + m))
        for s in range(1, self.nb):
            pk = np.sort(keyshift(newk, s)); self.ksh[bi][s] = np.insert(self.ksh[bi][s], np.searchsorted(self.ksh[bi][s], pk), pk)
        # supersession: new voxels already covered by finer bands; coarser voxels now covered by the new ones
        alive = np.ones(m, bool)
        for bf in range(bi):
            if self.n[bf] == 0: continue
            pk = self.ksh[bf][bi - bf]; p = np.searchsorted(pk, newk); ok = p < len(pk); ok[ok] = pk[p[ok]] == newk[ok]; alive &= ~ok
        self.alive[bi].append(alive)
        for bc in range(bi + 1, self.nb):
            if self.n[bc] == 0: continue
            q = keyshift(newk, bc - bi); ks = self.ks[bc]; p = np.minimum(np.searchsorted(ks, q), len(ks) - 1); hit = ks[p] == q
            if hit.any():
                pos = self.kpos[bc][p[hit]]; av = self.alive[bc].view(); pos = pos[av[pos]]
                if len(pos): av[pos] = False; self.alive_dirty[bc] = int(pos.min()) if self.alive_dirty[bc] < 0 else min(self.alive_dirty[bc], int(pos.min()))
        self._cache = None

    # ------------------------------------------------------------------ map
    def known_arrays(self): return [self.kn[b].view() for b in range(self.nb)] if self.R is not None else []
    def cidx_arrays(self): return [self.ci[b].view() for b in range(self.nb)] if self.R is not None else []

    def _flat(self, kk):
        g = (np.arange(kk) + 0.5) / kk
        return np.stack(np.meshgrid(g, [0.5], g, indexing="ij"), -1).reshape(-1, 3)
    def _check_vmin(self):
        """finest received band -> sub-point count per band; a change invalidates every densified cache."""
        vb = self.levels / L.M; have = [vb[b] for b in range(self.nb) if self.n[b]]
        if not have: return None
        vmin = min(have)
        if vmin != self.vmin:
            self.vmin = vmin; self.generation += 1; self._cache = None
            for b in range(self.nb):
                self.kk[b] = int(np.ceil(vb[b] / vmin - 1e-3)); kk2 = self.kk[b] ** 2; cap = (self.n[b] + 4096) * kk2   # sized for the rebuild: no doubling copy
                self.dens[b] = (Grow((3,), np.float64, cap), Grow((3,), np.uint8, cap)); self.comp[b] = None
                self.comp_n[b] = 0; self.alive_dirty[b] = -1
        return vmin
    def _dens_upto(self, b, n):
        """densify voxels [done, n) of band b (arrival order, grid frame) into the per-band cache; same expressions as the original."""
        dp, dc = self.dens[b]; kk2 = self.kk[b] ** 2; done = dp.n // kk2
        if done >= n: return
        v = self.levels[b] / L.M; flat = self._flat(self.kk[b]); u = self.kn[b].view()[done:n]; c = self.pal[self.ci[b].view()[done:n]]; m = n - done
        # == ((u[:, None, :] + flat[None]) * v + self.o).reshape(-1, 3) and np.repeat(c, kk2, 0), written in place into the caches
        p = dp.reserve(m * kk2).reshape(m, kk2, 3); np.add(u.astype(np.float64)[:, None, :], flat[None], out=p); np.multiply(p, v, out=p); np.add(p, self.o, out=p)
        dc.reserve(m * kk2).reshape(m, kk2, 3)[:] = c[:, None, :]
    def _compact(self, b):
        """alive voxels of band b densified (grid frame), arrival order.  While no voxel of the band is superseded the
        compacted cache IS the densified cache (no copy); after the first supersession it becomes a separate buffer that
        is re-done from the first changed row only."""
        n = self.n[b]; kk2 = self.kk[b] ** 2; d = self.alive_dirty[b]; self._dens_upto(b, n); dp, dc = self.dens[b]
        if self.comp[b] is None:
            if d < 0 and self.alive[b].view().all(): self.comp_n[b] = n; return dp.view(), dc.view()   # nothing superseded (also not on arrival)
            self.comp[b] = (Grow((3,), np.float64, dp.n + 4096 * kk2), Grow((3,), np.uint8, dp.n + 4096 * kk2)); self.comp_n[b] = 0; d = 0
        cp, cc = self.comp[b]
        if d >= 0 or self.comp_n[b] < n:
            al = self.alive[b].view()
            if d >= 0: a = min(d, self.comp_n[b]); cp.n = cc.n = int(np.count_nonzero(al[:a])) * kk2
            else: a = self.comp_n[b]
            m = np.repeat(al[a:n], kk2); p = dp.view()[a * kk2:n * kk2]; c = dc.view()[a * kk2:n * kk2]
            if not m.all(): p = p[m]; c = c[m]
            cp.append(p); cc.append(c); self.comp_n[b] = n; self.alive_dirty[b] = -1
        return cp.view(), cc.view()

    def map(self):
        if self.R is None or self._check_vmin() is None: return np.zeros((0, 3)), np.zeros((0, 3), np.uint8), 0.0
        if self._cache is not None: return self._cache
        pts, cols = [], []
        for b in range(self.nb):
            if self.n[b]: p, c = self._compact(b); pts.append(p); cols.append(c)
        self._cache = (np.concatenate(pts) @ self.R, np.concatenate(cols), self.vmin * self.spl)
        return self._cache

    def new_points_since_last(self):
        """(pts world float64[K,3], rgb uint8[K,3]) of the voxels appended since the previous call (only those not already
        superseded by finer voxels on arrival).  If self.generation changed since the previous call the finest band
        changed: the whole current map is returned and the viewer must clear its buffer first.  Superseded coarse voxels
        are NOT retracted (append-only viewer; the finer points are drawn on top of them)."""
        if self.R is None or self._check_vmin() is None: return np.zeros((0, 3)), np.zeros((0, 3), np.uint8)
        if self.generation != self._vgen:
            self._vgen = self.generation; self._vmark = list(self.n); p, c, _ = self.map(); return p.copy(), c.copy()
        pts, cols = [], []
        for b in range(self.nb):
            a, n = self._vmark[b], self.n[b]
            if n <= a: continue
            self._dens_upto(b, n); kk2 = self.kk[b] ** 2; dp, dc = self.dens[b]; m = np.repeat(self.alive[b].view()[a:n], kk2)
            pts.append(dp.view()[a * kk2:n * kk2][m]); cols.append(dc.view()[a * kk2:n * kk2][m]); self._vmark[b] = n
        if not pts: return np.zeros((0, 3)), np.zeros((0, 3), np.uint8)
        return np.concatenate(pts) @ self.R, np.concatenate(cols)

class RcDecRT(RcDec):
    """stream_best.RcDec (numpy path) with the plane vote done by one np.bincount per x-slice instead of np.add.at: the
    weights 3 / 1.5 / 1 add exactly in float64 in any order -> identical argmax -> identical contexts and symbols."""
    def band(self, vox, pc):
        P, zf, bounds = _geom_ctx(vox); N = len(vox); full = np.full(N + 1, NS, np.int64); pcv = pc * NCC
        rows = np.arange(0, (N + 1) * (NS + 1), NS + 1, dtype=np.int64)[:, None]; W = np.tile(WV, N + 1)
        for a, e in zip(bounds[:-1], bounds[1:]):
            n = e - a; nb = full[P[a:e]]
            cnt = np.bincount((nb + rows[:n]).ravel(), weights=W[:9 * n], minlength=n * (NS + 1)).reshape(n, NS + 1); cnt[:, NS] = 1e-3
            full[a:e] = self.run((cnt.argmax(1) * (NS + 1) + pcv[a:e]).tolist(), zf[a:e].tolist())
        return full[:N]

def make_decoder(): return Decoder()
