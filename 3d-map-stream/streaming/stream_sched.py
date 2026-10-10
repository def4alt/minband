"""stream_sched: WHAT TO SEND WHEN.  Same LOD voxel look / codec as stream_lod_conf (nested 0.5/1/2/4 m bands,
octree+palette codec from compress_study, CONF density pruning) but a different scheduler and rate control:
  * causal view prediction: at slot k the operator is scored from the pose at t_k+dt; we extrapolate position with
    the last keyframe velocity (rotation held) and give the predicted frustum full priority (zpred).
  * the already-flown path (keyframes 0..k, slerped) only gets PASTW x coarser treatment (keeps FINAL, saves bytes);
    points outside every frustum get FARW x coarser and are postponed (not sent) if they would want > DROPF x 4 m.
  * exact, fast budget fit: bytes ~ alpha^-s, secant in log-log, warm-started (2-3 encodes instead of 6-7).
  * records are ordered coarse band -> fine band inside a chunk; each record carries a grid offset (smaller octree).
Env: VSPEED 0 (m/s forward-cruise prior for slot 0 only; 0 = pose 0 only) | CONF 0.5 | PASTW 1.25 | FARW 2 | DROPF inf (2.5 = postpone far unseen points) | DMODE 2d|adapt (adapt: cube-fill stacked coarse voxels, fewer holes but lower PSNR) | FILL 0.985 | FR 0.5,0.75,1.0,1.15 (prediction fractions of dt)."""
import os, sys, struct, numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lod_common as L
from scipy.spatial.transform import Rotation, Slerp

LEVELS = np.array([0.5, 1.0, 2.0, 4.0]); SPL = 1.0; MINCNT = 2
E = os.environ.get
CONF = float(E("CONF", 0.5)); PASTW = float(E("PASTW", 1.25)); FARW = float(E("FARW", 2.0)); DROPF = float(E("DROPF", 1e9)); DMODE = E("DMODE", "2d")
VSPEED = float(E("VSPEED", 0)); FILL = float(E("FILL", 0.985)); FR = [float(x) for x in E("FR", "0.5,0.75,1.0,1.15").split(",")]
W, H, FX = L.Q.W, L.Q.H, L.Q.FX
M21 = (1 << 21) - 1
keyf = lambda u: (u[:, 0].astype(np.int64) << 42) | (u[:, 1].astype(np.int64) << 21) | u[:, 2].astype(np.int64)

def zview(P, R, c):
    q = (P - c) @ R; z = q[:, 2]; zz = np.maximum(z, 1e-6)
    u = FX * q[:, 0] / zz + W / 2; v = FX * q[:, 1] / zz + H / 2
    k = (z > 0.05) & (u >= 0) & (u < W) & (v >= 0) & (v < H)
    out = np.full(len(P), np.inf); out[k] = z[k]; return out

GUP = np.zeros(3)
def predicted_poses(traj, dt):
    p, R = traj[-1, 1:4], Rotation.from_quat(traj[-1, 4:8]).as_matrix()
    if len(traj) < 2:  # no velocity yet: optional prior = cruise along the camera's horizontal forward axis (VSPEED m/s)
        if VSPEED <= 0: return [(R, p)]
        f = R[:, 2].copy(); f -= (f @ GUP) * GUP; v = f / np.linalg.norm(f) * VSPEED / L.M
    else: v = (traj[-1, 1:4] - traj[-2, 1:4]) / (traj[-1, 0] - traj[-2, 0])
    return [(R, p + v * dt * f) for f in FR]

def hist_poses(traj, n=4):
    if len(traj) < 2: return [(Rotation.from_quat(traj[0, 4:8]).as_matrix(), traj[0, 1:4])]
    ts = traj[:, 0]; tq = np.linspace(ts[0], ts[-1], (len(ts) - 1) * n + 1)
    Rs = Slerp(ts, Rotation.from_quat(traj[:, 4:8]))(tq).as_matrix()
    Ps = np.stack([np.interp(tq, ts, traj[:, k]) for k in (1, 2, 3)], 1); return list(zip(Rs, Ps))

