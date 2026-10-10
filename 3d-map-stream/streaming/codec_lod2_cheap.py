"""codec_lod2_cheap: distance-LOD voxel bands (same selection / look as codec_lod_bands) with cheaper
per-band coding.  All bands share ONE octree range-coder stream with shared context tables, ONE
palette (PAL colours, 3*PAL bytes once) and ONE continuous colour context stream (no per-band warm-up,
no per-band palette/terminator).  Coarse bands may carry one colour per BLK^3 voxel block (nearest-
neighbour decode = crisp).  Per-band headers are 9 bytes (v mm u16, ext 3xu16, depth u8).
Env: BUDGET (28900) | ALPHA; LEVELS (0.5,0.7,1.0,1.4,2.0); SPL (0.8); MINCNT ("2" or per-band list);
PAL (16); BLK (per-band colour block factor, default 1,1,2,2,2); LAM (3); SHAREDGEO (1); SHAREDCOL (1); VERBOSE."""
import os, math, struct, numpy as np, constriction
import lod_common as L
geo = L.cb.geo
E = os.environ.get
def _lst(name, d, n):
    s = [float(x) for x in E(name, d).split(",")]
    return (s * n)[:n] if len(s) < n else s[:n]

# ---------------- geometry: octree walk with external context tables ----------------
def _walk(ext, D, coder, tb, tf, vox=None):
    fam = constriction.stream.model.Bernoulli(perfect=False)
    P = np.zeros((1, 3), np.int64)
    for Lv in range(1, D + 1):
        sh = D - Lv; e = ((ext - 1) >> sh) + 1
        K = np.zeros(tuple(e + 4), np.int8)
        if vox is not None:
            occ = np.zeros(tuple(e + 4), bool); c = vox >> sh; occ[c[:, 0] + 2, c[:, 1] + 2, c[:, 2] + 2] = True
        cands = []
        for k in range(8):
            b = np.array([(k >> 2) & 1, (k >> 1) & 1, k & 1]); cc = 2 * P + b
            cc = cc[(cc < e).all(1)] + 2; cands.append(cc); K[cc[:, 0], cc[:, 1], cc[:, 2]] = 2
        for k in range(8):
            cc = cands[k]; ctx, fctx = geo._ctx(K, cc, k)
            y = occ[cc[:, 0], cc[:, 1], cc[:, 2]].astype(np.int32) if vox is not None else np.empty(len(cc), np.int32)
            for st in range(0, len(cc), geo.CHUNK):
                cx = ctx[st:st + geo.CHUNK]; fx = fctx[st:st + geo.CHUNK]
                pc = (tb[cx, 1] + 0.4) / (tb[cx, 0] + tb[cx, 1] + 0.8)
                p1 = (tf[fx, 1] + geo.ALPHA * pc) / (tf[fx, 0] + tf[fx, 1] + geo.ALPHA)
                yy = coder(fam, p1, y[st:st + geo.CHUNK] if vox is not None else None)
                y[st:st + geo.CHUNK] = yy
                np.add.at(tb, (cx, yy), 1); np.add.at(tf, (fx, yy), 1)
            K[cc[:, 0], cc[:, 1], cc[:, 2]] = y
        P = np.argwhere(K == 1) - 2
        P = P[np.lexsort(P.T[::-1])]
    return P

def _tables(): return np.zeros((geo.NCTX, 2)), np.zeros((geo.H, 2))

# ---------------- colour: NS-colour palette, context model (plane vote + previous z), range coder ----------------
INC, LIMIT = 24, 1 << 13
def _key(a): return (a[:, 0].astype(np.int64) << 42) | (a[:, 1].astype(np.int64) << 21) | a[:, 2].astype(np.int64)
PLANE = [(-1, a, b) for a in (-1, 0, 1) for b in (-1, 0, 1)]
WV = np.array([3.0 / (1 + abs(a) + abs(b)) for _, a, b in PLANE])

def _geom_ctx(vox):
    N = len(vox); K = _key(vox); P = np.full((N, 9), -1, np.int64)
    for j, dx in enumerate(PLANE):
        k = _key(vox + np.array(dx, np.int64)); p = np.minimum(np.searchsorted(K, k), N - 1); P[:, j] = np.where(K[p] == k, p, -1)
    zf = np.zeros(N, bool)
    zf[1:] = (vox[1:, 0] == vox[:-1, 0]) & (vox[1:, 1] == vox[:-1, 1]) & (vox[1:, 2] == vox[:-1, 2] + 1)
    bounds = np.r_[0, np.nonzero(np.diff(vox[:, 0]))[0] + 1, N]
    return P, zf, bounds

