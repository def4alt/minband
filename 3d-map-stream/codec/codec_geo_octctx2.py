"""Geometry: octree, level-by-level, octant-by-octant binary occupancy coding with
context-adaptive range coding (constriction). Context = octant index + states (empty/occupied/unknown)
of 6 face neighbours at child level + capped counts of occupied/unknown edge neighbours (coarse ctx),
blended (PPM-like back-off) with a hashed fine ctx that adds 6 distance-2 axis neighbours + 8 corners.
Colour: identical to baseline (16-colour k-means palette, nibbles, lzma)."""
import lzma, struct, numpy as np, constriction

CHUNK = 512
ALPHA = 6.0
H = 1 << 22
NB = [(1,0,0),(-1,0,0),(0,1,0),(0,-1,0),(0,0,1),(0,0,-1)]
ED = [(a,b,c) for a in (-1,0,1) for b in (-1,0,1) for c in (-1,0,1) if abs(a)+abs(b)+abs(c)==2]
CAP = 3
NCTX = 8*729*(CAP+1)**2
FINE = [(2,0,0),(-2,0,0),(0,2,0),(0,-2,0),(0,0,2),(0,0,-2)] + [(a,b,c) for a in (-1,1) for b in (-1,1) for c in (-1,1)]

def palette16(rgb):
    from scipy.cluster.vq import kmeans2
    x = rgb.astype(np.float32)
    rng = np.random.default_rng(0)
    cent, lab = kmeans2(x[rng.choice(len(x), min(50000, len(x)), replace=False)], 16, minit="++", seed=0)
    d = ((x[:, None, :] - cent[None]) ** 2).sum(2)
    return np.clip(np.round(cent), 0, 255).astype(np.uint8), d.argmin(1).astype(np.uint8)

def _ctx(K, cc, k):
    g = lambda o: K[cc[:,0]+o[0], cc[:,1]+o[1], cc[:,2]+o[2]].astype(np.int64)
    s = np.zeros(len(cc), np.int64)
    for o in NB: s = s*3 + g(o)
    n1 = np.zeros(len(cc), np.int64); n2 = np.zeros(len(cc), np.int64)
    for o in ED:
        v = g(o); n1 += v == 1; n2 += v == 2
    co = ((k*729 + s)*(CAP+1) + np.minimum(n1, CAP))*(CAP+1) + np.minimum(n2, CAP)
    f = co.copy()
    for o in FINE: f = f*3 + g(o)
    return co, ((f.astype(np.uint64)*np.uint64(0x9E3779B97F4A7C15)) >> np.uint64(42)).astype(np.int64)

def _walk(ext, D, coder, vox=None):
    """coder(ctx_probs, y or None) -> y. Returns final-level occupancy grid (padded by 2)."""
    fam = constriction.stream.model.Bernoulli(perfect=False)
    tb = np.zeros((NCTX, 2), np.float64); tf = np.zeros((H, 2), np.float64)
    P = np.zeros((1, 3), np.int64)
    for L in range(1, D+1):
        sh = D - L; e = ((ext-1) >> sh) + 1
        K = np.zeros(tuple(e+4), np.int8)
        if vox is not None:
            occ = np.zeros(tuple(e+4), bool); c = vox >> sh; occ[c[:,0]+2, c[:,1]+2, c[:,2]+2] = True
        cands = []
        for k in range(8):
            b = np.array([(k>>2)&1, (k>>1)&1, k&1]); cc = 2*P + b
            cc = cc[(cc < e).all(1)] + 2; cands.append(cc); K[cc[:,0], cc[:,1], cc[:,2]] = 2
        for k in range(8):
            cc = cands[k]; ctx, fctx = _ctx(K, cc, k)
            y = occ[cc[:,0], cc[:,1], cc[:,2]].astype(np.int32) if vox is not None else np.empty(len(cc), np.int32)
            for st in range(0, len(cc), CHUNK):
                cx = ctx[st:st+CHUNK]; fx = fctx[st:st+CHUNK]
                pc = (tb[cx,1] + 0.4) / (tb[cx,0] + tb[cx,1] + 0.8)
                p1 = (tf[fx,1] + ALPHA*pc) / (tf[fx,0] + tf[fx,1] + ALPHA)
                yy = coder(fam, p1, y[st:st+CHUNK] if vox is not None else None)
                y[st:st+CHUNK] = yy
                np.add.at(tb, (cx, yy), 1); np.add.at(tf, (fx, yy), 1)
            K[cc[:,0], cc[:,1], cc[:,2]] = y
        P = np.argwhere(K == 1) - 2
        P = P[np.lexsort(P.T[::-1])]
    return P

def encode(vox, rgb):
    o = np.lexsort(vox.T[::-1]); vox, rgb = vox[o].astype(np.int64), rgb[o]
    ext = vox.max(0) + 1; D = int(np.ceil(np.log2(ext.max())))
    enc = constriction.stream.queue.RangeEncoder()
    def coder(fam, p, y): enc.encode(y, fam, p); return y
    _walk(ext, D, coder, vox)
    geo = enc.get_compressed().tobytes()
    pal, idx = palette16(rgb)
    if len(idx) % 2: idx = np.append(idx, 0)
    nib = (idx[0::2] << 4 | idx[1::2]).astype(np.uint8)
    col = lzma.compress(nib.tobytes(), preset=9 | lzma.PRESET_EXTREME)
    return struct.pack("<I3HB I", len(vox), *map(int, ext), D, len(geo)) + pal.tobytes() + geo + col

def decode(b):
    n, ex, ey, ez, D, lg = struct.unpack("<I3HB I", b[:15]); ext = np.array([ex, ey, ez], np.int64)
    pal = np.frombuffer(b[15:63], np.uint8).reshape(16, 3)
    dec = constriction.stream.queue.RangeDecoder(np.frombuffer(b[63:63+lg], np.uint32).copy())
    def coder(fam, p, y): return dec.decode(fam, p).astype(np.int32)
    vox = _walk(ext, D, coder).astype(np.int32)
    nib = np.frombuffer(lzma.decompress(b[63+lg:]), np.uint8)
    idx = np.stack([nib >> 4, nib & 15], 1).ravel()[:n]
    return vox, pal[idx]
