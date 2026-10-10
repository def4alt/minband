"""stream_best_rec: stream_best (verified best LIVE streamer) re-packaged as PER-BAND RECORDS so the receiver can
render after every record instead of waiting for the whole ~6 s chunk.

Wire format (byte-identical header to stream_best):
  first chunk only, STREAM HEADER = R 9*f32 (36 B) + o 3*f32 (12 B) + SPL f32 (4 B) + nb u8 (1 B) + nb*f32 levels
                    + 16*3 u8 palette (48 B)   ->  length = 53 + 4*b[52] + 48   (= 117 B for the 4 standard bands)
  then, for every band that has new voxels, COARSE BAND FIRST, one RECORD:
      <B band><I n> + payload(n bytes)
      payload = <3H extent><B depth><I lg> + geometry range-coder stream (lg B, constriction, u32 aligned)
                                            + colour range-coder stream (rest)
  Both streams are coded with the SAME adaptive context tables / colour model / palette as stream_best, which are
  persistent across records AND chunks on both sides (encoder and decoder update them record by record in the same
  order).  A fresh Decoder fed the records one at a time in transmission order (header prepended to the very first
  record) reconstructs exactly the same map as a Decoder fed whole chunks, and Decoder.map() is valid after any record.
  Cost vs stream_best: one range-coder flush per stream per record (~4 B geometry + ~5 B colour) + 11 B record fields
  instead of 8 B band fields  ->  ~10-20 B per band record.
Decoder.apply accepts a whole chunk or any single record (or any concatenation of records in order).
split_records(chunk_bytes, first) splits a chunk into its records (header riding with the first one).
Env: as stream_best (ZM, MAXT, FILL, TIE, CONF, LAM, CTX_KNOWN, CTX_PCOL); CTX_SHARE=0 is NOT supported per record."""
import os, sys, struct, numpy as np, constriction
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stream_best as SB
from stream_best import (walk, kn_grid, greedy_band, rc_encode, RcDec, parent_ctx, State, LEVELS, NS, SPL, keyf)
import lod_common as L

HDR_FIXED = 53  # R + o + SPL + nb

def header_len(b):
    """length of the stream header at the start of the first chunk / first record."""
    return HDR_FIXED + 4 * b[52] + 3 * NS

def split_records(chunk_bytes, first):
    """-> list of records in transmission order (coarse band first, as emitted); the stream header is prepended to the
    first record when `first` (chunk 0 of the stream)."""
    p = 0; hdr = b""
    if first: p = header_len(chunk_bytes); hdr = chunk_bytes[:p]
    out = []
    while p < len(chunk_bytes):
        n = int.from_bytes(chunk_bytes[p + 1:p + 5], "little"); out.append(chunk_bytes[p:p + 5 + n]); p += 5 + n
    if hdr: out[0] = hdr + out[0]
    return out

def code_records(bands, st, pal):
    """bands: list of (bi, u int64 lexsorted, col uint8) coarse->fine. Mutates st exactly like stream_best.code_chunk
    does (tables, colour model, known, cidx) but in record order (geometry_b, colour_b, geometry_b+1, ...)."""
    out = b""; pal64 = pal.astype(np.float64)
    for bi, u, col in bands:
        enc = constriction.stream.queue.RangeEncoder()
        def coder(fam, p, y): enc.encode(y, fam, p); return y
        ext = u.max(0) + 1; D = max(1, int(np.ceil(np.log2(ext.max()))))
        walk(ext, D, coder, st, lambda sh, e: kn_grid(bi, sh, e, st.known), u)
        st.known[bi] = np.concatenate([st.known[bi], u]); g = enc.get_compressed().tobytes()
        pre = st.col.copy(); syms, ctxs = [], []
        idx = greedy_band(u, col, pal64, st.col, syms, ctxs, parent_ctx(bi, u, st)); st.cidx[bi] = np.concatenate([st.cidx[bi], idx])
        cb = rc_encode(syms, ctxs, pre); st.col = pre
        payload = struct.pack("<3HBI", *map(int, ext), D, len(g)) + g + cb
        out += struct.pack("<BI", bi, len(payload)) + payload
    return out

class Encoder(SB.Encoder):
    """identical planning / budget fit / header to stream_best; only the chunk coding differs."""
    def trial(self, alpha, z, ztrue):
        st = self.st.copy(); bands = self.select(alpha, z, ztrue)  # select() yields coarse -> fine
        return code_records([b[:3] for b in bands], st, self.pal), st, bands

class Decoder:
    def __init__(self): self.R = None
    def apply(self, b):
        p = 0
        if self.R is None:  # stream header (same layout as stream_best)
            self.R = np.frombuffer(b[:36], np.float32).reshape(3, 3).astype(float); self.o = np.frombuffer(b[36:48], np.float32).astype(float)
            self.spl, nb = struct.unpack("<fB", b[48:53]); self.levels = np.frombuffer(b[53:53 + 4 * nb], np.float32).astype(float); p = 53 + 4 * nb
            self.pal = np.frombuffer(b[p:p + 3 * NS], np.uint8).reshape(NS, 3); p += 3 * NS
            self.st = State(nb)
        while p < len(b):
            bi, n = struct.unpack("<BI", b[p:p + 5]); p += 5; self._record(bi, b[p:p + n]); p += n
    def _record(self, bi, pl):
        ex, ey, ez, D, lg = struct.unpack("<3HBI", pl[:11]); g = pl[11:11 + lg]; cb = pl[11 + lg:] + b"\0" * 5
        dec = constriction.stream.queue.RangeDecoder(np.frombuffer(g, np.uint32).copy())
        def coder(fam, pr, y): return dec.decode(fam, pr).astype(np.int32)
        u = walk(np.array([ex, ey, ez], np.int64), D, coder, self.st, lambda sh, e: kn_grid(bi, sh, e, self.st.known))
        self.st.known[bi] = np.concatenate([self.st.known[bi], u])
        rd = RcDec(cb, self.st.col)
        self.st.cidx[bi] = np.concatenate([self.st.cidx[bi], rd.band(u, parent_ctx(bi, u, self.st))])
    map = SB.Decoder.map

def make_encoder(rate, P, C): return Encoder(rate, P, C)
def make_decoder(): return Decoder()
