"""Colour study: baseline geometry (delta int16 + lzma) + 16-colour palette whose indices are coded
with an adaptive context-model range coder.
Order = lexsort (x, y, z).  Context for voxel i = (m1, az):
  m1 = weighted vote winner among the 9 neighbours in plane x-1 (vectorised per x-slice),
  az = index of voxel (x, y, z-1) if present (= previous voxel), else 16.
Optional lossy RDO (LAM>0): index = argmin ||rgb - pal[s]|| + LAM * bits(s | ctx), iterated."""
import lzma, numpy as np

import os
LAM = float(os.environ.get("LAM", "0"))      # rate-distortion lambda (RGB-distance units per bit); 0 = plain nearest colour
ITERS = 4
INC, LIMIT = 24, 1 << 13
NS = 16

def palette16(rgb):
    from scipy.cluster.vq import kmeans2
    x = rgb.astype(np.float32)
    rng = np.random.default_rng(0)
    cent, lab = kmeans2(x[rng.choice(len(x), min(50000, len(x)), replace=False)], 16, minit="++", seed=0)
    return cent.astype(np.float64)

def _key(a):
    return (a[:, 0].astype(np.int64) << 42) | (a[:, 1].astype(np.int64) << 21) | a[:, 2].astype(np.int64)

PLANE = [(-1, a, b) for a in (-1, 0, 1) for b in (-1, 0, 1)]
W = np.array([3.0 / (1 + abs(a) + abs(b)) for _, a, b in PLANE])

def _geom_ctx(vox):
    """neighbour positions (N,9) in plane x-1 (-1 = absent), z-1 flag, slice boundaries."""
    N = len(vox); K = _key(vox)
    P = np.full((N, 9), -1, np.int64)
    for j, dx in enumerate(PLANE):
        k = _key(vox + np.array(dx, np.int32)); p = np.minimum(np.searchsorted(K, k), N - 1)
        P[:, j] = np.where(K[p] == k, p, -1)
    zf = np.zeros(N, bool)
    zf[1:] = (vox[1:, 0] == vox[:-1, 0]) & (vox[1:, 1] == vox[:-1, 1]) & (vox[1:, 2] == vox[:-1, 2] + 1)
    xs = vox[:, 0]
    bounds = np.r_[0, np.nonzero(np.diff(xs))[0] + 1, N]
    return P, zf, bounds

def _vote(P, idxfull):
    """m1 per voxel from plane neighbours, idxfull has an extra sentinel at the end (position -1)."""
    n = len(P)
    nb = idxfull[P]                      # (n,9) values 0..16  (P=-1 -> sentinel 16)
    cnt = np.zeros((n, 17))
    np.add.at(cnt, (np.repeat(np.arange(n), 9), nb.ravel()), np.tile(W, n))
    cnt[:, 16] = 1e-3
    return cnt.argmax(1)

def _ctx_all(P, zf, idx):
    full = np.r_[idx, 16]
    m1 = _vote(P, full)
    az = np.where(zf, np.r_[16, idx[:-1]], 16)
    return m1 * 17 + az

def _rdo(rgb, pal, P, zf):
    x = rgb.astype(np.float64)
    D = np.sqrt(((x[:, None, :] - pal[None]) ** 2).sum(2))
    idx = D.argmin(1)
    if LAM <= 0:
        return pal, idx
    N = len(idx)
    for it in range(ITERS):
        ctx = _ctx_all(P, zf, idx)
        cnt = np.zeros((17 * 17, NS)) + 0.5
        np.add.at(cnt, (ctx, idx), 1)
        bits = -np.log2(cnt / cnt.sum(1, keepdims=True))
        idx = (D + LAM * bits[ctx]).argmin(1)
        # refit palette to mean of members
        for k in range(NS):
            m = idx == k
            if m.any(): pal[k] = x[m].mean(0)
        D = np.sqrt(((x[:, None, :] - pal[None]) ** 2).sum(2))
    return pal, idx