def _vote(P, full, NS):
    n = len(P); nb = full[P]; cnt = np.zeros((n, NS + 1))
    np.add.at(cnt, (np.repeat(np.arange(n), 9), nb.ravel()), np.tile(WV, n)); cnt[:, NS] = 1e-3
    return cnt.argmax(1)

class ColState:
    def __init__(self, NS):
        self.NS = NS; self.freq = [[1] * NS for _ in range((NS + 1) ** 2)]; self.tot = [NS] * ((NS + 1) ** 2); self.prev = NS
    def upd(self, c, s):
        f = self.freq[c]; f[s] += INC; t = self.tot[c] + INC
        if t > LIMIT:
            t = 0
            for j in range(self.NS): f[j] = (f[j] + 1) >> 1; t += f[j]
        self.tot[c] = t

def palette(rgb, NS, w=None):
    from scipy.cluster.vq import kmeans2
    x = rgb.astype(np.float32); rng = np.random.default_rng(0)
    if w is not None:  # weighted: replicate by weight
        x = np.repeat(x, np.clip(w, 1, 8).astype(int), 0)
    cent, _ = kmeans2(x[rng.choice(len(x), min(50000, len(x)), replace=False)], NS, minit="++", seed=0)
    return cent.astype(np.float64)

def greedy_band(vox, rgb, pal, st, lam, syms, ctxs):
    """greedy RD choice of palette index per (sorted) voxel, continuing state st; appends to syms/ctxs."""
    NS = st.NS; P, zf, bounds = _geom_ctx(vox)
    x = rgb.astype(np.float64); D = np.sqrt(((x[:, None, :] - pal[None]) ** 2).sum(2))
    cand = np.argsort(D, 1)[:, :3]; Dc = (np.take_along_axis(D, cand, 1) / lam).tolist(); candl = cand.tolist()
    N = len(vox); full = np.full(N + 1, NS, np.int64); log2 = math.log2; prev = st.prev
    for a, e in zip(bounds[:-1], bounds[1:]):
        m1 = (_vote(P[a:e], full, NS) * (NS + 1)).tolist(); zl = zf[a:e].tolist(); res = []
        for i in range(e - a):
            c = m1[i] + (prev if zl[i] else NS); f = st.freq[c]
            cd = candl[a + i]; dd = Dc[a + i]; best = cd[0]; bj = dd[0] - log2(f[best])
            for k in (1, 2):
                s = cd[k]; j = dd[k] - log2(f[s])
                if j < bj: bj = j; best = s
            st.upd(c, best); res.append(best); ctxs.append(c); syms.append(best); prev = best
        full[a:e] = res
    st.prev = prev
    return full[:N]

def rc_encode(syms, ctxs, NS):
    out = bytearray(); low = 0; rng = 0xFFFFFFFF; cache = 0; csize = 1; st = ColState(NS)
    def shift():
        nonlocal low, rng, cache, csize
        while rng < 0x1000000:
            rng <<= 8
            if low < 0xFF000000 or low > 0xFFFFFFFF:
                carry = low >> 32; temp = cache
                while True:
                    out.append((temp + carry) & 0xFF); temp = 0xFF; csize -= 1
                    if csize == 0: break
                cache = (low >> 24) & 0xFF
            csize += 1; low = (low & 0x00FFFFFF) << 8
    for s, c in zip(syms, ctxs):
        f = st.freq[c]; t = st.tot[c]; r = rng // t
        low += r * sum(f[:s]); rng = r * f[s]; shift(); st.upd(c, s)
    for _ in range(5):  # flush (one byte per step, as the reference coder)
        if low < 0xFF000000 or low > 0xFFFFFFFF:
            carry = low >> 32; temp = cache
            while True:
                out.append((temp + carry) & 0xFF); temp = 0xFF; csize -= 1
                if csize == 0: break
            cache = (low >> 24) & 0xFF
        csize += 1; low = (low & 0x00FFFFFF) << 8
    return bytes(out)

