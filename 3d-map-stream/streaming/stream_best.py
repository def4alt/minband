"""stream_best: SYNTHESIS = stream_ctx (verified best: carried context tables, one palette, parent-colour ctx)
+ fast encoder from stream_sched/stream_geo ideas: per-band voxel ids precomputed once (bincount selection), zeff on
0.5 m voxel centres, O(1) colour-state copies, capped warm-started secant (<=4 trials), optional causal view
prediction (ZM=sched: velocity-extrapolated next pose + flown path, like stream_sched; measured LIVE 18.05 / FINAL 18.91
vs ZM=base 18.01 / 19.03 -> OFF by default). Env: ZM base|sched, MAXT 7 trials, FILL 0.98, TIE 0.01, CTX_* as stream_ctx.
Original stream_ctx description follows.

stream_ctx: LIVE streamer = stream_lod_conf (LOD bands 0.5/1/2/4 m, CONF pruning, per-slot budget) WITHOUT
restart overhead between chunks:
  * geometry: ONE octree range-coder stream per chunk for all bands; the adaptive context tables (coarse ctx +
    hashed fine ctx) are CARRIED from chunk to chunk on both sides (deterministic -> decoder stays in sync);
  * KNOWN-VOXEL CONTEXT: every octree cell gets a flag from the voxels the receiver already holds
    (0 = not covered, 1 = inside an already-received coarser voxel, 2 = exactly a received voxel / contains received
    finer voxels) + count of face neighbours with flag 2.  New voxels are mostly refinements of received coarse ones,
    so this is the big win (cells outside received coverage are ~certainly empty, cells inside are ~1/2^s occupied).
    Bands inside a chunk are coded coarse -> fine so the coarse band also serves as context for the finer one;
  * colour: ONE 16-colour palette for the whole stream, fitted once on the sender's map, sent once (48 B); greedy RD
    index choice (codec_col_greedy); the colour context model (plane vote + previous z) is carried across chunks and
    bands, one range-coder stream per chunk;
  * per-band record = 8 B (band id, extent, depth), no per-band palette / counts / lengths.
Budget fit: warm-started power-law (bytes ~ alpha^-g) secant + bracket, ~3-4 trials per slot.
Env: CTX_SHARE=0 resets the context tables every chunk, CTX_KNOWN=0 disables the known-voxel flag (ablation)."""
import os, sys, struct, copy, math, numpy as np, constriction
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lod_common as L, codec_lod_bands as LB
geo = L.cb.geo
LEVELS = np.array([0.5, 1.0, 2.0, 4.0]); SPL = 1.0; MINCNT = 2; CONF = float(os.environ.get("CONF", 0.5))
NS = 16; LAM = float(os.environ.get("LAM", 3)); NF = 9
SHARE = os.environ.get("CTX_SHARE", "1") == "1"; KNOWN = os.environ.get("CTX_KNOWN", "1") == "1"; PCOL = os.environ.get("CTX_PCOL", "1") == "1"
NCC = (NS + 1) ** 2
NB, ED, FINE, CAP = geo.NB, geo.ED, geo.FINE, geo.CAP
NCTX = geo.NCTX * NF; H = geo.H
keyf = lambda u: (u[:, 0].astype(np.int64) << 42) | (u[:, 1].astype(np.int64) << 21) | u[:, 2].astype(np.int64)
def _sort(v): return v[np.lexsort(v.T[::-1])]

# ---------------- state carried across chunks (identical on both sides) ----------------
class State:
    def __init__(self, nb):
        self.tb = np.zeros((NCTX, 2)); self.tf = np.zeros((H, 2), np.float32)
        self.col = ColState(); self.known = [np.zeros((0, 3), np.int64) for _ in range(nb)]; self.cidx = [np.zeros(0, np.int64) for _ in range(nb)]
    def copy(self):
        s = State.__new__(State); s.tb = self.tb.copy(); s.tf = self.tf.copy(); s.col = self.col.copy(); s.known = list(self.known); s.cidx = list(self.cidx); return s
    def reset_tables(self):
        self.tb[:] = 0; self.tf[:] = 0; self.col = ColState()

