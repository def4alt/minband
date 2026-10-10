"""stream_geo: LOD voxel band streamer (same selection / look as stream_lod_conf: bands 0.5/1/2/4 m, CONF
pruning, refinement near the drone) with CHEAPER BYTES PER VOXEL:
  * one octree range-coder stream per chunk for all bands, with context tables that PERSIST across bands
    and across slots (decoder state is a deterministic function of the received bytes) -> no warm-up cost
  * one colour context stream per chunk, context statistics persist across bands and slots
  * ONE global 16-colour palette, fit once from the sender's map and sent in slot 0 (48 B total instead of
    48 B per band per slot)
  * optional block colours per band (BLK, e.g. "1,1,1;2,1,2;1,1,1;1,1,1" = 2x1x2 voxels share one colour in the
    1 m band, nearest-neighbour decode). Measured: LIVE +0.14 dB but FINAL -0.3 dB / SSIM -0.04 -> OFF by default
  * alpha search driven by a byte PREDICTION (bits/voxel learnt from the previous slot) -> 1-3 real encodes
Env: BLK="bx,by,bz;..." per band (default all 1), CONF (0.5), LAM (3), NS (16)."""
import os, sys, time, struct, copy, math, numpy as np, constriction
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lod_common as L, codec_lod_bands as LB
import stream_geo_col as GC
geo = L.cb.geo
CH = L._load("lod2cheap", L._here / "codec_lod2_cheap.py")

LEVELS = np.array([0.5, 1.0, 2.0, 4.0]); SPL = 1.0; MINCNT = 2
CONF = float(os.environ.get("CONF", 0.5)); LAM = float(os.environ.get("LAM", 3)); NS = int(os.environ.get("NS", 16))
BLK = np.array([[int(x) for x in s.split(",")] for s in os.environ.get("BLK", "1,1,1;1,1,1;1,1,1;1,1,1").split(";")], np.int64)
keyf = lambda u: (u[:, 0].astype(np.int64) << 42) | (u[:, 1].astype(np.int64) << 21) | u[:, 2].astype(np.int64)

def voxelize(Q3, rgb, v, o):
    key = np.floor((Q3 - o) / v).astype(np.int64); kk = keyf(key)
    uk, first, inv, cnt = np.unique(kk, return_index=True, return_inverse=True, return_counts=True); inv = inv.ravel()
    col = np.zeros((len(uk), 3)); np.add.at(col, inv, rgb.astype(float))
    return key[first], np.round(col / cnt[:, None]).astype(np.uint8), cnt, inv

def tables(): return np.zeros((geo.NCTX, 2)), np.zeros((geo.H, 2))

def blocks(u, blk):
    bu = u // blk; kk = keyf(bu); uk, first, inv = np.unique(kk, return_index=True, return_inverse=True)
    return bu[first], inv.ravel()