# ---------------- range coder ----------------
def _rc_encode(syms, ctxs):
    out = bytearray(); low = 0; rng = 0xFFFFFFFF; cache = 0; csize = 1
    freq = [[1] * NS for _ in range(17 * 17)]; tot = [NS] * (17 * 17)
    for s, c in zip(syms, ctxs):
        f = freq[c]; t = tot[c]
        r = rng // t
        low += r * sum(f[:s]); rng = r * f[s]
        while rng < 0x1000000:
            rng <<= 8
            if low < 0xFF000000 or low > 0xFFFFFFFF:
                carry = low >> 32; temp = cache
                while True:
                    out.append((temp + carry) & 0xFF); temp = 0xFF; csize -= 1
                    if csize == 0: break
                cache = (low >> 24) & 0xFF
            csize += 1
            low = (low & 0x00FFFFFF) << 8
        f[s] += INC; t += INC
        if t > LIMIT:
            t = 0
            for j in range(NS):
                f[j] = (f[j] + 1) >> 1; t += f[j]
        tot[c] = t
    for _ in range(5):
        if low < 0xFF000000 or low > 0xFFFFFFFF:
            carry = low >> 32; temp = cache
            while True:
                out.append((temp + carry) & 0xFF); temp = 0xFF; csize -= 1
                if csize == 0: break
            cache = (low >> 24) & 0xFF
        csize += 1
        low = (low & 0x00FFFFFF) << 8
    return bytes(out)

class _Dec:
    def __init__(self, b):
        self.b = b; self.pos = 5; self.rng = 0xFFFFFFFF
        self.code = int.from_bytes(b[:5], "big")
        self.freq = [[1] * NS for _ in range(17 * 17)]; self.tot = [NS] * (17 * 17)

def _rc_decode_run(d, ctxs, zfl, prev):
    """decode len(ctxs) symbols; ctxs are m1*17 (az added inside since it depends on prev symbol)."""
    b = d.b; pos = d.pos; rng = d.rng; code = d.code; freq = d.freq; tot = d.tot; lb = len(b)
    res = []
    for m, z in zip(ctxs, zfl):
        c = m + (prev if z else 16)
        f = freq[c]; t = tot[c]
        r = rng // t
        v = code // r
        if v >= t: v = t - 1
        cum = 0; s = 0
        while cum + f[s] <= v:
            cum += f[s]; s += 1
        code -= r * cum; rng = r * f[s]
        while rng < 0x1000000:
            rng <<= 8; code = ((code << 8) | (b[pos] if pos < lb else 0)) & 0xFFFFFFFFFF; pos += 1
        f[s] += INC; t += INC
        if t > LIMIT:
            t = 0
            for j in range(NS):
                f[j] = (f[j] + 1) >> 1; t += f[j]
        tot[c] = t
        res.append(s); prev = s
    d.pos = pos; d.rng = rng; d.code = code
    return res, prev

# ---------------- codec ----------------
def encode_colour(vox, rgb):
    P, zf, bounds = _geom_ctx(vox)
    pal, idx = _rdo(rgb, palette16(rgb), P, zf)
    ctx = _ctx_all(P, zf, idx)
    palb = np.clip(np.round(pal), 0, 255).astype(np.uint8).tobytes()
    return palb + _rc_encode(idx.tolist(), ctx.tolist())

def decode_colour(vox, b):
    pal = np.frombuffer(b[:48], np.uint8).reshape(16, 3)
    P, zf, bounds = _geom_ctx(vox)
    N = len(vox); full = np.full(N + 1, 16, np.int64)
    d = _Dec(b[48:]); prev = 16
    for a, e in zip(bounds[:-1], bounds[1:]):
        m1 = _vote(P[a:e], full) * 17
        res, prev = _rc_decode_run(d, m1.tolist(), zf[a:e].tolist(), prev)
        full[a:e] = res
    return pal[full[:N]]

def encode(vox, rgb):
    o = np.lexsort(vox.T[::-1]); vox, rgb = vox[o], rgb[o]
    dq = np.diff(vox, axis=0, prepend=0).astype(np.int16)
    g = lzma.compress(dq.T.tobytes(), preset=9 | lzma.PRESET_EXTREME)
    c = encode_colour(vox, rgb)
    return np.uint32(len(vox)).tobytes() + np.uint32(len(g)).tobytes() + g + c

def decode(b):
    n = int(np.frombuffer(b[:4], np.uint32)[0]); gl = int(np.frombuffer(b[4:8], np.uint32)[0])
    raw = lzma.decompress(b[8:8 + gl])
    vox = np.cumsum(np.frombuffer(raw, np.int16).reshape(3, n).T.astype(np.int32), axis=0)
    return vox, decode_colour(vox, b[8 + gl:])
