"""Selection study on top of codec_lod_bands (same coder, same look).  Env knobs (all optional):
  BUDGET bytes (28900) | LEVELS | SPL | MINCNT | DMODE | ZMODE=min|hm|cov   (cov: z = 1/sqrt(sum 1/z^2) = pixel-coverage importance)
  EXTW   weight of the +5 s extension poses (1): their z is divided by EXTW (0 => they never refine; points seen only there dropped)
  FARCUT metres: drop points whose zeff > FARCUT (sky-line far field)
  ISO=1  drop voxels with no 26-neighbour inside their band (floaters)
  CONF   drop voxels whose point count < CONF * expected(z) where expected ~ median(cnt * z^2 / v^2) per band
  NB=K   ...unless the voxel has >= K occupied 26-neighbours in its band (only floaters / fringe go)
  COLDS=k COLB=b  bands >= b carry colour on a k-times coarser grid (geometry stays sharp; colour from parent cell)
  TW     time-visibility exponent: z *= (nmax / nvis)^TW  (points seen in few poses get coarser)
  DBG=1  print per-band geometry / colour bytes."""
import os, struct, numpy as np
import lod_common as L
E = os.environ
DENS = float(E.get("DENS", 1))

def zeff(P, traj):
    Rs, Ps = L.poses(traj, 48); ts = traj[:, 0]; tq = np.linspace(ts[0], ts[-1] + 5.0, 48); ext = tq > ts[-1]
    W, H, FX = L.Q.W, L.Q.H, L.Q.FX; extw = float(E.get("EXTW", 1))
    zmin = np.full(len(P), np.inf); acc = np.zeros(len(P)); n = np.zeros(len(P)); nw = np.zeros(len(P))
    for R, c, e in zip(Rs, Ps, ext):
        q = (P - c) @ R; z = q[:, 2]; zz = np.maximum(z, 1e-6)
        u = FX * q[:, 0] / zz + W / 2; v = FX * q[:, 1] / zz + H / 2
        k = (z > 0.05) & (u >= 0) & (u < W) & (v >= 0) & (v < H)
        w = extw if e else 1.0
        if w <= 0: continue
        zw = z[k] / w
        zmin[k] = np.minimum(zmin[k], zw); acc[k] += w / z[k] ** 2; n[k] += 1; nw[k] += w
    mode = E.get("ZMODE", "min")
    with np.errstate(divide="ignore", invalid="ignore"):
        if mode == "hm": z = np.sqrt(nw / acc)
        elif mode == "cov": z = 1 / np.sqrt(acc)
        else: z = zmin
    z = np.where(n == 0, np.inf, z)
    tw = float(E.get("TW", 0))
    if tw > 0: z = z * (n.max() / np.maximum(n, 1)) ** tw
    return z * L.M, n

def _neigh(u):
    """number of occupied 26-neighbours of each voxel (same band)."""
    key = lambda a: (a[:, 0] * 4096 + a[:, 1]) * 4096 + a[:, 2]
    ks = np.sort(key(u)); has = np.zeros(len(u), np.int32)
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for dz in (-1, 0, 1):
                if dx == dy == dz == 0: continue
                kk = key(u + [dx, dy, dz]); i = np.clip(np.searchsorted(ks, kk), 0, len(ks) - 1); has |= ks[i] == kk
    return has

def _band_bytes(u, col, colds):
    """geometry via cb.geo context coder; colour either full (cb) or on a colds x coarser grid."""
    if colds <= 1: return L.cb.encode(u.astype(np.int32), col), None
    import constriction
    o = np.lexsort(u.T[::-1]); u, col = u[o].astype(np.int64), col[o]
    ext = u.max(0) + 1; D = int(np.ceil(np.log2(ext.max())))
    enc = constriction.stream.queue.RangeEncoder()
    def coder(fam, p, y): enc.encode(y, fam, p); return y
    L.cb.geo._walk(ext, D, coder, u); g = enc.get_compressed().tobytes()
    par = u // colds; pu, inv = np.unique(par, axis=0, return_inverse=True); inv = inv.ravel()
    pc = np.zeros((len(pu), 3)); np.add.at(pc, inv, col.astype(float)); pc = np.round(pc / np.bincount(inv)[:, None]).astype(np.uint8)
    c = L.cb.col.encode_colour(pu.astype(np.int32), pc)
    return struct.pack("<I3HBI", len(u), *map(int, ext), D, len(g)) + g + c, (len(g), len(c))