# ---------------- geometry: octree walk, known-voxel flag in the context ----------------
def kn_grid(bi, sh, e, known):
    """int8 grid (padded by 2) at octree level with cell = LEVELS[bi]*2^sh: 0 none / 1 inside coarser received voxel / 2 exact."""
    KN = np.zeros(tuple(e + 4), np.int8)
    if not KNOWN: return KN
    for val in (1, 2):
        for bj, U in enumerate(known):
            if len(U) == 0: continue
            s = int(round(math.log2(LEVELS[bj] / LEVELS[bi]))) - sh
            if (val == 1) != (s > 0): continue
            if s > 0:
                # keep only coarse voxels that overlap the grid
                hi = (e - 1) >> s; U = U[(U >= 0).all(1) & (U <= hi).all(1)]
                if len(U) == 0: continue
                r = np.arange(1 << s); offs = np.stack(np.meshgrid(r, r, r, indexing="ij"), -1).reshape(-1, 3)
                cells = ((U << s)[:, None, :] + offs[None]).reshape(-1, 3)
            elif s == 0: cells = U
            else: cells = U >> (-s)
            m = (cells >= 0).all(1) & (cells < e).all(1); cells = cells[m] + 2
            KN[cells[:, 0], cells[:, 1], cells[:, 2]] = val
    return KN

def _ctx(K, KN, cc, k):
    g = lambda G, o: G[cc[:, 0] + o[0], cc[:, 1] + o[1], cc[:, 2] + o[2]].astype(np.int64)
    s = np.zeros(len(cc), np.int64)
    for o in NB: s = s * 3 + g(K, o)
    n1 = np.zeros(len(cc), np.int64); n2 = np.zeros(len(cc), np.int64)
    for o in ED: v = g(K, o); n1 += v == 1; n2 += v == 2
    co = ((k * 729 + s) * (CAP + 1) + np.minimum(n1, CAP)) * (CAP + 1) + np.minimum(n2, CAP)
    kn = g(KN, (0, 0, 0)); nb2 = np.zeros(len(cc), np.int64)
    for o in NB: nb2 += g(KN, o) == 2
    ctx = co * NF + kn * 3 + np.minimum(nb2, 2)
    f = ctx.copy()
    for o in FINE: f = f * 3 + g(K, o)
    return ctx, ((f.astype(np.uint64) * np.uint64(0x9E3779B97F4A7C15)) >> np.uint64(42)).astype(np.int64)

def walk(ext, D, coder, st, knfn, vox=None):
    fam = constriction.stream.model.Bernoulli(perfect=False); tb, tf = st.tb, st.tf
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
            ctx, fctx = _ctx(K, KN, cc, k)
            y = occ[cc[:, 0], cc[:, 1], cc[:, 2]].astype(np.int32) if vox is not None else np.empty(len(cc), np.int32)
            for a in range(0, len(cc), geo.CHUNK):
                cx = ctx[a:a + geo.CHUNK]; fx = fctx[a:a + geo.CHUNK]
                pc = (tb[cx, 1] + 0.4) / (tb[cx, 0] + tb[cx, 1] + 0.8)
                p1 = (tf[fx, 1].astype(np.float64) + geo.ALPHA * pc) / (tf[fx, 0].astype(np.float64) + tf[fx, 1] + geo.ALPHA)
                yy = coder(fam, p1, y[a:a + geo.CHUNK] if vox is not None else None); y[a:a + geo.CHUNK] = yy
                np.add.at(tb, (cx, yy), 1); np.add.at(tf, (fx, yy), 1)
            K[cc[:, 0], cc[:, 1], cc[:, 2]] = y
        P = _sort(np.argwhere(K == 1) - 2)
    return P

# ---------------- colour: carried context model (plane vote + previous z), range coder ----------------
INC, LIMIT = 24, 1 << 13
PLANE = [(-1, a, b) for a in (-1, 0, 1) for b in (-1, 0, 1)]
WV = np.array([3.0 / (1 + abs(a) + abs(b)) for _, a, b in PLANE])
def parent_ctx(bi, u, st):
    """per voxel: 0 if no received coarser voxel contains it, else 1 + palette index of the nearest such parent."""
    pc = np.zeros(len(u), np.int64)
    if not PCOL: return pc
    for bj in range(bi + 1, len(st.known)):
        kv = st.known[bj]; ci = st.cidx[bj]; n = min(len(kv), len(ci))
        if n == 0: continue
        kk = keyf(kv[:n]); o = np.argsort(kk); ks = kk[o]
        q = keyf(u >> (bj - bi)); p = np.minimum(np.searchsorted(ks, q), n - 1); hit = (ks[p] == q) & (pc == 0)
        pc[hit] = 1 + ci[:n][o[p[hit]]]
    return pc

