"""live_adapter.py: feed a GROWING SLAM cloud to a whole-cloud LIVE streamer (stream_best.py or stream_lod_conf.py).

The streamers are built for one fixed cloud (make_encoder(rate, P, C) once, then enc.update per keyframe).  Live, the
cloud grows with every keyframe, so before each update we replace enc.P / enc.C (enc.Cf) and enc.Q3 = P @ enc.R.T while
keeping the world frame enc.R, the grid origin enc.o (fixed from the first snapshot with a margin so later points stay
inside the grid), the "already sent" state and the budget-fit warm start enc.alpha.  Also splits a chunk into wire
records (stream_lod_conf chunks = self-contained band records; stream_best chunks = ONE record, they share one range coder).
"""
import importlib.util, struct, numpy as np

def load_streamer(path):
    s = importlib.util.spec_from_file_location("streamer", path); m = importlib.util.module_from_spec(s); __import__("sys").modules[s.name] = m; s.loader.exec_module(m); return m

def frame_from_kf0(L, P, traj):
    """same recipe as both streamers: ground plane fitted on the half of the cloud nearest to keyframe 0."""
    d = np.linalg.norm(P - traj[0, 1:4], axis=1); m0 = d < np.percentile(d, 50)
    return L.ground_frame(P[m0], traj)

class LiveEncoder:
    def __init__(self, mod, rate, P0, C0, margin_units):
        self.mod, self.L = mod, mod.L; self.margin = float(margin_units)
        self.enc = mod.make_encoder(rate, np.asarray(P0, float), np.asarray(C0, np.uint8))
        self.kind = "best" if hasattr(self.enc, "_setup") else "conf"   # stream_best (+sched-like) vs stream_lod_conf
        self.first = True; self.dropped = 0; self.npts = len(P0)
        if self.kind == "best":   # hook: after the streamer picks its frame, widen the origin margin and rebuild the voxel ids
            orig = self.enc._setup
            def setup(traj):
                orig(traj); self.enc.o = self.enc.Q3.min(0) - self.margin; self._apply(self.enc.P, self.enc.Cf.astype(np.uint8))
            self.enc._setup = setup

    # ---- cloud replacement -------------------------------------------------------------------------
    def _inside(self, Q3):
        enc = self.enc; vmin = float(np.min(enc.vb)); u = (Q3 - enc.o) / vmin
        return (u >= 0).all(1) & (u < 65000).all(1)   # ids must be >= 0 (keyf bit packing) and fit the uint16 extents

    def _apply(self, P, C):
        enc, mod = self.enc, self.mod
        Q3 = P @ enc.R.T; ok = self._inside(Q3); self.dropped = int((~ok).sum())
        if self.dropped: P, C, Q3 = P[ok], C[ok], Q3[ok]
        self.npts = len(P)
        if self.kind == "conf":
            enc.P, enc.C, enc.Q3 = P, C, Q3; return     # sent sets are keyed by voxel id -> independent of P
        enc.P, enc.Cf, enc.Q3 = P, C.astype(float), Q3; bands = []
        if hasattr(enc, "CfT"): enc.CfT = np.ascontiguousarray(enc.Cf.T)   # stream_rt caches the transposed colours for its kernels
        for bi, v in enumerate(enc.vb):
            kk = mod.keyf(np.floor((Q3 - enc.o) / v).astype(np.int64)); uq, inv = np.unique(kk, return_inverse=True)
            u = np.stack([uq >> 42, (uq >> 21) & mod.M21, uq & mod.M21], 1)
            kn = enc.st.known[bi]; sent = np.isin(uq, mod.keyf(kn)) if len(kn) else np.zeros(len(uq), bool)
            bands.append(dict(u=u, inv=inv.ravel(), n=len(uq), sent=sent))
        enc.bands = bands; B = bands[0]; enc.cen = (B["u"] + 0.5) * enc.vb[0] + enc.o; enc.cenw = enc.cen @ enc.R

    def set_cloud(self, P, C):
        P = np.asarray(P, float); C = np.asarray(C, np.uint8)
        if self.first: self.enc.P = P; self.enc.C = C
        if self.first and self.kind == "best":
            self.enc.Cf = C.astype(float)
            if hasattr(self.enc, "CfT"): self.enc.CfT = np.ascontiguousarray(self.enc.Cf.T)
        if not self.first: self._apply(P, C)

    # ---- one transmission slot ----------------------------------------------------------------------
    def update(self, k, traj, dt):
        enc, mod = self.enc, self.mod; hdr = b""
        if self.first and self.kind == "conf":   # stream_lod_conf has no setup hook: fix the frame here and emit its header ourselves
            enc.R = frame_from_kf0(self.L, enc.P, traj); enc.Q3 = enc.P @ enc.R.T; enc.o = enc.Q3.min(0) - self.margin
            self._apply(enc.P, enc.C)
            hdr = (enc.R.astype(np.float32).tobytes() + enc.o.astype(np.float32).tobytes()
                   + struct.pack("<fB", mod.SPL, len(mod.LEVELS)) + mod.LEVELS.astype(np.float32).tobytes())
        chunk = hdr + enc.update(k, np.asarray(traj, float).copy(), float(dt)); self.first = False
        return chunk

    def split_records(self, chunk, first):
        """wire payloads for one chunk, coarse band first; the first record of the stream carries the header."""
        if self.kind == "best" or not chunk: return [chunk] if chunk else []
        p = 0; hdr = b""
        if first: nb = chunk[52]; p = 53 + 4 * nb; hdr = chunk[:p]
        recs = []
        while p + 5 <= len(chunk):
            bi = chunk[p]; n = int.from_bytes(chunk[p + 1:p + 5], "little"); recs.append((bi, chunk[p:p + 5 + n])); p += 5 + n
        out = [r for _, r in sorted(recs, key=lambda x: -x[0])]
        if not out: return [hdr] if hdr else []
        out[0] = hdr + out[0]; return out

def pose_payload(row, dt):
    return b"POSE" + np.asarray(row, "<f8").tobytes() + struct.pack("<d", float(dt))
