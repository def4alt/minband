"""PROGRESSIVE LIVE streamer: one nested voxel octree (0.5/1/2/4 m, common origin, world frame fixed from
keyframe 0, sent once with a 16-colour palette).  Chunk 0 sends the whole area as 4 m root voxels, then
REFINES: a refinement record for level L+1 -> L codes (a) one flag per not-yet-refined voxel of level L+1
(context: x-1 neighbour flag + previous flag), (b) the 8 child-occupancy bits of every flagged parent with
the octctx2 context model (neighbouring already-refined voxels give real context; unrefined ones are
'unknown'), (c) child palette indices with context (parent index, previous index).  Nothing is ever re-sent:
a region goes 4 -> 2 -> 1 -> 0.5 m over successive chunks by adding levels only.  Receiver renders the leaves.
CONF density pruning + MINCNT as the baseline; per-slot budget by warm-started bisection on alpha.
Budget fit: 6-step warm-started bisection on alpha, then (if < 97% used) 3-step bisection on a radius r (<= 60 m) around the drone
inside which points get one level finer (continuous knob: alpha alone jumps because the flat ground under the path
shares one depth).  Env: CONF (0.5) MINCNT (2) LAM (3) PROG_DBG=1 (per-record byte split) ITERS (6) FILL_ITERS (3) FILL_R (60 m)."""
import os, sys, math, struct, numpy as np, constriction
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lod_common as L, codec_lod_bands as LB
C2 = L._load("c2", L._here / "codec_lod2_cheap.py")
geo = L.cb.geo
LEVELS = np.array([0.5, 1.0, 2.0, 4.0]); NL = 4; SPL = 1.0; NS = 16
MINCNT = int(os.environ.get("MINCNT", 2)); CONF = float(os.environ.get("CONF", 0.5)); LAM = float(os.environ.get("LAM", 3))
ITERS = int(os.environ.get("ITERS", 6)); FILL_ITERS = int(os.environ.get("FILL_ITERS", 3)); FILL_R = float(os.environ.get("FILL_R", 60)); FILL_USE = float(os.environ.get("FILL_USE", 0.97)); DBG = os.environ.get("PROG_DBG") == "1"
MASK = (1 << 21) - 1
def pack(u): return (u[:, 0].astype(np.int64) << 42) | (u[:, 1].astype(np.int64) << 21) | u[:, 2].astype(np.int64)
def unpack(k): k = np.asarray(k, np.int64); return np.stack([k >> 42, (k >> 21) & MASK, k & MASK], 1)
def parent_key(k): k = np.asarray(k, np.int64); return ((k >> 43) << 42) | (((k >> 22) & (MASK >> 1)) << 21) | ((k & MASK) >> 1)

class State:
    """receiver-side octree state shared by encoder (mirror) and decoder."""
    def __init__(self):
        self.idx = [dict() for _ in range(NL)]      # level -> key -> palette index
        self.refined = [set() for _ in range(NL)]
        self.tb = np.zeros((geo.NCTX, 2), np.float32); self.tf = np.zeros((geo.H, 2), np.float32)
        self.col = AStat((NS + 1) ** 2, NS); self.fl = AStat(27, 2)
    def copy(self):
        s = State.__new__(State); s.idx = [d.copy() for d in self.idx]; s.refined = [r.copy() for r in self.refined]
        s.tb, s.tf = self.tb.copy(), self.tf.copy(); s.col, s.fl = self.col.copy(), self.fl.copy(); return s
    def keys(self, Lv): return np.fromiter(self.idx[Lv].keys(), np.int64, len(self.idx[Lv]))
    def unref_sorted(self, Lv):
        k = self.keys(Lv); r = self.refined[Lv]
        if r: k = k[np.array([x not in r for x in k.tolist()], bool)]
        return np.sort(k)


