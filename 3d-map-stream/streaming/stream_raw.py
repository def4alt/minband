"""RAW live streamer: no voxelisation, no compression - the sanity baseline for "is the live SLAM map itself OK?".
Same API as the other streamers (make_encoder / enc.update / make_decoder / dec.apply / dec.map).
Chunk = one record <B 0><I n> + payload; payload = <I N> + float32 xyz[N,3] + uint8 rgb[N,3] of the points not sent yet
(the sender's append-only ingestion only ever appends points). Use with --rate 1e8 (unlimited) and --slot 1."""
import struct, numpy as np, lod_common as L
LEVELS = np.array([0.25]); SPL = 1.0   # the sender adapter reads these; irrelevant here
HDR_LEN = 53 + 4 * len(LEVELS)         # the adapter prepends its frame header to the first chunk of a 'conf'-kind streamer

class Encoder:
    def __init__(self, rate, P, C): self.rate, self.P, self.C, self.n_sent = rate, P, C, 0; self.R = None
    def update(self, k, traj, dt):
        n = len(self.P)
        if n <= self.n_sent: return b""
        pts = np.ascontiguousarray(self.P[self.n_sent:n], np.float32); rgb = np.ascontiguousarray(self.C[self.n_sent:n], np.uint8); self.n_sent = n
        payload = struct.pack("<I", len(pts)) + pts.tobytes() + rgb.tobytes()
        return struct.pack("<BI", 0, len(payload)) + payload

class Decoder:
    def __init__(self): self.pts, self.rgb, self.first = [], [], True
    def apply(self, b):
        p = 0
        if self.first:  # the first record always carries the adapter's stream header (strict: no sniffing)
            if len(b) < HDR_LEN or b[52] != len(LEVELS): raise ValueError("first raw record does not start with the stream header")
            p = HDR_LEN; self.first = False
        new = []
        while p + 5 <= len(b):
            band, n = struct.unpack("<BI", b[p:p + 5]); p += 5
            if p + n > len(b) or n < 4: raise ValueError(f"truncated raw record ({n} B announced, {len(b) - p} left)")
            rec = b[p:p + n]; p += n; N = struct.unpack("<I", rec[:4])[0]
            if n != 4 + 15 * N: raise ValueError(f"malformed raw record: {n} B for {N} points")
            new.append((np.frombuffer(rec[4:4 + 12 * N], np.float32).reshape(N, 3).astype(float), np.frombuffer(rec[4 + 12 * N:4 + 15 * N], np.uint8).reshape(N, 3).copy()))
        for q, c in new: self.pts.append(q); self.rgb.append(c)  # both arrays appended together, only after the whole record parsed
    def map(self):
        if not self.pts: return np.zeros((0, 3)), np.zeros((0, 3), np.uint8), 0.25 / L.M
        return np.concatenate(self.pts), np.concatenate(self.rgb), 0.25 / L.M

def make_encoder(rate, P, C): return Encoder(rate, P, C)
def make_decoder(): return Decoder()

def split_records(chunk, first):
    """One record per chunk (the adapter's frame header rides on the first one); the sender uses this verbatim."""
    return [chunk] if chunk else []
