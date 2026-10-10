"""Baseline LIVE streamer: LOD voxel bands + CONF density pruning (from live_sim_sel.py), per-slot budget,
warm-started ALPHA search, nested grids 0.5/1/2/4 m with a world frame fixed from keyframe 0 (sent once).
Refinement = re-sending the area at the finer band (receiver drops coarse voxels under finer ones)."""
import os, sys, struct, numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lod_common as L, codec_lod_bands as LB

LEVELS = np.array([0.5, 1.0, 2.0, 4.0]); SPL = 1.0; MINCNT = 2; CONF = float(os.environ.get("CONF", 0.5))
keyf = lambda u: (u[:, 0].astype(np.int64) << 42) | (u[:, 1].astype(np.int64) << 21) | u[:, 2].astype(np.int64)

class Encoder:
    def __init__(self, rate, P, C):
        self.rate, self.P, self.C = rate, P, C
        self.R = self.o = None; self.sent = [set() for _ in LEVELS]; self.alpha = 0.02; self.vb = LEVELS / L.M

    def _chunk(self, alpha, z):
        want = alpha * z; band = np.abs(np.log(want)[:, None] - np.log(LEVELS)[None]).argmin(1)
        out, newk = b"", []
        for bi, v in enumerate(self.vb):
            m = band == bi
            if not m.any(): newk.append([]); continue
            u, col, cnt, inv = L.voxelize(self.Q3[m], self.C[m], v, self.o); keep = cnt >= MINCNT
            if CONF > 0:  # density relative to expectation at this (known-path) distance
                zs = np.zeros(len(u)); np.add.at(zs, inv, z[m]); zs /= cnt; dens = cnt * zs ** 2
                keep &= dens >= CONF * np.median(dens[cnt >= MINCNT])
            u, col = u[keep], col[keep]
            kk = keyf(u); fresh = np.array([k not in self.sent[bi] for k in kk], bool)
            u, col, kk = u[fresh], col[fresh], kk[fresh]
            if len(u) == 0: newk.append([]); continue
            s = L.cb.encode(u.astype(np.int32), col); out += struct.pack("<BI", bi, len(s)) + s; newk.append(kk.tolist())
        return out, newk

    def update(self, k, traj, dt):
        budget = self.rate * dt; hdr = b""
        if self.R is None:  # frame from keyframe-0 view, sent once
            d = np.linalg.norm(self.P - traj[0, 1:4], axis=1); m0 = d < np.percentile(d, 50)
            self.R = L.ground_frame(self.P[m0], traj); self.Q3 = self.P @ self.R.T; self.o = self.Q3.min(0) - 50 / L.M
            hdr = self.R.astype(np.float32).tobytes() + self.o.astype(np.float32).tobytes() + struct.pack("<fB", SPL, len(LEVELS)) + LEVELS.astype(np.float32).tobytes()
        sub = traj if len(traj) > 1 else np.vstack([traj[0], traj[0] + [1e-3, 0, 0, 0, 0, 0, 0, 0]])
        z = LB.zeff(self.P, sub, "min"); far = ~np.isfinite(z); z[far] = np.linalg.norm(self.P[far] - traj[-1, 1:4], axis=1) * L.M
        lo, hi = np.log(self.alpha / 2), np.log(self.alpha * 2); best = None
        for it in range(6):  # warm-started bisection on log(alpha); widen upward if even the coarse end is over budget
            mid = (lo + hi) / 2; b, nk = self._chunk(np.exp(mid), z)
            if len(b) + len(hdr) <= budget: best, hi = (b, nk, mid), mid
            else: lo = mid
        while best is None:
            hi += np.log(1.5); b, nk = self._chunk(np.exp(hi), z)
            if len(b) + len(hdr) <= budget or hi > np.log(1.0): best = (b, nk, hi)
        b, nk, a = best; self.alpha = float(np.exp(a))
        for bi, kk in enumerate(nk): self.sent[bi].update(kk)
        return hdr + b

class Decoder:
    def __init__(self): self.R = None; self.vox = None
    def apply(self, b):
        p = 0
        if self.R is None:
            self.R = np.frombuffer(b[:36], np.float32).reshape(3, 3).astype(float); self.o = np.frombuffer(b[36:48], np.float32).astype(float)
            self.spl, nb = struct.unpack("<fB", b[48:53]); self.levels = np.frombuffer(b[53:53 + 4 * nb], np.float32).astype(float); p = 53 + 4 * nb
            self.vox = [dict() for _ in range(nb)]
        while p < len(b):
            bi, n = struct.unpack("<BI", b[p:p + 5]); p += 5; u, c = L.cb.decode(b[p:p + n]); p += n
            u = np.asarray(u).astype(np.int64); c = np.asarray(c)
            for key, uu, cc in zip(keyf(u).tolist(), u, c): self.vox[bi][key] = (uu, cc)
    def map(self):
        vb = self.levels / L.M; bands = [(bi, vb[bi], np.array([x[0] for x in d.values()]), np.array([x[1] for x in d.values()])) for bi, d in enumerate(self.vox) if d]
        out = []
        for bi, v, u, c in bands:  # finest band wins inside its footprint
            keep = np.ones(len(u), bool)
            for bj, vf, uf, cf in bands:
                if vf < v:
                    r = int(round(v / vf)); cov = set(keyf(uf // r).tolist()); keep &= np.array([k not in cov for k in keyf(u).tolist()], bool)
            out.append((v, u[keep], c[keep]))
        vmin = min(v for v, _, _ in out); pts, cols = [], []
        for v, u, c in out:
            kk = int(np.ceil(v / vmin - 1e-3)); g = (np.arange(kk) + 0.5) / kk
            flat = np.stack(np.meshgrid(g, [0.5], g, indexing="ij"), -1).reshape(-1, 3)
            pts.append(((u[:, None, :] + flat[None]) * v + self.o).reshape(-1, 3)); cols.append(np.repeat(c, len(flat), 0))
        return np.concatenate(pts) @ self.R, np.concatenate(cols), vmin * self.spl

def make_encoder(rate, P, C): return Encoder(rate, P, C)
def make_decoder(): return Decoder()