# ---------- adaptive multi-symbol range coder with explicit contexts and persistent statistics ----------
INC, LIMIT = 24, 1 << 13
class AStat:
    def __init__(self, nctx, ns): self.ns = ns; self.freq = [[1] * ns for _ in range(nctx)]; self.tot = [ns] * nctx
    def copy(self): s = AStat.__new__(AStat); s.ns = self.ns; s.freq = [f[:] for f in self.freq]; s.tot = self.tot[:]; return s
    def upd(self, c, s):
        f = self.freq[c]; f[s] += INC; t = self.tot[c] + INC
        if t > LIMIT:
            t = 0
            for j in range(self.ns): f[j] = (f[j] + 1) >> 1; t += f[j]
        self.tot[c] = t

def rc_enc(syms, ctxs, st):
    out = bytearray(); low = 0; rng = 0xFFFFFFFF; cache = 0; csize = 1
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
    for _ in range(5):
        if low < 0xFF000000 or low > 0xFFFFFFFF:
            carry = low >> 32; temp = cache
            while True:
                out.append((temp + carry) & 0xFF); temp = 0xFF; csize -= 1
                if csize == 0: break
            cache = (low >> 24) & 0xFF
        csize += 1; low = (low & 0x00FFFFFF) << 8
    return bytes(out)

class RcD:
    def __init__(self, b, st): self.b = b; self.pos = 5; self.rng = 0xFFFFFFFF; self.code = int.from_bytes(b[:5], "big"); self.st = st
    def dec(self, c):
        st = self.st; f = st.freq[c]; t = st.tot[c]; r = self.rng // t; v = self.code // r
        if v >= t: v = t - 1
        cum = 0; s = 0
        while cum + f[s] <= v: cum += f[s]; s += 1
        self.code -= r * cum; rng = r * f[s]; b = self.b; pos = self.pos; code = self.code
        while rng < 0x1000000:
            rng <<= 8; code = ((code << 8) | (b[pos] if pos < len(b) else 0)) & 0xFFFFFFFFFF; pos += 1
        self.rng = rng; self.pos = pos; self.code = code; st.upd(c, s); return s

KX, KY = 1 << 42, 1 << 21
def _nb(d, k):
    """neighbour palette index for colour context: majority of (z-1, x-1, y-1) known neighbours, else first known, else NS."""
    a = d.get(k - 1); b = d.get(k - KX); c = d.get(k - KY)
    if a is not None:
        if a == b or a == c: return a
        if b is not None and b == c: return b
        return a
    if b is not None: return b
    return c if c is not None else NS

# ---------- shared coding primitives (encoder and decoder run the same code with a coder callback) ----------
def _flag_ctx_vec(cand, f):
    def nb(off):
        kx = cand - off; p = np.minimum(np.searchsorted(cand, kx), len(cand) - 1); has = cand[p] == kx
        return np.where(has, f[p], 2)
    return nb(KX) * 9 + nb(KY) * 3 + nb(1)

def code_flags_enc(st, cand, f):
    """cand sorted keys (candidates for refinement), f 0/1 flags -> bytes."""
    return rc_enc(f.tolist(), _flag_ctx_vec(cand, f).tolist(), st.fl)

def code_flags_dec(st, cand, b):
    rd = RcD(b, st.fl); d = {}; f = []
    for k in cand.tolist():
        c = d.get(k - KX, 2) * 9 + d.get(k - KY, 2) * 3 + d.get(k - 1, 2); s = rd.dec(c); d[k] = s; f.append(s)
    return np.array(f, np.int64)

