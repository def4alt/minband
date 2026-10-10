"""Best-of study: octree context-coded geometry (codec_geo_octctx2) + greedy context-coded
4-bit colour (codec_col_greedy / codec_col_ctx). Needs `constriction` (pip)."""
import struct, importlib.util, pathlib, numpy as np, constriction

def _load(name):
    s = importlib.util.spec_from_file_location(name, str(pathlib.Path(__file__).with_name(name + ".py")))
    m = importlib.util.module_from_spec(s); s.loader.exec_module(m); return m

geo, col = _load("codec_geo_octctx2"), _load("codec_col_greedy")

def encode(vox, rgb):
    o = np.lexsort(vox.T[::-1]); vox, rgb = vox[o].astype(np.int64), rgb[o]
    ext = vox.max(0) + 1; D = int(np.ceil(np.log2(ext.max())))
    enc = constriction.stream.queue.RangeEncoder()
    def coder(fam, p, y): enc.encode(y, fam, p); return y
    geo._walk(ext, D, coder, vox)
    g = enc.get_compressed().tobytes()
    c = col.encode_colour(vox.astype(np.int32), rgb)
    return struct.pack("<I3HBI", len(vox), *map(int, ext), D, len(g)) + g + c

def decode(b):
    n, ex, ey, ez, D, lg = struct.unpack("<I3HBI", b[:15])
    dec = constriction.stream.queue.RangeDecoder(np.frombuffer(b[15:15 + lg], np.uint32).copy())
    def coder(fam, p, y): return dec.decode(fam, p).astype(np.int32)
    vox = geo._walk(np.array([ex, ey, ez], np.int64), D, coder).astype(np.int32)
    vox = vox[np.lexsort(vox.T[::-1])]
    return vox, col.cc.decode_colour(vox, b[15 + lg:])