class ColState:
    def __init__(self): self.freq = [[1] * NS for _ in range(NCC * (NS + 1))]; self.tot = [NS] * (NCC * (NS + 1)); self.prev = NS
    def copy(self):
        s = ColState.__new__(ColState); s.freq = [list(f) for f in self.freq]; s.tot = list(self.tot); s.prev = self.prev; return s
    def upd(self, c, s):
        f = self.freq[c]; f[s] += INC; t = self.tot[c] + INC
        if t > LIMIT:
            t = 0
            for j in range(NS): f[j] = (f[j] + 1) >> 1; t += f[j]
        self.tot[c] = t

def _geom_ctx(vox):
    N = len(vox); K = keyf(vox); P = np.full((N, 9), -1, np.int64)
    for j, dx in enumerate(PLANE):
        k = keyf(vox + np.array(dx, np.int64)); p = np.minimum(np.searchsorted(K, k), N - 1); P[:, j] = np.where(K[p] == k, p, -1)
    zf = np.zeros(N, bool)
    zf[1:] = (vox[1:, 0] == vox[:-1, 0]) & (vox[1:, 1] == vox[:-1, 1]) & (vox[1:, 2] == vox[:-1, 2] + 1)
    return P, zf, np.r_[0, np.nonzero(np.diff(vox[:, 0]))[0] + 1, N]

def _vote(P, full):
    n = len(P); nb = full[P]; cnt = np.zeros((n, NS + 1))
    np.add.at(cnt, (np.repeat(np.arange(n), 9), nb.ravel()), np.tile(WV, n)); cnt[:, NS] = 1e-3
    return cnt.argmax(1)

def greedy_band(vox, rgb, pal, st, syms, ctxs, pc):
    P, zf, bounds = _geom_ctx(vox); pcl = (pc * NCC).tolist()
    x = rgb.astype(np.float64); D = np.sqrt(((x[:, None, :] - pal[None]) ** 2).sum(2))
    cand = np.argsort(D, 1)[:, :3]; Dc = (np.take_along_axis(D, cand, 1) / LAM).tolist(); candl = cand.tolist()
    N = len(vox); full = np.full(N + 1, NS, np.int64); log2 = math.log2; prev = st.prev
    for a, e in zip(bounds[:-1], bounds[1:]):
        m1 = (_vote(P[a:e], full) * (NS + 1)).tolist(); zl = zf[a:e].tolist(); res = []
        for i in range(e - a):
            c = m1[i] + (prev if zl[i] else NS) + pcl[a + i]; f = st.freq[c]
            cd = candl[a + i]; dd = Dc[a + i]; best = cd[0]; bj = dd[0] - log2(f[best])
            for k in (1, 2):
                s = cd[k]; j = dd[k] - log2(f[s])
                if j < bj: bj = j; best = s
            st.upd(c, best); res.append(best); ctxs.append(c); syms.append(best); prev = best
        full[a:e] = res
    st.prev = prev
    return full[:N]

def rc_encode(syms, ctxs, st):
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

class RcDec:
    def __init__(self, b, st): self.b = b; self.pos = 5; self.rng = 0xFFFFFFFF; self.code = int.from_bytes(b[:5], "big"); self.st = st
    def run(self, m1l, zfl):
        b = self.b; pos = self.pos; rng = self.rng; code = self.code; st = self.st; lb = len(b); prev = st.prev; res = []
        for m, z in zip(m1l, zfl):  # m already includes the parent-colour offset
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
    def band(self, vox, pc):
        P, zf, bounds = _geom_ctx(vox); N = len(vox); full = np.full(N + 1, NS, np.int64); pcv = pc * NCC
        for a, e in zip(bounds[:-1], bounds[1:]): full[a:e] = self.run((_vote(P[a:e], full) * (NS + 1) + pcv[a:e]).tolist(), zf[a:e].tolist())
        return full[:N]

def palette(rgb):
    from scipy.cluster.vq import kmeans2
    x = rgb.astype(np.float32); rng = np.random.default_rng(0)
    cent, _ = kmeans2(x[rng.choice(len(x), min(50000, len(x)), replace=False)], NS, minit="++", seed=0)
    return np.clip(np.round(cent), 0, 255).astype(np.uint8)