def children_walk(st, Lv, par_keys, coder, occ_keys=None):
    """code child occupancy (level Lv) of parents par_keys (level Lv+1, sorted). occ_keys: set of occupied child keys (encoder).
    returns sorted child keys."""
    pc = unpack(par_keys); pmin = pc.min(0) - 1; pext = pc.max(0) - pmin + 2
    shp = tuple(2 * pext + 4); K = np.zeros(shp, np.int8)
    # context from receiver state: children of unrefined known parents = unknown(2); of refined parents = known occupancy
    ak = st.keys(Lv + 1)
    if len(ak):
        ac = unpack(ak) - pmin; inb = ((ac >= 0) & (ac < pext)).all(1); ak, ac = ak[inb], ac[inb]
        ref = np.array([k in st.refined[Lv + 1] for k in ak.tolist()], bool)
        for kk in range(8):
            b = np.array([(kk >> 2) & 1, (kk >> 1) & 1, kk & 1]); cc = 2 * ac[~ref] + b + 2; K[cc[:, 0], cc[:, 1], cc[:, 2]] = 2
        ck = st.keys(Lv)
        if len(ck):
            c = unpack(ck) - 2 * pmin; inb = ((c >= 0) & (c < 2 * pext)).all(1); c = c[inb] + 2; K[c[:, 0], c[:, 1], c[:, 2]] = 1
    P = pc - pmin; cands = []
    for kk in range(8):
        b = np.array([(kk >> 2) & 1, (kk >> 1) & 1, kk & 1]); cc = 2 * P + b + 2; cands.append(cc); K[cc[:, 0], cc[:, 1], cc[:, 2]] = 2
    fam = constriction.stream.model.Bernoulli(perfect=False); tb, tf = st.tb, st.tf; out = []
    for kk in range(8):
        cc = cands[kk]; ctx, fctx = geo._ctx(K, cc, kk)
        if occ_keys is not None:
            ck = pack(cc - 2 + 2 * pmin); y = np.array([k in occ_keys for k in ck.tolist()], np.int32)
        else: y = np.empty(len(cc), np.int32)
        for s in range(0, len(cc), geo.CHUNK):
            cx = ctx[s:s + geo.CHUNK]; fx = fctx[s:s + geo.CHUNK]
            pcx = (tb[cx, 1].astype(np.float64) + 0.4) / (tb[cx, 0].astype(np.float64) + tb[cx, 1] + 0.8)
            p1 = (tf[fx, 1].astype(np.float64) + geo.ALPHA * pcx) / (tf[fx, 0].astype(np.float64) + tf[fx, 1] + geo.ALPHA)
            yy = coder(fam, p1, y[s:s + geo.CHUNK] if occ_keys is not None else None); y[s:s + geo.CHUNK] = yy
            np.add.at(tb, (cx, yy), 1); np.add.at(tf, (fx, yy), 1)
        K[cc[:, 0], cc[:, 1], cc[:, 2]] = y; out.append(cc[y == 1])
    c = np.concatenate(out) - 2 + 2 * pmin
    return np.sort(pack(c))

def child_col_enc(st, Lv, ckeys, rgb, pal):
    """palette index per child (sorted keys), context (parent idx, neighbour idx). returns (bytes, idx)."""
    pidx = [st.idx[Lv + 1][k] for k in parent_key(ckeys).tolist()]
    x = rgb.astype(np.float64); D = np.sqrt(((x[:, None, :] - pal[None]) ** 2).sum(2))
    cand = np.argsort(D, 1)[:, :3]; Dc = (np.take_along_axis(D, cand, 1) / LAM).tolist(); candl = cand.tolist()
    stc = st.col.copy(); d = dict(st.idx[Lv]); syms, ctxs = [], []; log2 = math.log2
    for i, (k, pi) in enumerate(zip(ckeys.tolist(), pidx)):
        c = pi * (NS + 1) + _nb(d, k); f = stc.freq[c]; cd = candl[i]; dd = Dc[i]; best = cd[0]; bj = dd[0] - log2(f[best])
        for j in (1, 2):
            s = cd[j]; v = dd[j] - log2(f[s])
            if v < bj: bj = v; best = s
        stc.upd(c, best); syms.append(best); ctxs.append(c); d[k] = best
    return rc_enc(syms, ctxs, st.col), np.array(syms, np.int64)