class Encoder:
    def __init__(self, rate, P, C):
        self.rate, self.P, self.C = rate, P, C
        self.R = None; self.sent = [set() for _ in LEVELS]; self.alpha = 0.02; self.vb = LEVELS / L.M
        self.tb, self.tf = tables(); self.cst = GC.ColState(NS)
        self.bpv = np.full(len(LEVELS), 2.2); self.bpc = np.full(len(LEVELS), 3.0)  # bits per voxel (geo) / per colour unit
        self.pal = None

    def _select(self, alpha, z):
        want = alpha * z; band = np.abs(np.log(want)[:, None] - np.log(LEVELS)[None]).argmin(1); sel = []
        for bi, v in enumerate(self.vb):
            m = band == bi
            if not m.any(): sel.append(None); continue
            u, col, cnt, inv = voxelize(self.Q3[m], self.C[m], v, self.o); keep = cnt >= MINCNT
            if CONF > 0:
                zs = np.zeros(len(u)); np.add.at(zs, inv, z[m]); zs /= cnt; dens = cnt * zs ** 2
                keep &= dens >= CONF * np.median(dens[cnt >= MINCNT])
            u, col, cnt = u[keep], col[keep], cnt[keep]
            kk = keyf(u); fresh = np.array([k not in self.sent[bi] for k in kk.tolist()], bool)
            u, col, cnt, kk = u[fresh], col[fresh], cnt[fresh], kk[fresh]
            if len(u) == 0: sel.append(None); continue
            o_ = np.lexsort(u.T[::-1]); u, col, cnt, kk = u[o_], col[o_], cnt[o_], kk[o_]
            bu, binv = blocks(u, BLK[bi]) if (BLK[bi] > 1).any() else (u, np.arange(len(u)))
            bc = np.zeros((len(bu), 3)); np.add.at(bc, binv, col.astype(float) * cnt[:, None]); bc /= np.bincount(binv, cnt)[:, None]
            sel.append((u, kk, bu, bc))
        return sel

    def _predict(self, sel):
        return sum(10 + len(s[0]) * self.bpv[bi] / 8 + len(s[2]) * self.bpc[bi] / 8 for bi, s in enumerate(sel) if s is not None)

    def _encode(self, sel):
        tb, tf = self.tb.copy(), self.tf.copy(); enc = constriction.stream.queue.RangeEncoder()
        def coder(fam, p, y): enc.encode(y, fam, p); return y
        hdr = b""; gsz = []
        for bi, s in enumerate(sel):
            if s is None: continue
            u = s[0]; lo = u.min(0); ext = u.max(0) - lo + 1; D = max(1, int(np.ceil(np.log2(ext.max()))))
            n0 = len(enc.get_compressed()) * 4
            CH._walk(ext, D, coder, tb, tf, (u - lo).astype(np.int64)); gsz.append(len(enc.get_compressed()) * 4 - n0)
            hdr += struct.pack("<BI3H3HB", bi, len(u), *map(int, lo), *map(int, ext), D)
        g = enc.get_compressed().tobytes()
        st = copy.deepcopy(self.cst); syms, ctxs = [], []; csz = []
        for bi, s in enumerate(sel):
            if s is None: continue
            n0 = len(syms); CH.greedy_band(s[2].astype(np.int64), s[3], self.pal.astype(np.float64), st, LAM, syms, ctxs); csz.append(len(syms) - n0)
        cb = GC.rc_encode(syms, ctxs, copy.deepcopy(self.cst))
        out = struct.pack("<BI", sum(s is not None for s in sel), len(g)) + hdr + g + cb
        return out, (tb, tf, st, gsz, csz, len(cb))

    def _learn(self, sel, info, w=0.5):
        tb, tf, st, gsz, csz, ncb = info; j = 0; tot = max(sum(csz), 1)
        for bi, s in enumerate(sel):
            if s is None: continue
            if len(s[0]) > 200: self.bpv[bi] = (1 - w) * self.bpv[bi] + w * 8 * gsz[j] / len(s[0])
            if len(s[2]) > 200: self.bpc[bi] = (1 - w) * self.bpc[bi] + w * 8 * ncb * csz[j] / tot / len(s[2])
            j += 1

    def update(self, k, traj, dt):
        budget = self.rate * dt; hdr = b""
        if self.R is None:
            d = np.linalg.norm(self.P - traj[0, 1:4], axis=1); m0 = d < np.percentile(d, 50)
            self.R = L.ground_frame(self.P[m0], traj); self.Q3 = self.P @ self.R.T; self.o = self.Q3.min(0) - 50 / L.M
            self.pal = np.clip(np.round(CH.palette(self.C, NS)), 0, 255).astype(np.uint8)
            hdr = (self.R.astype(np.float32).tobytes() + self.o.astype(np.float32).tobytes() + struct.pack("<fBB", SPL, len(LEVELS), NS)
                   + LEVELS.astype(np.float32).tobytes() + BLK.astype(np.uint8).tobytes() + self.pal.tobytes())
        sub = traj if len(traj) > 1 else np.vstack([traj[0], traj[0] + [1e-3, 0, 0, 0, 0, 0, 0, 0]])
        z = LB.zeff(self.P, sub, "min"); far = ~np.isfinite(z); z[far] = np.linalg.norm(self.P[far] - traj[-1, 1:4], axis=1) * L.M
        target = (budget - len(hdr)) * 0.985; best = None; cache = {}; lo0, hi0 = np.log(self.alpha / 2.5), np.log(self.alpha * 2.5)
        bpv0, bpc0 = self.bpv.copy(), self.bpc.copy(); t_sel = t_enc = 0.0
        for rnd in range(3):  # bisection on PREDICTED bytes (cheap), then one real encode; re-learn b/v and repeat
            lo, hi = lo0, hi0
            for it in range(7):
                mid = (lo + hi) / 2
                if mid not in cache: t = time.time(); cache[mid] = self._select(np.exp(mid), z); t_sel += time.time() - t
                if self._predict(cache[mid]) <= target: hi = mid
                else: lo = mid
            a = hi; sel = cache[a]
            if all(s is None for s in sel): out, info = b"", None; break
            t = time.time(); out, info = self._encode(sel); t_enc += time.time() - t
            self._learn(sel, info, 1.0)
            if len(out) <= target and (best is None or len(out) > len(best[0])): best = (out, info, sel, a)
            if len(out) <= target and len(out) >= 0.93 * target: break
            if len(out) > target: lo0 = a            # only coarser from here
            else: hi0 = a                            # only finer from here
        while best is None:  # last resort: coarsen until it fits
            a += np.log(1.4); sel = self._select(np.exp(a), z); out, info = self._encode(sel)
            if len(out) <= target or a > np.log(1.0): best = (out, info, sel, a)
        self.bpv, self.bpc = bpv0, bpc0
        if os.environ.get("DBG"): print(f"   k{k}: select {t_sel:.1f}s encode {t_enc:.1f}s rounds {rnd+1}")
        out, info, sel, a = best; self.alpha = float(np.exp(a))
        if info is not None:
            self._learn(sel, info, 0.5); self.tb, self.tf, self.cst = info[0], info[1], info[2]
            for bi, s in enumerate(sel):
                if s is not None: self.sent[bi].update(s[1].tolist())
        return hdr + out