# ---------------- chunk coding (shared by encoder trials and the ablation) ----------------
def code_chunk(bands, st, pal):
    """bands: list of (bi, u int64 sorted, col uint8) coarse->fine. Mutates st (tables, colour state, known). Returns bytes."""
    enc = constriction.stream.queue.RangeEncoder(); hdrs = b""
    def coder(fam, p, y): enc.encode(y, fam, p); return y
    for bi, u, col in bands:
        ext = u.max(0) + 1; D = max(1, int(np.ceil(np.log2(ext.max()))))
        walk(ext, D, coder, st, lambda sh, e: kn_grid(bi, sh, e, st.known), u)
        hdrs += struct.pack("<B3HB", bi, *map(int, ext), D); st.known[bi] = np.concatenate([st.known[bi], u])
    g = enc.get_compressed().tobytes()
    pre = st.col.copy(); syms, ctxs = [], []
    for bi, u, col in bands:
        idx = greedy_band(u, col, pal.astype(np.float64), st.col, syms, ctxs, parent_ctx(bi, u, st)); st.cidx[bi] = np.concatenate([st.cidx[bi], idx])
    cb = rc_encode(syms, ctxs, pre); st.col = pre
    return struct.pack("<BI", len(bands), len(g)) + hdrs + g + cb

ZM = os.environ.get("ZM", "base"); PASTW = float(os.environ.get("PASTW", 1.25)); FARW = float(os.environ.get("FARW", 2.0))
FR = [float(x) for x in os.environ.get("FR", "0.5,0.75,1.0,1.15").split(",")]; MAXT = int(os.environ.get("MAXT", 7)); FILL = float(os.environ.get("FILL", 0.98)); TIE = float(os.environ.get("TIE", 0.01))
M21 = (1 << 21) - 1
from scipy.spatial.transform import Rotation, Slerp

def zview(P, R, c):
    W, H, FX = L.Q.W, L.Q.H, L.Q.FX
    q = (P - c) @ R; z = q[:, 2]; zz = np.maximum(z, 1e-6)
    u = FX * q[:, 0] / zz + W / 2; v = FX * q[:, 1] / zz + H / 2
    k = (z > 0.05) & (u >= 0) & (u < W) & (v >= 0) & (v < H)
    out = np.full(len(P), np.inf); out[k] = z[k]; return out

def zmin(P, poses):
    z = np.full(len(P), np.inf)
    for R, c in poses: z = np.minimum(z, zview(P, R, c))
    return z

def predicted_poses(traj, dt):
    p, R = traj[-1, 1:4], Rotation.from_quat(traj[-1, 4:8]).as_matrix()
    if len(traj) < 2: return [(R, p)]
    v = (traj[-1, 1:4] - traj[-2, 1:4]) / (traj[-1, 0] - traj[-2, 0])
    return [(R, p + v * dt * f) for f in FR]

def hist_poses(traj, n=4):
    if len(traj) < 2: return [(Rotation.from_quat(traj[0, 4:8]).as_matrix(), traj[0, 1:4])]
    ts = traj[:, 0]; tq = np.linspace(ts[0], ts[-1], (len(ts) - 1) * n + 1)
    Rs = Slerp(ts, Rotation.from_quat(traj[:, 4:8]))(tq).as_matrix()
    Ps = np.stack([np.interp(tq, ts, traj[:, k]) for k in (1, 2, 3)], 1); return list(zip(Rs, Ps))