def zmin(P, poses):
    z = np.full(len(P), np.inf)
    for R, c in poses: z = np.minimum(z, zview(P, R, c))
    return z

class Encoder:
    def __init__(self, rate, P, C):
        self.rate, self.P, self.Cf = rate, P, C.astype(float); self.R = None; self.alpha = 0.02; self.slope = 1.8

    def _setup(self, traj):
        d = np.linalg.norm(self.P - traj[0, 1:4], axis=1); m0 = d < np.percentile(d, 50)
        self.R = L.ground_frame(self.P[m0], traj); self.Q3 = self.P @ self.R.T; self.o = self.Q3.min(0) - 50 / L.M
        self.vb = LEVELS / L.M; self.bands = []
        for v in self.vb:  # fixed nested grids: voxel ids per point computed once
            kk = keyf(np.floor((self.Q3 - self.o) / v).astype(np.int64)); uq, inv = np.unique(kk, return_inverse=True)
            u = np.stack([uq >> 42, (uq >> 21) & M21, uq & M21], 1)
            self.bands.append(dict(u=u, inv=inv.ravel(), n=len(uq), sent=np.zeros(len(uq), bool)))

    def _chunk(self, alpha, z, ztrue):
        want = alpha * z; band = np.abs(np.log(want)[:, None] - np.log(LEVELS)[None]).argmin(1)
        band[want > LEVELS[-1] * DROPF] = -1
        out, marks = b"", []
        for bi in range(len(LEVELS) - 1, -1, -1):  # coarse first: a partially received chunk is already useful
            B = self.bands[bi]; m = band == bi
            if not m.any(): continue
            inv = B["inv"][m]; cnt = np.bincount(inv, minlength=B["n"]); ok = cnt >= MINCNT; keep = ok.copy()
            if CONF > 0 and ok.any():
                zs = np.bincount(inv, ztrue[m], minlength=B["n"]); dens = cnt * (zs / np.maximum(cnt, 1)) ** 2
                keep &= dens >= CONF * np.median(dens[ok])
            keep &= ~B["sent"]; idx = np.nonzero(keep)[0]
            if len(idx) == 0: continue
            col = np.stack([np.bincount(inv, self.Cf[m, ch], minlength=B["n"])[idx] for ch in range(3)], 1) / cnt[idx, None]
            u = B["u"][idx]; off = u.min(0)
            s = L.cb.encode((u - off).astype(np.int32), np.round(col).astype(np.uint8))
            out += struct.pack("<BI3H", bi, len(s), *map(int, off)) + s; marks.append((bi, idx))
        return out, marks

    def update(self, k, traj, dt):
        budget = self.rate * dt; hdr = b""
        if self.R is None:
            self._setup(traj); GUP[:] = self.R[1]  # ground-plane normal (world), from the first keyframe's cloud
            hdr = self.R.astype(np.float32).tobytes() + self.o.astype(np.float32).tobytes() + struct.pack("<fB", SPL, len(LEVELS)) + LEVELS.astype(np.float32).tobytes()
        pred = predicted_poses(traj, dt); zp = zmin(self.P, pred); zh = zmin(self.P, hist_poses(traj))
        ztrue = np.minimum(zp, zh); z = np.minimum(zp, zh * PASTW); unseen = ~np.isfinite(ztrue)
        dist = np.linalg.norm(self.P - pred[-1][1], axis=1); ztrue[unseen] = dist[unseen]; z[unseen] = dist[unseen] * FARW
        z *= L.M; ztrue *= L.M
        target = budget - len(hdr) - 4; probes = []; a = self.alpha; s = self.slope
        for it in range(7):
            b, mk = self._chunk(a, z, ztrue); probes.append((a, len(b), b, mk))
            good = [p for p in probes if p[1] <= target]
            if good and max(good, key=lambda p: p[1])[1] >= FILL * target: break
            if len(b) == 0: break
            big = [p for p in probes if p[1] > target]; tgt = np.log(0.995 * target)
            if good and big:  # bracketed: log-log interpolation between the two closest probes
                (a1, b1) = max(big, key=lambda p: p[0])[:2]; (a2, b2) = min(good, key=lambda p: p[0])[:2]
                s = float(np.clip(-(np.log(b2) - np.log(b1)) / (np.log(a2) - np.log(a1)), 0.5, 5)); self.slope = s
                a = float(np.exp(np.log(a1) + (np.log(b1) - tgt) / s))
            else:
                if len(probes) >= 2:
                    (a1, b1), (a2, b2) = probes[-2][:2], probes[-1][:2]
                    if a1 != a2 and b1 != b2 and b1 > 0 and b2 > 0:
                        s = float(np.clip(-(np.log(b2) - np.log(b1)) / (np.log(a2) - np.log(a1)), 0.5, 5)); self.slope = s
                a = float(a * np.exp((np.log(len(b)) - tgt) / s))
        good = [p for p in probes if p[1] <= target]
        while not good:
            a *= 1.6; b, mk = self._chunk(a, z, ztrue)
            if len(b) <= target: good = [(a, len(b), b, mk)]
        a, n, b, mk = max(good, key=lambda p: p[1]); self.alpha = a
        for bi, idx in mk: self.bands[bi]["sent"][idx] = True
        if E("DBG"): print(f"   slot {k}: {len(probes)} probes, alpha {a:.4f}, {n}/{target:.0f} B, bands " + " ".join(f"{LEVELS[bi]}m:{len(idx)}" for bi, idx in mk))
        return hdr + b

