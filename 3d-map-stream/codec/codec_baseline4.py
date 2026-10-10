"""Baseline: delta-coded sorted int16 coords + 16-colour k-means palette (4-bit, nibble-packed) + lzma."""
import lzma, numpy as np

def palette16(rgb):
    from scipy.cluster.vq import kmeans2
    x = rgb.astype(np.float32)
    rng = np.random.default_rng(0)
    cent, lab = kmeans2(x[rng.choice(len(x), min(50000, len(x)), replace=False)], 16, minit="++", seed=0)
    d = ((x[:, None, :] - cent[None]) ** 2).sum(2)
    return np.clip(np.round(cent), 0, 255).astype(np.uint8), d.argmin(1).astype(np.uint8)

def encode(vox, rgb):
    o = np.lexsort(vox.T[::-1]); vox, rgb = vox[o], rgb[o]
    pal, idx = palette16(rgb)
    if len(idx) % 2: idx = np.append(idx, 0)
    nib = (idx[0::2] << 4 | idx[1::2]).astype(np.uint8)
    dq = np.diff(vox, axis=0, prepend=0).astype(np.int16)
    return np.uint32(len(vox)).tobytes() + pal.tobytes() + lzma.compress(dq.T.tobytes() + nib.tobytes(), preset=9 | lzma.PRESET_EXTREME)

def decode(b):
    n = int(np.frombuffer(b[:4], np.uint32)[0]); pal = np.frombuffer(b[4:52], np.uint8).reshape(16, 3)
    raw = lzma.decompress(b[52:])
    vox = np.cumsum(np.frombuffer(raw[:6 * n], np.int16).reshape(3, n).T.astype(np.int32), axis=0)
    nib = np.frombuffer(raw[6 * n:], np.uint8)
    idx = np.stack([nib >> 4, nib & 15], 1).ravel()[:n]
    return vox, pal[idx]