def child_col_dec(st, Lv, ckeys, b):
    pidx = [st.idx[Lv + 1][k] for k in parent_key(ckeys).tolist()]
    rd = RcD(b, st.col); d = dict(st.idx[Lv]); out = []
    for k, pi in zip(ckeys.tolist(), pidx):
        s = rd.dec(pi * (NS + 1) + _nb(d, k)); d[k] = s; out.append(s)
    return np.array(out, np.int64)

def root_enc(st, keys, rgb, pal):
    u = unpack(keys); umin = u.min(0); ul = u - umin; ext = ul.max(0) + 1; D = max(int(np.ceil(np.log2(ext.max()))), 1)
    enc = constriction.stream.queue.RangeEncoder()
    def coder(fam, p, y): enc.encode(y, fam, p); return y
    C2._walk(ext, D, coder, st.tb, st.tf, ul.astype(np.int64)); g = enc.get_compressed().tobytes()
    stc = C2.ColState(NS); syms, ctxs = [], []
    idx = C2.greedy_band(ul.astype(np.int64), rgb, pal, stc, LAM, syms, ctxs); c = C2.rc_encode(syms, ctxs, NS)
    return struct.pack("<3i3HBI", *map(int, umin), *map(int, ext), D, len(g)) + g + c, idx, (len(g), len(c))

def root_dec(st, b):
    x0, y0, z0, ex, ey, ez, D, lg = struct.unpack("<3i3HBI", b[:23])
    dec = constriction.stream.queue.RangeDecoder(np.frombuffer(b[23:23 + lg], np.uint32).copy())
    def coder(fam, p, y): return dec.decode(fam, p).astype(np.int32)
    ul = C2._sort(C2._walk(np.array([ex, ey, ez], np.int64), D, coder, st.tb, st.tf))
    idx = C2.RcDec(b[23 + lg:], NS).band(ul)
    return pack(ul + np.array([x0, y0, z0])), np.asarray(idx, np.int64)