class Decoder:
    def __init__(self): self.R = None
    def apply(self, b):
        p = 0
        if self.R is None:
            self.R = np.frombuffer(b[:36], np.float32).reshape(3, 3).astype(float); self.o = np.frombuffer(b[36:48], np.float32).astype(float)
            self.spl, nb, ns = struct.unpack("<fBB", b[48:54]); p = 54
            self.levels = np.frombuffer(b[p:p + 4 * nb], np.float32).astype(float); p += 4 * nb
            self.blk = np.frombuffer(b[p:p + 3 * nb], np.uint8).reshape(nb, 3).astype(np.int64); p += 3 * nb
            self.pal = np.frombuffer(b[p:p + 3 * ns], np.uint8).reshape(ns, 3).copy(); p += 3 * ns
            self.vox = [dict() for _ in range(nb)]; self.tb, self.tf = tables(); self.cst = GC.ColState(ns)
        if p >= len(b): return
        nbands, lg = struct.unpack("<BI", b[p:p + 5]); p += 5; hd = []
        for _ in range(nbands): hd.append(struct.unpack("<BI3H3HB", b[p:p + 18])); p += 18
        dec = constriction.stream.queue.RangeDecoder(np.frombuffer(b[p:p + lg], np.uint32).copy()); p += lg
        def coder(fam, pr, y): return dec.decode(fam, pr).astype(np.int32)
        geos = []
        for (bi, n, ox, oy, oz, ex, ey, ez, D) in hd:
            u = CH._walk(np.array([ex, ey, ez], np.int64), D, coder, self.tb, self.tf) + np.array([ox, oy, oz], np.int64)
            geos.append((bi, u[np.lexsort(u.T[::-1])]))
        rd = GC.RcDec(b[p:], self.cst)
        for bi, u in geos:
            if (self.blk[bi] > 1).any(): bu, binv = blocks(u, self.blk[bi]); idx = rd.band(bu.astype(np.int64))[binv]
            else: idx = rd.band(u.astype(np.int64))
            c = self.pal[idx]
            for key, uu, cc in zip(keyf(u).tolist(), u, c): self.vox[bi][key] = (uu, cc)
    def map(self):
        vb = self.levels / L.M; bands = [(bi, vb[bi], np.array([x[0] for x in d.values()]), np.array([x[1] for x in d.values()])) for bi, d in enumerate(self.vox) if d]
        out = []
        for bi, v, u, c in bands:  # finest band wins inside its footprint
            keep = np.ones(len(u), bool)
            for bj, vf, uf, cf in bands:
                if vf < v:
                    r = int(round(v / vf)); cov = set(keyf(uf // r).tolist()); keep &= np.array([k not in cov for k in keyf(u).tolist()], bool)
            out.append((v, u[keep], c[keep]))
        vmin = min(v for v, _, _ in out); pts, cols = [], []
        for v, u, c in out:
            kk = int(np.ceil(v / vmin - 1e-3)); g = (np.arange(kk) + 0.5) / kk
            flat = np.stack(np.meshgrid(g, [0.5], g, indexing="ij"), -1).reshape(-1, 3)
            pts.append(((u[:, None, :] + flat[None]) * v + self.o).reshape(-1, 3)); cols.append(np.repeat(c, len(flat), 0))
        return np.concatenate(pts) @ self.R, np.concatenate(cols), vmin * self.spl

def make_encoder(rate, P, C): return Encoder(rate, P, C)
def make_decoder(): return Decoder()