class RcDec:
    def __init__(self, b, NS): self.b = b; self.pos = 5; self.rng = 0xFFFFFFFF; self.code = int.from_bytes(b[:5], "big"); self.st = ColState(NS)
    def run(self, m1l, zfl):
        b = self.b; pos = self.pos; rng = self.rng; code = self.code; st = self.st; NS = st.NS; lb = len(b); prev = st.prev; res = []
        for m, z in zip(m1l, zfl):
            c = m + (prev if z else NS); f = st.freq[c]; t = st.tot[c]; r = rng // t; v = code // r
            if v >= t: v = t - 1
            cum = 0; s = 0
            while cum + f[s] <= v: cum += f[s]; s += 1
            code -= r * cum; rng = r * f[s]
            while rng < 0x1000000:
                rng <<= 8; code = ((code << 8) | (b[pos] if pos < lb else 0)) & 0xFFFFFFFFFF; pos += 1
            st.upd(c, s); res.append(s); prev = s
        self.pos = pos; self.rng = rng; self.code = code; st.prev = prev
        return res
    def band(self, vox):
        NS = self.st.NS; P, zf, bounds = _geom_ctx(vox); N = len(vox); full = np.full(N + 1, NS, np.int64)
        for a, e in zip(bounds[:-1], bounds[1:]):
            full[a:e] = self.run((_vote(P[a:e], full, NS) * (NS + 1)).tolist(), zf[a:e].tolist())
        return full[:N]