# ---------------- encoder ----------------
class Encoder:
    def __init__(self, rate, P, C):
        self.rate, self.P, self.C = rate, P, C; self.R = None; self.st = State(); self.alpha = 0.02
        self.stats = []

    def _levels(self, tl, z):
        """per level: (keys, mean rgb, idx of voxels) after MINCNT / CONF pruning; from points with target level <= L."""
        out = []
        for Lv in range(NL):
            m = tl <= Lv
            if not m.any(): out.append((np.zeros(0, np.int64), np.zeros((0, 3), np.uint8))); continue
            kk = self.key0[m] if Lv == 0 else pack(self.c0[m] >> Lv)
            u, inv, cnt = np.unique(kk, return_inverse=True, return_counts=True); inv = inv.ravel()
            col = np.stack([np.bincount(inv, self.C[m][:, j], len(u)) for j in range(3)], 1) / cnt[:, None]
            keep = cnt >= MINCNT
            if CONF > 0 and keep.any():
                zs = np.bincount(inv, z[m], len(u)) / cnt; dens = cnt * zs ** 2
                keep &= dens >= CONF * np.median(dens[keep])
            out.append((u[keep], np.round(col[keep]).astype(np.uint8)))
        return out

    def _chunk(self, alpha, z, st, r=0.0):
        """build one chunk on a COPY of the state; points within ground radius r (m) of the drone get one level finer.
        returns (bytes, state, stats)."""
        st = st.copy(); tl = np.abs(np.log(alpha * z)[:, None] - np.log(LEVELS)[None]).argmin(1)
        if r > 0: tl = np.where(self.dxz < r, np.maximum(tl - 1, 0), tl)
        lv = self._levels(tl, z); out = b""; stats = dict(root=0, flags=0, geo=0, col=0, nroot=0, nref=0)
        for Lv in range(NL - 1, -1, -1):
            keys, rgb = lv[Lv]
            if Lv == NL - 1:  # new root voxels
                fresh = np.array([k not in st.idx[Lv] for k in keys.tolist()], bool); keys, rgb = keys[fresh], rgb[fresh]
                if len(keys) == 0: continue
                b, idx, (lg, lc) = root_enc(st, keys, rgb, self.pal)
                for k, i in zip(keys.tolist(), idx.tolist()): st.idx[Lv][k] = i
                out += struct.pack("<BBI", 0, Lv, len(b)) + b; stats["root"] += len(b); stats["nroot"] += len(keys); continue
            # refinement: children whose parent is known & unrefined
            pk = parent_key(keys); dP, rP = st.idx[Lv + 1], st.refined[Lv + 1]
            ok = np.array([(k in dP) and (k not in rP) for k in pk.tolist()], bool); keys, rgb, pk = keys[ok], rgb[ok], pk[ok]
            if len(keys) == 0: continue
            par = np.unique(pk); cand = st.unref_sorted(Lv + 1)
            f = np.isin(cand, par).astype(np.int64); fb = code_flags_enc(st, cand, f)
            enc = constriction.stream.queue.RangeEncoder()
            def coder(fam, p, y): enc.encode(y, fam, p); return y
            o = np.argsort(keys); keys, rgb = keys[o], rgb[o]
            ck = children_walk(st, Lv, par, coder, set(keys.tolist())); g = enc.get_compressed().tobytes()
            assert np.array_equal(ck, keys)
            cb, idx = child_col_enc(st, Lv, keys, rgb, self.pal)
            for k, i in zip(keys.tolist(), idx.tolist()): st.idx[Lv][k] = i
            st.refined[Lv + 1].update(par.tolist())
            b = struct.pack("<II", len(fb), len(g)) + fb + g + cb
            out += struct.pack("<BBI", 1, Lv, len(b)) + b
            stats["flags"] += len(fb); stats["geo"] += len(g); stats["col"] += len(cb); stats["nref"] += len(keys)
        return out, st, stats

    def update(self, k, traj, dt):
        budget = self.rate * dt; hdr = b""
        if self.R is None:
            d = np.linalg.norm(self.P - traj[0, 1:4], axis=1); m0 = d < np.percentile(d, 50)
            self.R = L.ground_frame(self.P[m0], traj); self.Q3 = self.P @ self.R.T; self.o = self.Q3.min(0) - 50 / L.M
            self.v0 = LEVELS[0] / L.M; self.c0 = np.floor((self.Q3 - self.o) / self.v0).astype(np.int64); self.key0 = pack(self.c0)
            u, inv = np.unique(pack(self.c0 >> 2), return_inverse=True); inv = inv.ravel()
            col = np.stack([np.bincount(inv, self.C[:, j], len(u)) for j in range(3)], 1) / np.bincount(inv)[:, None]
            self.pal = C2.palette(col, NS)
            self.pal = np.clip(np.round(self.pal), 0, 255).astype(np.uint8).astype(np.float64)
            hdr = (self.R.astype(np.float32).tobytes() + self.o.astype(np.float32).tobytes() + struct.pack("<ffB", SPL, self.v0, NL)
                   + self.pal.astype(np.uint8).tobytes())
        sub = traj if len(traj) > 1 else np.vstack([traj[0], traj[0] + [1e-3, 0, 0, 0, 0, 0, 0, 0]])
        z = LB.zeff(self.P, sub, "min"); far = ~np.isfinite(z); z[far] = np.linalg.norm(self.P[far] - traj[-1, 1:4], axis=1) * L.M
        q = (self.P - traj[-1, 1:4]) @ self.R.T; self.dxz = np.hypot(q[:, 0], q[:, 2]) * L.M  # ground distance to the drone
        lo, hi = np.log(self.alpha / 2), np.log(self.alpha * 2); best = None; nev = 0
        for it in range(ITERS):
            mid = (lo + hi) / 2; b, st, s = self._chunk(np.exp(mid), z, self.st); nev += 1
            if len(b) + len(hdr) <= budget: best, hi = (b, st, s, mid), mid
            else: lo = mid
        while best is None:
            hi += np.log(1.5); b, st, s = self._chunk(np.exp(hi), z, self.st); nev += 1
            if len(b) + len(hdr) <= budget or hi > np.log(1.0): best = (b, st, s, hi)
        a = best[3]; rlo, rhi = 0.0, FILL_R; rbest = 0.0
        for it in range(FILL_ITERS):  # fill the remaining budget: one level finer within radius r of the drone
            if len(best[0]) + len(hdr) > FILL_USE * budget: break
            rm = (rlo + rhi) / 2; b, st, s = self._chunk(np.exp(a), z, self.st, rm); nev += 1
            if len(b) + len(hdr) <= budget: best, rlo, rbest = (b, st, s, a), rm, rm
            else: rhi = rm
        b, st, s, a = best; self.alpha = float(np.exp(a)); self.st = st; s["alpha"] = self.alpha; s["bytes"] = len(hdr) + len(b); s["fill_r"] = rbest; s["nev"] = nev
        self.stats.append(s)
        if DBG: print(f"  chunk {k}: {s}", flush=True)
        return hdr + b

