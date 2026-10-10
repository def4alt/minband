"""stream_sharp_render: FREE SHARPNESS AT THE RECEIVER.  The bitstream is stream_best_pkt's, byte for byte (encoder,
split_records, record framing, context tables, 16-colour palette all taken as is); only Decoder.map() changes, i.e.
how the received voxels are turned into the point set that quality.render draws.

quality.render draws every point as a camera-facing square of `splat` metres anchored at the projected point
(it extends to +u/+v, i.e. right/down in the image).  The reference is drawn the same way with 0.25 m squares on the
dense cloud, so a reference surface = its true footprint + a 0.25 m overhang to the right/down.  stream_best drew a
0.5 m voxel as ONE point with a 0.5 m square (overhang 0.5 m: every edge of the map is shifted ~0.25 m against the
reference -> ~5 px at 10 m) and a 2 m voxel as 16 such points.  Here (all decoder-only, flags via env):
  * SHARP_LAT (default 2): the map is drawn on a lattice of finest/LAT metres (0.25 m) with splat = lattice step:
    a 0.5 m voxel is a flat 2x2 patch of 0.25 m squares, a 2 m voxel an 8x8 patch -> every edge lands where the
    reference draws it; the splat is the same for every band because the renderer takes one value, so per-band
    splat sizes are realised by the number of lattice cells per voxel.  LAT=1 reproduces stream_best's look.
  * SHARP_EDGE (default 0): also draw the +1 lattice row/column at the voxel's far side (the reference's overhang)
    unless that lattice cell belongs to another received voxel.  With EDGE the lattice points sit at the cell corner
    (SHARP_ANCHOR 0.0), without it at the cell centre (0.5) like stream_best.
  * SHARP_GROUND (default 0): "1": the ground level is the modal height of the finest received band; a coarse voxel
    (1/2/4 m) whose height range contains the ground is drawn as a flat patch AT the ground level instead of at its
    mid-height (a 4 m ground voxel drawn 2 m too high lands ~1-2 m off in the operator view); "local": a coarse
    voxel takes the mean height of the finer received voxels in its footprint (else 3x3 neighbourhood), i.e. it
    continues the received terrain / roofs instead of floating at its mid-height.
  * SHARP_CUBE (default 0): 'stack' draws voxels that have a received voxel directly above or below (walls, trees,
    house fronts) as a lattice shell (top + bottom + 4 side faces) instead of a flat mid-height patch; 'coarse'
    only for coarse voxels that do not contain the ground.  Costs 3-6x points for those voxels.
  * SHARP_SPL (default 1.25): splat multiplier on the lattice step (1.0 = squares exactly tile the lattice).
MEASURED (replay of one cached stream_best_pkt PKT=500 run at 20 kbit/s, 1 s slots, replay_map.py; base = stream_best
map: LIVE 18.10/0.568 FINAL 18.99/0.653 TIMEAVG 17.42/0.543, near-field ALL 17.47/0.465):
  LAT=2 EDGE=0 SPL=1.0  LIVE 18.09/0.578 FINAL 19.10/0.669 TIMEAVG 17.45/0.558 near ALL 17.63/0.493  (best SSIM)
  LAT=2 EDGE=0 SPL=1.25 LIVE 18.08/0.576 FINAL 19.10/0.666 TIMEAVG 17.44/0.556 near ALL 17.62/0.488  (default)
  LAT=2 EDGE=0 SPL=1.5  LIVE 18.09/0.576 FINAL 19.14/0.665 TIMEAVG 17.46/0.556 near ALL 17.67/0.487  (fewest holes)
  LAT=2 EDGE=1 (corner lattice + overhang row): FINAL 19.05/0.658, LIVE 18.02 -> the reference-overhang emulation
      does NOT help; LAT=4: -0.4 dB; SPL=0.8: gaps, worse; GROUND=1 / local: -0.05..-0.2 dB (hilly scene, the
      mid-height patch is the better prior); CUBE=stack / coarse: -1.2 / -1.7 dB (shells over-cover: rejected).
  Cost: ~4x points (1.2 M vs 0.3 M) -> map()+render ~4x slower; LAT=1 restores the old point count.
No blending, no smoothing, no invented voxels: every drawn square carries the palette colour of its own voxel.
API: make_encoder / make_decoder / split_records / header_len as stream_best_pkt (env PKT etc. apply)."""
import os, sys, numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stream_best_pkt as SP, lod_common as L
from stream_best import keyf

