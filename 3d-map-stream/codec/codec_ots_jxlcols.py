"""OTS 2.5D codec: y-columns -> images. Geometry: occupancy mask + top-height + per-column count as
JPEG-XL lossless images (holes nearest-filled), below-top gaps as brotli stream. Colour: 16-colour
entropy-constrained palette (lambda=2), labels nibble-packed in lexsort order, raw LZMA2 lc=4 pb=0 lp=0.
Needs imagecodecs + brotli (venv_ots)."""
import sys, glob, struct, lzma, numpy as np
for p in glob.glob('/tmp/claude-1000/-home-matteo-Documents-3d/ec416e8c-37fe-40e2-9d59-d38cd41f70c1/scratchpad/venv_ots/lib/python3*/site-packages'):
    if p not in sys.path: sys.path.append(p)
import imagecodecs as ic, brotli
from scipy import ndimage

LZF = [{'id': lzma.FILTER_LZMA2, 'preset': 9 | lzma.PRESET_EXTREME, 'lc': 4, 'lp': 0, 'pb': 0}]
def xz(b): return lzma.compress(b, format=lzma.FORMAT_RAW, filters=LZF)
def unxz(b): return lzma.decompress(b, format=lzma.FORMAT_RAW, filters=LZF)
def jxl(a, e=9): return ic.jpegxl_encode(a, lossless=True, effort=e)
def fill(img, m):
    ii = ndimage.distance_transform_edt(~m, return_distances=False, return_indices=True)
    return img[ii[0], ii[1]]

def palette_ec(rgb, lam=2.0):
    from scipy.cluster.vq import kmeans2
    x = rgb.astype(np.float64); rng = np.random.default_rng(0)
    c, _ = kmeans2(x[rng.choice(len(x), min(50000, len(x)), replace=False)].astype(np.float32), 16, minit="++", seed=0)
    c = c.astype(np.float64); p = np.full(16, 1 / 16)
    sub = x[rng.choice(len(x), min(80000, len(x)), replace=False)]
    for _ in range(12):
        D = np.sqrt(((sub[:, None] - c[None]) ** 2).sum(2))
        l = (D - lam * np.log2(np.maximum(p, 1e-9))[None]).argmin(1)
        p = np.bincount(l, minlength=16) / len(l)
        for j in range(16):
            if p[j] > 0: c[j] = sub[l == j].mean(0)
    cq = np.round(c).clip(0, 255)
    D = np.sqrt(((x[:, None] - cq[None]) ** 2).sum(2))
    l = (D - lam * np.log2(np.maximum(p, 1e-9))[None]).argmin(1)
    return cq.astype(np.uint8), l.astype(np.uint8)

def encode(vox, rgb):
    X, Y, Z = vox[:, 0], vox[:, 1], vox[:, 2]
    Hh, Yn, W = int(X.max()) + 1, int(Y.max()) + 1, int(Z.max()) + 1
    s = np.lexsort((-Y, Z, X)); Xs, Ys, Zs = X[s], Y[s], Z[s]
    k = Xs.astype(np.int64) * W + Zs
    newc = np.r_[True, k[1:] != k[:-1]]; st = np.flatnonzero(newc); cnt = np.diff(np.r_[st, len(k)])
    m = np.zeros((Hh, W), bool); m[Xs[st], Zs[st]] = True
    top = np.zeros((Hh, W), np.uint8); top[Xs[st], Zs[st]] = Ys[st]
    C = np.zeros((Hh, W), np.uint8); C[Xs[st], Zs[st]] = np.minimum(cnt, 255)
    assert cnt.max() < 255 and Yn <= 256
    g = (Ys[:-1] - Ys[1:])[~newc[1:]] - 1
    parts = [jxl((m * 255).astype(np.uint8)), jxl(fill(top, m), 10), jxl(fill(C, m), 10),
             brotli.compress(g.astype(np.uint8).tobytes(), quality=11, lgwin=24)]
    # colour in lexsort (x,y,z) order
    o = np.lexsort((Z, Y, X)); pal, lab = palette_ec(rgb[o])
    if len(lab) % 2: lab = np.append(lab, 0)
    parts.append(xz((lab[0::2] << 4 | lab[1::2]).astype(np.uint8).tobytes()))
    hdr = struct.pack('<IHHH', len(vox), Hh, W, 0) + pal.tobytes() + struct.pack('<4I', *[len(p) for p in parts[:4]])
    encode.sizes = [len(p) for p in parts]
    return hdr + b''.join(parts)

def decode(b):
    n, Hh, W, _ = struct.unpack('<IHHH', b[:10]); pal = np.frombuffer(b[10:58], np.uint8).reshape(16, 3)
    L = struct.unpack('<4I', b[58:74]); pos = 74; parts = []
    for l in L: parts.append(b[pos:pos + l]); pos += l
    parts.append(b[pos:])
    m = ic.jpegxl_decode(parts[0]) > 127; top = ic.jpegxl_decode(parts[1]); C = ic.jpegxl_decode(parts[2])
    g = np.frombuffer(brotli.decompress(parts[3]), np.uint8).astype(np.int32) + 1
    cx, cz = np.nonzero(m)  # raster order == lexsort(Z,X)
    cnt = C[cx, cz].astype(np.int64); y0 = top[cx, cz].astype(np.int32)
    N = int(cnt.sum()); assert N == n
    colid = np.repeat(np.arange(len(cnt)), cnt)
    first = np.zeros(N, bool); first[np.r_[0, np.cumsum(cnt)[:-1]]] = True
    d = np.zeros(N, np.int32); d[~first] = g
    # y = y0 - cumulative gaps within column
    cs = np.cumsum(d); base = cs[np.r_[0, np.cumsum(cnt)[:-1]]]
    y = y0[colid] - (cs - base[colid])
    vox = np.stack([cx[colid], y, cz[colid]], 1).astype(np.int32)
    vox = vox[np.lexsort((vox[:, 2], vox[:, 1], vox[:, 0]))]
    nib = np.frombuffer(unxz(parts[4]), np.uint8)
    lab = np.stack([nib >> 4, nib & 15], 1).ravel()[:n]
    return vox, pal[lab]