class Encoder:
    def __init__(self, rate, P, C):
        self.rate, self.P, self.Cf = rate, P, C.astype(float); self.R = None; self.alpha = 0.02; self.vb = LEVELS / L.M
        self.st = State(len(LEVELS)); self.pal = palette(C); self.g = 2.0

    def _setup(self, traj):
        d = np.linalg.norm(self.P - traj[0, 1:4], axis=1); m0 = d < np.percentile(d, 50)
        self.R = L.ground_frame(self.P[m0], traj); self.Q3 = self.P @ self.R.T; self.o = self.Q3.min(0) - 50 / L.M
        self.bands = []
        for v in self.vb:  # fixed nested grids: voxel ids per point computed once (keys sorted = lexsort order of u)
            kk = keyf(np.floor((self.Q3 - self.o) / v).astype(np.int64)); uq, inv = np.unique(kk, return_inverse=True)
            u = np.stack([uq >> 42, (uq >> 21) & M21, uq & M21], 1)
            self.bands.append(dict(u=u, inv=inv.ravel(), n=len(uq), sent=np.zeros(len(uq), bool)))
        B = self.bands[0]; self.cen = (B["u"] + 0.5) * self.vb[0] + self.o  # 0.5 m voxel centres (ground frame)
        self.cenw = self.cen @ self.R  # world

    def _z(self, traj, dt):
        """effective viewing distance (m) per point, evaluated on 0.5 m voxel centres and mapped back to points."""
        Pc = self.cenw; inv = self.bands[0]["inv"]
        if ZM == "sched":
            pred = predicted_poses(traj, dt); zp = zmin(Pc, pred); zh = zmin(Pc, hist_poses(traj))
            ztrue = np.minimum(zp, zh); z = np.minimum(zp, zh * PASTW); unseen = ~np.isfinite(ztrue)
            dist = np.linalg.norm(Pc - pred[-1][1], axis=1); ztrue[unseen] = dist[unseen]; z[unseen] = dist[unseen] * FARW
            return (z * L.M)[inv], (ztrue * L.M)[inv]
        sub = traj if len(traj) > 1 else np.vstack([traj[0], traj[0] + [1e-3, 0, 0, 0, 0, 0, 0, 0]])
        z = LB.zeff(Pc, sub, "min"); far = ~np.isfinite(z); z[far] = np.linalg.norm(Pc[far] - traj[-1, 1:4], axis=1) * L.M
        return z[inv], z[inv]

    def select(self, alpha, z, ztrue):
        want = alpha * z; band = np.abs(np.log(want)[:, None] - np.log(LEVELS)[None]).argmin(1); out = []
        for bi in range(len(LEVELS) - 1, -1, -1):
            B = self.bands[bi]; m = band == bi
            if not m.any(): continue
            inv = B["inv"][m]; cnt = np.bincount(inv, minlength=B["n"]); ok = cnt >= MINCNT; keep = ok.copy()
            if CONF > 0 and ok.any():
                zs = np.bincount(inv, ztrue[m], minlength=B["n"]); dens = cnt * (zs / np.maximum(cnt, 1)) ** 2
                keep &= dens >= CONF * np.median(dens[ok])
            keep &= ~B["sent"]; idx = np.nonzero(keep)[0]
            if len(idx) == 0: continue
            col = np.stack([np.bincount(inv, self.Cf[m, ch], minlength=B["n"])[idx] for ch in range(3)], 1) / cnt[idx, None]
            out.append((bi, B["u"][idx], np.round(col).astype(np.uint8), idx))
        return out

    def trial(self, alpha, z, ztrue):
        st = self.st.copy()
        if not SHARE: st.reset_tables()
        bands = self.select(alpha, z, ztrue)
        return code_chunk([b[:3] for b in bands], st, self.pal), st, bands

    def update(self, k, traj, dt):
        budget = self.rate * dt; hdr = b""
        if self.R is None:
            self._setup(traj)
            hdr = (self.R.astype(np.float32).tobytes() + self.o.astype(np.float32).tobytes() + struct.pack("<fB", SPL, len(LEVELS))
                   + LEVELS.astype(np.float32).tobytes() + self.pal.tobytes())
        z, ztrue = self._z(traj, dt)
        if TIE > 0:  # break ties (flat ground at one depth) by distance to the drone so bytes(alpha) is near-continuous: nearest promoted first
            d = np.linalg.norm(self.P - traj[-1, 1:4], axis=1); z = z * (1 + TIE * d / d.max())
        bud = budget - len(hdr); a = self.alpha; best = None; lo = hi = None; tr = []
        for it in range(MAXT):
            b, st, bands = self.trial(a, z, ztrue); n = len(b); tr.append((a, n)); self.ntrials = it + 1
            if n <= bud:
                if best is None or n > len(best[0]): best = (b, st, bands, a)
                hi = a if hi is None else min(hi, a)
                if n > FILL * bud: break
            else: lo = a if lo is None else max(lo, a)
            if lo is not None and hi is not None and hi / lo < 1.001: break
            if len(tr) >= 2 and tr[-1][0] != tr[-2][0]:  # local power law bytes ~ alpha^-g
                n1, n0 = max(tr[-1][1], 1), max(tr[-2][1], 1)  # a trial may code 0 B (no band selected, e.g. stream_best_rec in a refinement slot)
                if n1 != n0: g = -math.log(n1 / n0) / math.log(tr[-1][0] / tr[-2][0]); self.g = min(max(g, 0.7), 30.0)
            a_new = a * (max(n, 1) / (0.99 * bud)) ** (1 / self.g)
            if lo is not None and hi is not None and not (lo < a_new < hi): a_new = math.sqrt(lo * hi)
            elif lo is not None and a_new <= lo: a_new = lo * 1.05
            elif hi is not None and a_new >= hi: a_new = hi / 1.05
            a = a_new
        while best is None:
            a *= 1.5; b, st, bands = self.trial(a, z, ztrue)
            if len(b) <= bud or a > 1.0: best = (b, st, bands, a)
        b, st, bands, a = best; self.alpha = a; self.st = st
        for bi, u, col, idx in bands: self.bands[bi]["sent"][idx] = True
        if os.environ.get("DBG"): print(f"   slot {k}: {self.ntrials} trials alpha {a:.4f} {len(b)}/{bud:.0f} B " + " ".join(f"{LEVELS[bi]}m:{len(u)}" for bi, u, _, _ in bands) + " tr " + str([(round(x, 4), y) for x, y in tr]))
        return hdr + b