LAT = int(os.environ.get("SHARP_LAT", 2)); EDGE = os.environ.get("SHARP_EDGE", "0") == "1"
ANCHOR = float(os.environ.get("SHARP_ANCHOR", 0.0 if EDGE else 0.5)); GROUND = os.environ.get("SHARP_GROUND", "0")  # "0" mid-height, "1" global ground level, "local" terrain continuation
CUBE = os.environ.get("SHARP_CUBE", "0"); SPLF = float(os.environ.get("SHARP_SPL", 1.25))
make_encoder = SP.make_encoder; split_records = SP.split_records; header_len = SP.header_len
UP = np.array([0, 1, 0], np.int64)  # ground-frame y = plane normal (height axis of the voxel grid)

def unrefined(levels, known, cols):
    """[(bi, u, col)] per band with the voxels that a finer received band already refines removed (as stream_best)."""
    bands = [(bi, known[bi], cols[bi]) for bi in range(len(levels)) if len(known[bi])]
    out = []
    for bi, u, c in bands:
        keep = np.ones(len(u), bool)
        for bj, uf, _ in bands:
            if levels[bj] < levels[bi]: keep &= ~np.isin(keyf(u), keyf(uf // int(round(levels[bi] / levels[bj]))))
        out.append((bi, u[keep], c[keep]))
    return out

def ground_level(levels, out, vfine, minvox=200):
    """ground height in FINEST voxel units (int) = modal height index of the finest band that has >= minvox voxels."""
    for bi, u, _ in sorted(out, key=lambda b: levels[b[0]]):
        if len(u) < minvox: continue
        kf = int(round(levels[bi] / vfine)); y = u[:, 1]; cnt = np.bincount(y - y.min()); return int(y.min() + cnt.argmax()) * kf + kf // 2
    return None

def local_height(vb, out, bi, u):
    """SHARP_GROUND=local: height fraction (0..1) inside every coarse voxel u of band bi taken from the FINER received
    voxels (any finer band) in the same xz footprint (mean height), else in the 3x3 footprint neighbourhood, else 0.5:
    a coarse voxel next to received fine terrain continues that terrain instead of floating at its mid-height."""
    X, Z, Y = [], [], []
    for bj, uj, _ in out:
        if vb[bj] >= vb[bi] or len(uj) == 0: continue
        r = int(round(vb[bi] / vb[bj])); X.append(uj[:, 0] // r); Z.append(uj[:, 2] // r); Y.append((uj[:, 1] + 0.5) / r)
    h = np.full(len(u), 0.5)
    if not X: return h
    X, Z, Y = np.concatenate(X), np.concatenate(Z), np.concatenate(Y)
    key = (X << 21) | Z; cells, inv = np.unique(key, return_inverse=True); sy = np.bincount(inv.ravel(), Y); cn = np.bincount(inv.ravel()).astype(float)
    def look(dx, dz):
        q = ((u[:, 0] + dx) << 21) | (u[:, 2] + dz); i = np.searchsorted(cells, q); i = np.minimum(i, len(cells) - 1); hit = cells[i] == q
        return np.where(hit, sy[i], 0.0), np.where(hit, cn[i], 0.0)
    s0, c0 = look(0, 0); s3, c3 = s0.copy(), c0.copy()
    for dx in (-1, 0, 1):
        for dz in (-1, 0, 1):
            if dx or dz: a, b = look(dx, dz); s3 += a; c3 += b
    own = c0 > 0; nb = ~own & (c3 > 0)
    h[own] = s0[own] / c0[own] - u[own, 1]; h[nb] = s3[nb] / c3[nb] - u[nb, 1]
    return np.clip(h, 0.0, 1.0)

def stacked(u):
    """voxels with a received voxel of the same band directly above or below."""
    k = keyf(u); return np.isin(keyf(u + UP), k) | np.isin(keyf(u - UP), k)

def lattice_patch(u, kk, h, edge):
    """flat patch: lattice cells (X, Y, Z) of every voxel u (band with kk lattice cells per axis) at height fraction
    h (per voxel). -> cells int64[n*m, 3] in lattice units (Y = lattice row of the patch height), own mask bool."""
    r = np.arange(kk + (1 if edge else 0)); ox, oz = np.meshgrid(r, r, indexing="ij"); ox, oz = ox.ravel(), oz.ravel()
    own = (ox < kk) & (oz < kk); n, m = len(u), len(ox)
    X = (u[:, 0] * kk)[:, None] + ox[None]; Z = (u[:, 2] * kk)[:, None] + oz[None]
    Y = np.floor((u[:, 1] + h) * kk).astype(np.int64)[:, None].repeat(m, 1)
    yl = (u[:, 1] + h)[:, None].repeat(m, 1)  # exact patch height in voxel units (not snapped to the lattice)
    return np.stack([X, Y, Z], -1).reshape(-1, 3), np.tile(own, n), yl.ravel()

def lattice_shell(u, kk):
    """lattice shell of a cube voxel: top + bottom layers (kk+1)^2 and the 4 side faces -> cells int64[n*m, 3]."""
    r = np.arange(kk + 1); X, Y, Z = np.meshgrid(r, r, r, indexing="ij")
    face = (X == 0) | (X == kk) | (Y == 0) | (Y == kk) | (Z == 0) | (Z == kk)
    off = np.stack([X[face], Y[face], Z[face]], 1)
    cells = ((u * kk)[:, None, :] + off[None]).reshape(-1, 3)
    return cells, (cells[:, 1] + ANCHOR) / kk

def sharp_map(levels, known, cols, o, R, lat=LAT, edge=EDGE, anchor=ANCHOR, ground=GROUND, cube=CUBE, splf=SPLF):
    """decoder-only map: -> (pts world float[M,3], rgb uint8[M,3], splat)."""
    vb = levels / L.M; out = unrefined(vb, known, cols)
    if not out: return np.zeros((0, 3)), np.zeros((0, 3), np.uint8), vb.min() * splf
    vfine = vb.min(); s = vfine / lat
    g = ground_level(vb, out, vfine) if ground == "1" else None
    cells, col, own, yv = [], [], [], []  # yv: height of every lattice point in voxel units of its band
    for bi, u, c in out:
        kk = int(round(vb[bi] / s)); kf = int(round(vb[bi] / vfine)); n = len(u)
        h = np.full(n, 0.5); onground = np.zeros(n, bool)
        if ground == "local" and kf > 1: h = local_height(vb, out, bi, u)
        elif g is not None and kf > 1:
            lo = u[:, 1] * kf; onground = (g >= lo) & (g < lo + kf); h[onground] = (g - lo[onground] + 0.5) / kf
        asbox = np.zeros(n, bool)
        if cube == "stack": asbox = stacked(u) & ~onground
        elif cube == "coarse": asbox = (kf > 1) & ~onground
        if asbox.any():
            sh, y = lattice_shell(u[asbox], kk); cells.append(sh); col.append(np.repeat(c[asbox], len(sh) // asbox.sum(), 0)); own.append(np.ones(len(sh), bool)); yv.append(y * vb[bi])
        rest = ~asbox
        if rest.any():
            pc, ow, y = lattice_patch(u[rest], kk, h[rest], edge); cells.append(pc); col.append(np.repeat(c[rest], len(pc) // rest.sum(), 0)); own.append(ow); yv.append(y * vb[bi])
    cells = np.concatenate(cells); col = np.concatenate(col); own = np.concatenate(own); yv = np.concatenate(yv)
    if edge:  # drop overhang cells that another voxel owns (the reference draws that voxel's colour there)
        k = keyf(cells); keep = own | ~np.isin(k, k[own]); cells, col, yv = cells[keep], col[keep], yv[keep]
    pts = (cells + anchor) * s; pts[:, 1] = yv; pts += o
    return pts @ R, col, s * splf

class Decoder(SP.Decoder):
    def map(self):
        cols = [self.pal[self.st.cidx[bi]] for bi in range(len(self.levels))]
        return sharp_map(self.levels, self.st.known, cols, self.o, self.R)

def make_decoder(): return Decoder()