# ---------------- decoder ----------------
class Decoder:
    def __init__(self): self.R = None; self.st = State()
    def apply(self, b):
        p = 0
        if self.R is None:
            self.R = np.frombuffer(b[:36], np.float32).reshape(3, 3).astype(float); self.o = np.frombuffer(b[36:48], np.float32).astype(float)
            self.spl, self.v0, nl = struct.unpack("<ffB", b[48:57]); p = 57
            self.pal = np.frombuffer(b[p:p + 3 * NS], np.uint8).reshape(NS, 3).copy(); p += 3 * NS
        st = self.st
        while p < len(b):
            t, Lv, n = struct.unpack("<BBI", b[p:p + 6]); p += 6; rec = b[p:p + n]; p += n
            if t == 0:
                keys, idx = root_dec(st, rec)
                for k, i in zip(keys.tolist(), idx.tolist()): st.idx[Lv][k] = i
            else:
                lf, lg = struct.unpack("<II", rec[:8]); q = 8
                cand = st.unref_sorted(Lv + 1); f = code_flags_dec(st, cand, rec[q:q + lf]); q += lf; par = cand[f == 1]
                dec = constriction.stream.queue.RangeDecoder(np.frombuffer(rec[q:q + lg], np.uint32).copy()); q += lg
                def coder(fam, pr, y): return dec.decode(fam, pr).astype(np.int32)
                keys = children_walk(st, Lv, par, coder); idx = child_col_dec(st, Lv, keys, rec[q:])
                for k, i in zip(keys.tolist(), idx.tolist()): st.idx[Lv][k] = i
                st.refined[Lv + 1].update(par.tolist())
    def map(self):
        st = self.st; leaves = []
        for Lv in range(NL):
            k = st.keys(Lv)
            if len(k) == 0: continue
            r = st.refined[Lv]
            if r: k = k[np.array([x not in r for x in k.tolist()], bool)]
            if len(k) == 0: continue
            idx = np.array([st.idx[Lv][x] for x in k.tolist()], np.int64)
            leaves.append((Lv, unpack(k), self.pal[idx]))
        lmin = min(l for l, _, _ in leaves); vmin = self.v0 * 2 ** lmin; pts, cols = [], []
        for Lv, u, c in leaves:
            v = self.v0 * 2 ** Lv; kk = 2 ** (Lv - lmin); g = (np.arange(kk) + 0.5) / kk
            flat = np.stack(np.meshgrid(g, [0.5], g, indexing="ij"), -1).reshape(-1, 3)
            pts.append(((u[:, None, :] + flat[None]) * v + self.o).reshape(-1, 3)); cols.append(np.repeat(c, len(flat), 0))
        return np.concatenate(pts) @ self.R, np.concatenate(cols), vmin * self.spl

def make_encoder(rate, P, C): return Encoder(rate, P, C)
def make_decoder(): return Decoder()