class Decoder:
    def __init__(self): self.R = None
    def apply(self, b):
        p = 0
        if self.R is None:
            self.R = np.frombuffer(b[:36], np.float32).reshape(3, 3).astype(float); self.o = np.frombuffer(b[36:48], np.float32).astype(float)
            self.spl, nb = struct.unpack("<fB", b[48:53]); self.levels = np.frombuffer(b[53:53 + 4 * nb], np.float32).astype(float); p = 53 + 4 * nb
            self.vox = [[] for _ in range(nb)]
        while p < len(b):
            bi, n, ox, oy, oz = struct.unpack("<BI3H", b[p:p + 11]); p += 11; u, c = L.cb.decode(b[p:p + n]); p += n
            self.vox[bi].append((np.asarray(u).astype(np.int64) + [ox, oy, oz], np.asarray(c)))
    def map(self):
        vb = self.levels / L.M; bands = []
        for bi, recs in enumerate(self.vox):
            if not recs: continue
            u = np.concatenate([r[0] for r in recs]); c = np.concatenate([r[1] for r in recs])
            _, i = np.unique(keyf(u), return_index=True); bands.append((vb[bi], u[i], c[i]))
        out = []
        for v, u, c in bands:  # finest band wins inside its footprint
            keep = np.ones(len(u), bool); ku = keyf(u)
            for vf, uf, cf in bands:
                if vf < v: keep &= ~np.isin(ku, keyf(uf // int(round(v / vf))))
            out.append((v, u[keep], c[keep]))
        vmin = min(v for v, _, _ in out); pts, cols = [], []
        for v, u, c in out:
            kk = int(np.ceil(v / vmin - 1e-3)); g = (np.arange(kk) + 0.5) / kk
            flat = np.stack(np.meshgrid(g, [0.5], g, indexing="ij"), -1).reshape(-1, 3)
            wall = np.zeros(len(u), bool)
            if DMODE == "adapt" and kk > 1:  # coarse voxel stacked on a vertical neighbour (slope/wall/tree): fill the cube, not a sheet
                ku = keyf(u); wall = np.isin(ku, keyf(u + [0, 1, 0])) | np.isin(ku, keyf(u + [0, -1, 0]))
            full = np.stack(np.meshgrid(g, g, g, indexing="ij"), -1).reshape(-1, 3)
            for m, sub in ((~wall, flat), (wall, full)):
                if m.any(): pts.append(((u[m][:, None, :] + sub[None]) * v + self.o).reshape(-1, 3)); cols.append(np.repeat(c[m], len(sub), 0))
        return np.concatenate(pts) @ self.R, np.concatenate(cols), vmin * self.spl

def make_encoder(rate, P, C): return Encoder(rate, P, C)
def make_decoder(): return Decoder()
