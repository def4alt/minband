"""View-dependent LOD: each point gets an effective viewing distance zeff from the flight path
(48 poses incl. +5 s extension, frustum-tested, no occlusion: scene is ~2.5D and >97% of voxels
are visible anyway).  Ideal voxel size = ALPHA * zeff, snapped to a band list LEVELS (metres).
Each band is voxelised on its own grid (common origin) and coded with compress_study/codec_combo.
Decoder densifies coarse voxels into k^3 sub-points so a single splat (finest v * SPL) fills them.
Points never inside any frustum are dropped.
Env: BUDGET bytes (default 57500, encoder bisects ALPHA to fit) or fixed ALPHA; LEVELS (0.5,0.7,1.0,1.4,2.0); SPL (0.8 x finest v); MINCNT (2: drop voxels with <2 source pts); DMODE (2d|adapt|3d); ZMODE (min|hm)."""
import os, struct, numpy as np
import lod_common as L
DENS = float(os.environ.get("DENS", 1))

def zeff(P, traj, mode):
    Rs, Ps = L.poses(traj, 48)
    W, H, FX = L.Q.W, L.Q.H, L.Q.FX
    zmin = np.full(len(P), np.inf); acc = np.zeros(len(P)); n = np.zeros(len(P))
    for R, c in zip(Rs, Ps):
        q = (P - c) @ R; z = q[:, 2]; zz = np.maximum(z, 1e-6)
        u = FX * q[:, 0] / zz + W / 2; v = FX * q[:, 1] / zz + H / 2
        k = (z > 0.05) & (u >= 0) & (u < W) & (v >= 0) & (v < H)
        zmin[k] = np.minimum(zmin[k], z[k]); acc[k] += 1 / z[k] ** 2; n[k] += 1
    if mode == "hm":
        with np.errstate(divide="ignore", invalid="ignore"): z = np.sqrt(n / acc)
        z[n == 0] = np.inf; return z * L.M
    return zmin * L.M

def _enc(Q3, c, z, o, R, alpha, levels, spl):
    want = alpha * z
    band = np.abs(np.log(want)[:, None] - np.log(levels)[None]).argmin(1)
    out = R.astype(np.float32).tobytes() + np.array([*o], np.float32).tobytes() + struct.pack("<fB", spl, len(levels))
    for b, vm in enumerate(levels):
        m = band == b; v = vm / L.M
        if m.sum() == 0: out += struct.pack("<fI", v, 0); continue
        u, col, cnt, _ = L.voxelize(Q3[m], c[m], v, o)
        mc = int(os.environ.get("MINCNT", 2))
        if mc > 1: u, col = u[cnt >= mc], col[cnt >= mc]
        if len(u) == 0: out += struct.pack("<fI", v, 0); continue
        s = L.cb.encode(u.astype(np.int32), col); out += struct.pack("<fI", v, len(s)) + s
    return out

def encode(P, rgb, traj):
    spl = float(os.environ.get("SPL", 0.8))
    levels = np.array([float(x) for x in os.environ.get("LEVELS", "0.5,0.7,1.0,1.4,2.0").split(",")])
    z = zeff(P, traj, os.environ.get("ZMODE", "min")); keep = np.isfinite(z)
    R = L.ground_frame(P, traj); Q3 = (P @ R.T)[keep]; c = rgb[keep]; o = Q3.min(0); z = z[keep]
    if "ALPHA" in os.environ: return _enc(Q3, c, z, o, R, float(os.environ["ALPHA"]), levels, spl)
    budget = float(os.environ.get("BUDGET", 57500)); lo, hi = np.log(0.004), np.log(0.04); best = None
    for _ in range(int(os.environ.get("ITERS", 8))):  # bisection on log(alpha): largest bitstream within budget
        mid = (lo + hi) / 2; b = _enc(Q3, c, z, o, R, np.exp(mid), levels, spl)
        if len(b) <= budget: best, hi = b, mid
        else: lo = mid
    return best if best is not None else _enc(Q3, c, z, o, R, np.exp(hi), levels, spl)

def decode(b):
    R = np.frombuffer(b[:36], np.float32).reshape(3, 3).astype(float); o = np.frombuffer(b[36:48], np.float32).astype(float)
    spl, nb = struct.unpack("<fB", b[48:53]); p = 53; vs, chunks = [], []
    for _ in range(nb):
        v, n = struct.unpack("<fI", b[p:p + 8]); p += 8
        if n: chunks.append((v, L.cb.decode(b[p:p + n]))); vs.append(v)
        p += n
    vmin = min(vs); pts, cols = [], []
    for v, (vox, rgb) in chunks:
        k = int(np.ceil(v / vmin * DENS - 1e-3)); g = (np.arange(k) + 0.5) / k
        vox = np.asarray(vox).astype(np.int64); rgb = np.asarray(rgb)
        flat = np.stack(np.meshgrid(g, [0.5], g, indexing="ij"), -1).reshape(-1, 3)
        full = np.stack(np.meshgrid(g, g, g, indexing="ij"), -1).reshape(-1, 3)
        dm = os.environ.get("DMODE", "2d")
        if dm == "3d": wall = np.ones(len(vox), bool)
        elif dm == "2d" or k == 1: wall = np.zeros(len(vox), bool)
        else:  # vertical neighbour (ground-frame axis 1) in same band -> part of a wall: fill full cube
            key = lambda a: (a[:, 0] * 4096 + a[:, 1]) * 4096 + a[:, 2]
            ks = np.sort(key(vox)); wall = np.zeros(len(vox), bool)
            for d in (-1, 1):
                kk = key(vox + [0, d, 0]); i = np.clip(np.searchsorted(ks, kk), 0, len(ks) - 1); wall |= ks[i] == kk
        for m, sub in ((~wall, flat), (wall, full)):
            q = (vox[m][:, None, :] + sub[None]) * v + o
            pts.append(q.reshape(-1, 3)); cols.append(np.repeat(rgb[m], len(sub), 0))
    return np.concatenate(pts) @ R, np.concatenate(cols), float(vmin) * spl