class Decoder:
    def __init__(self): self.R = None
    def apply(self, b):
        p = 0
        if self.R is None:
            self.R = np.frombuffer(b[:36], np.float32).reshape(3, 3).astype(float); self.o = np.frombuffer(b[36:48], np.float32).astype(float)
            self.spl, nb = struct.unpack("<fB", b[48:53]); self.levels = np.frombuffer(b[53:53 + 4 * nb], np.float32).astype(float); p = 53 + 4 * nb
            self.pal = np.frombuffer(b[p:p + 3 * NS], np.uint8).reshape(NS, 3); p += 3 * NS
            self.st = State(nb)
        if not SHARE: self.st.reset_tables()
        nbd, lg = struct.unpack("<BI", b[p:p + 5]); p += 5; hd = []
        for _ in range(nbd): hd.append(struct.unpack("<B3HB", b[p:p + 8])); p += 8
        dec = constriction.stream.queue.RangeDecoder(np.frombuffer(b[p:p + lg], np.uint32).copy()); p += lg
        def coder(fam, pr, y): return dec.decode(fam, pr).astype(np.int32)
        got = []
        for bi, ex, ey, ez, D in hd:
            u = walk(np.array([ex, ey, ez], np.int64), D, coder, self.st, lambda sh, e, bi=bi: kn_grid(bi, sh, e, self.st.known))
            self.st.known[bi] = np.concatenate([self.st.known[bi], u]); got.append((bi, u))
        rd = RcDec(b[p:], self.st.col)
        for bi, u in got: self.st.cidx[bi] = np.concatenate([self.st.cidx[bi], rd.band(u, parent_ctx(bi, u, self.st))])
    def map(self):
        vb = self.levels / L.M; bands = [(vb[bi], self.st.known[bi], self.pal[self.st.cidx[bi]]) for bi in range(len(vb)) if len(self.st.known[bi])]
        out = []
        for v, u, c in bands:
            keep = np.ones(len(u), bool)
            for vf, uf, _ in bands:
                if vf < v: keep &= ~np.isin(keyf(u), keyf(uf // int(round(v / vf))))
            out.append((v, u[keep], c[keep]))
        vmin = min(v for v, _, _ in out); pts, cols = [], []
        for v, u, c in out:
            kk = int(np.ceil(v / vmin - 1e-3)); g = (np.arange(kk) + 0.5) / kk
            flat = np.stack(np.meshgrid(g, [0.5], g, indexing="ij"), -1).reshape(-1, 3)
            pts.append(((u[:, None, :] + flat[None]) * v + self.o).reshape(-1, 3)); cols.append(np.repeat(c, len(flat), 0))
        return np.concatenate(pts) @ self.R, np.concatenate(cols), vmin * self.spl

def make_encoder(rate, P, C): return Encoder(rate, P, C)
def make_decoder(): return Decoder()
