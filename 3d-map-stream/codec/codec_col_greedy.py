"""Colour study: like codec_col_ctx (baseline geometry, context-model range coder for palette
indices) but the encoder picks each voxel's index greedily in coding order among its 3 nearest
palette colours, minimising  ||rgb - pal[s]|| + LAM * (-log2 p_adaptive(s | ctx)).
Palette is refit to member means afterwards (free error reduction). Decoder = codec_col_ctx."""
import lzma, math, os, numpy as np
import importlib.util, pathlib
_spec = importlib.util.spec_from_file_location("_cc", str(pathlib.Path(__file__).with_name("codec_col_ctx.py")))
cc = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(cc)

LAM = float(os.environ.get("LAM", "3"))
NCAND = 3
NS, INC, LIMIT = cc.NS, cc.INC, cc.LIMIT

def encode_colour(vox, rgb):
    P, zf, bounds = cc._geom_ctx(vox)
    pal = cc.palette16(rgb)
    x = rgb.astype(np.float64)
    D = np.sqrt(((x[:, None, :] - pal[None]) ** 2).sum(2))
    cand = np.argsort(D, 1)[:, :NCAND]
    Dc = np.take_along_axis(D, cand, 1) / LAM if LAM > 0 else None
    N = len(vox); full = np.full(N + 1, 16, np.int64)
    freq = [[1] * NS for _ in range(17 * 17)]; tot = [NS] * (17 * 17)
    log2 = math.log2
    ctxs = []
    prev = 16
    candl = cand.tolist(); Dcl = Dc.tolist()
    for a, e in zip(bounds[:-1], bounds[1:]):
        m1 = (cc._vote(P[a:e], full) * 17).tolist()
        zl = zf[a:e].tolist()
        res = []
        for i in range(e - a):
            c = m1[i] + (prev if zl[i] else 16)
            f = freq[c]; t = tot[c]
            cd = candl[a + i]; dd = Dcl[a + i]
            best = cd[0]; bj = dd[0] - log2(f[best])
            for k in range(1, NCAND):
                s = cd[k]; j = dd[k] - log2(f[s])
                if j < bj: bj = j; best = s
            s = best
            f[s] += INC; t += INC
            if t > LIMIT:
                t = 0
                for j in range(NS):
                    f[j] = (f[j] + 1) >> 1; t += f[j]
            tot[c] = t
            res.append(s); ctxs.append(c); prev = s
        full[a:e] = res
    idx = full[:N]
    for k in range(NS):
        m = idx == k
        if m.any(): pal[k] = x[m].mean(0)
    palb = np.clip(np.round(pal), 0, 255).astype(np.uint8).tobytes()
    return palb + cc._rc_encode(idx.tolist(), ctxs)

def encode(vox, rgb):
    o = np.lexsort(vox.T[::-1]); vox, rgb = vox[o], rgb[o]
    dq = np.diff(vox, axis=0, prepend=0).astype(np.int16)
    g = lzma.compress(dq.T.tobytes(), preset=9 | lzma.PRESET_EXTREME)
    c = encode_colour(vox, rgb)
    return np.uint32(len(vox)).tobytes() + np.uint32(len(g)).tobytes() + g + c

decode = cc.decode