def _band_decode(b, colds):
    if colds <= 1: return L.cb.decode(b)
    import constriction
    n, ex, ey, ez, D, lg = struct.unpack("<I3HBI", b[:15])
    dec = constriction.stream.queue.RangeDecoder(np.frombuffer(b[15:15 + lg], np.uint32).copy())
    def coder(fam, p, y): return dec.decode(fam, p).astype(np.int32)
    vox = L.cb.geo._walk(np.array([ex, ey, ez], np.int64), D, coder).astype(np.int32)
    vox = vox[np.lexsort(vox.T[::-1])]
    par = vox // colds; pu, inv = np.unique(par, axis=0, return_inverse=True); inv = inv.ravel()
    pc = L.cb.col.cc.decode_colour(pu.astype(np.int32), b[15 + lg:])
    return vox, np.asarray(pc)[inv]

def _enc(Q3, c, z, o, R, alpha, levels, spl, dbg=False):
    want = alpha * z
    band = np.abs(np.log(want)[:, None] - np.log(levels)[None]).argmin(1)
    colds, colb = int(E.get("COLDS", 1)), int(E.get("COLB", 99))
    out = R.astype(np.float32).tobytes() + np.array([*o], np.float32).tobytes() + struct.pack("<fBB", spl, len(levels), colds)
    mc = int(E.get("MINCNT", 2)); conf = float(E.get("CONF", 0)); iso = E.get("ISO") == "1"
    for b, vm in enumerate(levels):
        m = band == b; v = vm / L.M
        if m.sum() == 0: out += struct.pack("<fIB", v, 0, 0); continue
        u, col, cnt, inv = L.voxelize(Q3[m], c[m], v, o)
        keep = cnt >= mc
        if conf > 0:
            zs = np.zeros(len(u)); np.add.at(zs, inv, z[m]); zs /= cnt
            dens = cnt * zs ** 2; med = np.median(dens[cnt >= mc])
            low = dens < conf * med
            nb = int(E.get("NB", 0))  # rescue low-confidence voxels that have >= NB occupied neighbours
            if nb > 0: low &= _neigh(u) < nb
            keep &= ~low
        if iso: keep &= _neigh(u) > 0
        u, col = u[keep], col[keep]
        if len(u) == 0: out += struct.pack("<fIB", v, 0, 0); continue
        cd = colds if b >= colb else 1
        s, split = _band_bytes(u, col, cd)
        if dbg: print(f"  band {vm:4.2f}m: {len(u):6d} vox {len(s):6d} B" + (f"  geo {split[0]} col {split[1]}" if split else ""))
        out += struct.pack("<fIB", v, len(s), cd) + s
    return out

def encode(P, rgb, traj):
    spl = float(E.get("SPL", 0.8))
    levels = np.array([float(x) for x in E.get("LEVELS", "0.5,0.7,1.0,1.4,2.0").split(",")])
    z, nvis = zeff(P, traj); keep = np.isfinite(z)
    if "FARCUT" in E: keep &= z <= float(E["FARCUT"])
    R = L.ground_frame(P, traj); Q3 = (P @ R.T)[keep]; c = rgb[keep]; o = Q3.min(0); z = z[keep]
    if "ALPHA" in E: return _enc(Q3, c, z, o, R, float(E["ALPHA"]), levels, spl, E.get("DBG") == "1")
    budget = float(E.get("BUDGET", 28900)); lo, hi = np.log(0.004), np.log(0.25); best = None; ba = None
    for _ in range(int(E.get("ITERS", 8))):
        mid = (lo + hi) / 2; b = _enc(Q3, c, z, o, R, np.exp(mid), levels, spl)
        if len(b) <= budget: best, hi, ba = b, mid, mid
        else: lo = mid
    if best is None: best = _enc(Q3, c, z, o, R, np.exp(hi), levels, spl); ba = hi
    if E.get("DBG") == "1": print(f"  alpha {np.exp(ba):.4f}"); _enc(Q3, c, z, o, R, np.exp(ba), levels, spl, True)
    return best

def decode(b):
    R = np.frombuffer(b[:36], np.float32).reshape(3, 3).astype(float); o = np.frombuffer(b[36:48], np.float32).astype(float)
    spl, nb, colds = struct.unpack("<fBB", b[48:54]); p = 54; vs, chunks = [], []
    for _ in range(nb):
        v, n, cd = struct.unpack("<fIB", b[p:p + 9]); p += 9
        if n: chunks.append((v, _band_decode(b[p:p + n], cd))); vs.append(v)
        p += n
    vmin = min(vs); pts, cols = [], []
    for v, (vox, rgb) in chunks:
        k = int(np.ceil(v / vmin * DENS - 1e-3)); g = (np.arange(k) + 0.5) / k
        vox = np.asarray(vox).astype(np.int64); rgb = np.asarray(rgb)
        flat = np.stack(np.meshgrid(g, [0.5], g, indexing="ij"), -1).reshape(-1, 3)
        q = (vox[:, None, :] + flat[None]) * v + o
        pts.append(q.reshape(-1, 3)); cols.append(np.repeat(rgb, len(flat), 0))
    return np.concatenate(pts) @ R, np.concatenate(cols), float(vmin) * spl