def _sort(v): return v[np.lexsort(v.T[::-1])]
def _blocks(vox, f):
    bu, inv = np.unique(vox // f, axis=0, return_inverse=True); return bu, inv.ravel()

# ---------------- codec ----------------
def _enc(Q3, c, z, o, R, alpha, levels, spl, verbose=False):
    nb = len(levels); NS = int(E("PAL", 16)); lam = float(E("LAM", 3))
    mincnt = _lst("MINCNT", "2", nb); blk = [int(x) for x in _lst("BLK", "1,1,2,2,2", nb)]
    want = alpha * z; band = np.abs(np.log(want)[:, None] - np.log(levels)[None]).argmin(1)
    bands = []
    for b, vm in enumerate(levels):
        m = band == b; v = vm / L.M
        if m.sum() == 0: bands.append(None); continue
        u, col, cnt, _ = L.voxelize(Q3[m], c[m], v, o)
        k = cnt >= mincnt[b]; u, col, cnt = u[k], col[k], cnt[k]
        if len(u) == 0: bands.append(None); continue
        o_ = np.lexsort(u.T[::-1]); u, col, cnt = u[o_], col[o_], cnt[o_]
        if blk[b] > 1:
            bu, binv = _blocks(u, blk[b]); bc = np.zeros((len(bu), 3)); np.add.at(bc, binv, col.astype(float) * cnt[:, None])
            bc /= np.bincount(binv, cnt)[:, None]; cu, cc, cw = bu, bc, np.bincount(binv, cnt)
        else: cu, cc, cw = u, col.astype(float), cnt
        bands.append((v, u, cu, cc, cw))
    live = [bd for bd in bands if bd is not None]
    # shared palette over colour units of all bands (weight ~ sqrt of point count, capped)
    allc = np.concatenate([bd[3] for bd in live]); allw = np.concatenate([bd[4] for bd in live])
    pal = palette(allc, NS, np.sqrt(allw))
    # geometry: one range coder, shared tables
    enc = constriction.stream.queue.RangeEncoder(); tb, tf = _tables(); hdrs = b""; gsz = []
    def coder(fam, p, y): enc.encode(y, fam, p); return y
    for bd in bands:
        if bd is None: hdrs += struct.pack("<H3HB", 0, 0, 0, 0, 0); continue
        v, u = bd[0], bd[1]; ext = u.max(0) + 1; D = int(np.ceil(np.log2(ext.max())))
        if not int(E("SHAREDGEO", 1)): tb, tf = _tables()
        n0 = len(enc.get_compressed()) * 4
        _walk(ext, D, coder, tb, tf, u.astype(np.int64)); gsz.append(len(enc.get_compressed()) * 4 - n0)
        hdrs += struct.pack("<H3HB", int(round(v * L.M * 1000)), *map(int, ext), D)
    g = enc.get_compressed().tobytes()
    # colour: one continuous context stream
    st = ColState(NS); syms, ctxs = [], []; idxs = []; csz = []; parts = []
    for bd in live:
        n0 = len(syms)
        if not int(E("SHAREDCOL", 1)): st = ColState(NS); syms, ctxs = [], []
        idxs.append(greedy_band(bd[2].astype(np.int64), bd[3], pal, st, lam, syms, ctxs)); csz.append(len(syms) - n0)
        if not int(E("SHAREDCOL", 1)): parts.append(rc_encode(syms, ctxs, NS))
    for k in range(NS):  # refit palette to members
        m = np.concatenate([(i == k) for i in idxs])
        if m.any(): pal[k] = allc[m].mean(0)
    palb = np.clip(np.round(pal), 0, 255).astype(np.uint8).tobytes()
    cbytes = rc_encode(syms, ctxs, NS) if int(E("SHAREDCOL", 1)) else b"".join(struct.pack("<H", len(x)) + x for x in parts)
    out = (R.astype(np.float32).tobytes() + np.array([*o], np.float32).tobytes() + struct.pack("<fBBI", spl, nb, NS, len(g))
           + bytes([x for x in blk]) + hdrs + palb + g + cbytes)
    if verbose:
        tot = len(cbytes); est = np.array(csz) / max(sum(csz), 1) * tot
        print(f"  alpha={alpha:.4f} total={len(out)} hdr={len(out)-len(g)-len(cbytes)} geo={len(g)} col={len(cbytes)}")
        j = 0
        for b, bd in enumerate(bands):
            if bd is None: print(f"   band {levels[b]:.2f}m: empty"); continue
            print(f"   band {levels[b]:.2f}m: vox={len(bd[1]):6d} colunits={len(bd[2]):6d} geo={gsz[j]:6d}B ({8*gsz[j]/len(bd[1]):.2f} b/vox)  col~{est[j]:6.0f}B ({8*est[j]/len(bd[2]):.2f} b/unit)")
            j += 1
    return out

def encode(P, rgb, traj):
    spl = float(E("SPL", 0.8)); levels = np.array([float(x) for x in E("LEVELS", "0.5,0.7,1.0,1.4,2.0").split(",")])
    LB = L._load("lodb", L._here / "codec_lod_bands.py")
    z = LB.zeff(P, traj, E("ZMODE", "min")); keep = np.isfinite(z)
    R = L.ground_frame(P, traj); Q3 = (P @ R.T)[keep]; c = rgb[keep]; o = Q3.min(0); z = z[keep]
    V = bool(E("VERBOSE"))
    if "ALPHA" in os.environ: return _enc(Q3, c, z, o, R, float(E("ALPHA")), levels, spl, V)
    budget = float(E("BUDGET", 28900)); lo, hi = np.log(0.004), np.log(0.06); best = None; ba = None
    for _ in range(int(E("ITERS", 8))):
        mid = (lo + hi) / 2; b = _enc(Q3, c, z, o, R, np.exp(mid), levels, spl)
        if len(b) <= budget: best, hi, ba = b, mid, mid
        else: lo = mid
    if best is None: best, ba = _enc(Q3, c, z, o, R, np.exp(hi), levels, spl), hi
    if V: _enc(Q3, c, z, o, R, np.exp(ba), levels, spl, True)
    return best

def decode(b):
    R = np.frombuffer(b[:36], np.float32).reshape(3, 3).astype(float); o = np.frombuffer(b[36:48], np.float32).astype(float)
    spl, nb, NS, lg = struct.unpack("<fBBI", b[48:58]); p = 58; blk = list(b[p:p + nb]); p += nb
    hd = []
    for _ in range(nb): hd.append(struct.unpack("<H3HB", b[p:p + 9])); p += 9
    pal = np.frombuffer(b[p:p + 3 * NS], np.uint8).reshape(NS, 3); p += 3 * NS
    dec = constriction.stream.queue.RangeDecoder(np.frombuffer(b[p:p + lg], np.uint32).copy()); p += lg
    def coder(fam, pr, y): return dec.decode(fam, pr).astype(np.int32)
    tb, tf = _tables(); geos = []
    for (vmm, ex, ey, ez, D) in hd:
        if vmm == 0: geos.append(None); continue
        if not int(E("SHAREDGEO", 1)): tb, tf = _tables()
        geos.append((vmm / 1000 / L.M, _sort(_walk(np.array([ex, ey, ez], np.int64), D, coder, tb, tf))))
    rd = RcDec(b[p:], NS); pts, cols = [], []; cp = p
    vmin = min(g[0] for g in geos if g is not None)
    for bi, g in enumerate(geos):
        if g is None: continue
        v, vox = g
        if not int(E("SHAREDCOL", 1)):
            n, = struct.unpack("<H", b[cp:cp + 2]); rd = RcDec(b[cp + 2:cp + 2 + n], NS); cp += 2 + n
        if blk[bi] > 1:
            bu, binv = _blocks(vox, blk[bi]); idx = rd.band(bu)[binv]
        else: idx = rd.band(vox)
        rgb = pal[idx]
        k = int(np.ceil(v / vmin - 1e-3)); gg = (np.arange(k) + 0.5) / k
        flat = np.stack(np.meshgrid(gg, [0.5], gg, indexing="ij"), -1).reshape(-1, 3)
        q = (vox[:, None, :] + flat[None]) * v + o
        pts.append(q.reshape(-1, 3)); cols.append(np.repeat(rgb, len(flat), 0))
    return np.concatenate(pts) @ R, np.concatenate(cols), float(vmin) * spl
