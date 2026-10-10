"""Stage 3 of tools/recon3d: detections into the reconstruction, in metres, as 3D tracks.

    conda run -n YOLO-CUDA-13 python tools/recon3d/lift.py <run> [--car-length 4.5]

Inputs (from slam_export.py and detect.py): <run>/slam/{meta,poses}.json, slam/frames/*.npz,
slam/keyframes.npz, detections.json, clip.mp4.

1. Lift. A detection's mask selects its own pixels in the frame's pointmap (MASt3R-SLAM gives every
   frame a pixel-aligned 3D pointmap); the confident ones at the object's depth, in the world frame,
   are the object's visible surface. A box-only detection (no "poly": a VisDrone detector has no masks)
   selects the central 60 % of its box instead (at least 3x3 pointmap pixels). The median of those
   pixels' points gives the object's *direction* from the camera.
   Its *range* comes from the reconstruction: the ray meets the static map's terrain (a height field,
   lowest surface per 0.5 m cell) at the class's centre height above it. One frame's depth jitters
   by metres along the ray at a shallow look-down angle; the fused multi-view terrain does not
   (DESIGN.md 5: the ray against the server's own terrain, done on the edge). No flat ground.
2. Metric frame. The ground plane is fitted to the static map (RANSAC, normals near the cameras' up);
   up is its normal, north the first camera's heading, origin the ground below the first camera.
   Monocular SLAM has no scale, so the scale comes from the cars: the median ground-plane length of
   the car surfaces = `--car-length` (4.5 m). Stated assumption, like tools/footage's person width.
   Big cars with a mask measure it; without masks, big boxes do, corrected by `--box-gain`.
3. Track. ByteTrack's 2D ids give the association; per object a causal constant-velocity Kalman
   filter on the ground gives position and velocity (no smoothing with future frames: the edge
   cannot); the height is the terrain under the filtered position plus the class's centre height.
   A 2D track born where a lost one of the same coarse class was (a parked object's static estimate, a
   mover's extrapolation) is the same object: its prediction must land in the new box and agree in 3D
   (re-identification of ByteTrack's id switches). A track is reported only once it has lived REPORT_AFTER_S
   (or the detector is sure of it): a flicker never reaches the edge.
4. Appearance. Per object, from its best view in its first 2 s: its footprint measured in the
   reconstruction (length and width, clamped to class priors), the masked image crop (WebP with
   alpha, <= 128 px), and the projection from the object's frame (x along its heading, z up) into
   that crop. The page draws a solid of that size with the drone's own view of the object projected
   onto the faces the drone saw: the operator sees that car, not a blob, and its pixels cross the
   link once while its motion keeps crossing as contact reports.
5. Static map. Keyframe pointmaps fused in the metric frame, with every detected object masked out,
   so moving cars leave no smears; voxel-filtered.

Writes <run>/scene/: tracks3d.csv (tick,id,class,e,n,u,ve,vn,vu,conf,bu,bv,bw,bh,ce,yaw), cameras.csv
(tick,e,n,u,yaw,pitch,roll), chips.json, map.bin (+ map.json), summary.json.
"""
import argparse
import json
import os
import pathlib
import pickle
import zlib
from collections import defaultdict

import cv2
import numpy as np

ap = argparse.ArgumentParser()
ap.add_argument("run")
ap.add_argument("--car-length", type=float, default=4.5)
ap.add_argument("--fps", type=float, default=30.0)
ap.add_argument("--conf", type=float, default=1.5, help="pointmap confidence floor")
ap.add_argument("--voxel", type=float, default=0.15, help="static map voxel (m)")
ap.add_argument("--cell", type=float, default=0.5, help="terrain height field cell (m)")
ap.add_argument("--min-hits", type=int, default=15, help="2D hits before a track is reported (15, half a second of frames: "
                "the detector's flickers, a roof or a bush seen for a few frames, never reach the edge, whose contacts live on for "
                "a minute; Labrador clip, against 6: individual recall 85.9 -> 88.6 %%, mover sightings 91.0 -> 89.2 %%, unmatched 378 -> 370)")
ap.add_argument("--far-cell", type=float, default=4.0, help="cell of the far terrain tier (m): every keyframe point, any confidence")
ap.add_argument("--map-mask", default="inner", choices=["inner", "full"], help="what of each detection is cut out of the "
                "terrain's map: its own pixels (inner: the ground between parked cars stays) or its dilated box (full, the old way)")
ap.add_argument("--yaw-min-elev", type=float, default=20.0, help="a view counts for a static vehicle's footprint axis only when the camera looks down on it at least this steeply (deg)")
ap.add_argument("--box-gain", type=float, default=0.77, help="a box-only car sighting measures this fraction of a mask's length (scale from boxes)")
ap.add_argument("--box-min-px", type=float, default=80, help="smallest box (long side, px) whose car sighting counts for the scale when it has no mask")
ap.add_argument("--cache", action="store_true", help="keep the lifted and ranged detections (the slow part) in <run>/lift_stage1.pkl and lift_ranging.pkl and reuse them: for iterating on tracking; delete both after changing the lifting, the terrain or the scale")
args = ap.parse_args()

run = pathlib.Path(args.run)
slam = run / "slam"
out = run / "scene"
out.mkdir(exist_ok=True)
meta = json.loads((slam / "meta.json").read_text())
poses = {p["i"]: np.array(p["T"]) for p in json.loads((slam / "poses.json").read_text()) if p["ok"]}
dets = json.loads((run / "detections.json").read_text())["frames"]
H, W = meta["pm_h"], meta["pm_w"]
SW, SH, CW, CH = meta["scale_w"], meta["scale_h"], meta["crop_w"], meta["crop_h"]
TICK_HZ = 120
COARSE = {0: 0, 1: 1, 2: 1, 3: 1, 5: 1, 7: 1}
BOX_INNER = 0.6      # a box-only detection's pointmap pixels: the central 60 % of its box


def to_pm(xy):
    """Source pixels -> pointmap pixels."""
    xy = np.asarray(xy, np.float64)
    return np.stack([xy[..., 0] / SW - CW, xy[..., 1] / SH - CH], -1)


def mask_of(d, full=False):
    """Pointmap pixels of a detection: its polygon, or for a box-only detection (no "poly") the box's central
    60 % in x and in y, at least 3x3 pixels. `full`: the whole box, to mask an object out of the map."""
    m = np.zeros((H, W), np.uint8)
    if "poly" in d:
        cv2.fillPoly(m, [np.round(to_pm(d["poly"])).astype(np.int32)], 1)
        if m.sum() > 30:
            m = cv2.erode(m, np.ones((3, 3), np.uint8))
    else:
        (x1, y1), (x2, y2) = to_pm([d["box"][:2], d["box"][2:]])
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        hw, hh = (x2 - x1) / 2 * (1.0 if full else BOX_INNER), (y2 - y1) / 2 * (1.0 if full else BOX_INNER)
        xa, xb, ya, yb = int(np.floor(cx - hw)), int(np.floor(cx + hw)), int(np.floor(cy - hh)), int(np.floor(cy + hh))
        if xb - xa < 2:
            xa, xb = int(np.floor(cx)) - 1, int(np.floor(cx)) + 1
        if yb - ya < 2:
            ya, yb = int(np.floor(cy)) - 1, int(np.floor(cy)) + 1
        m[max(ya, 0):max(yb + 1, 0), max(xa, 0):max(xb + 1, 0)] = 1
    return m.astype(bool)


def world(M, X):
    return X @ M[:3, :3].T + M[:3, 3]


def frames_rgb():
    cap = cv2.VideoCapture(str(run / "clip.mp4"))
    while True:
        ok, img = cap.read()
        if not ok:
            return
        yield cv2.cvtColor(cv2.resize(img, (W, H), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB)


# ---- 1. lift every detection (SLAM frame) ---------------------------------------------------------
lifted = {}  # (frame, det index) -> dict(p, pts, rgb)
STAGE1_CACHE = run / "lift_stage1.pkl"
if args.cache and STAGE1_CACHE.exists():
    lifted = pickle.load(open(STAGE1_CACHE, "rb"))
    print(f"stage 1 from {STAGE1_CACHE.name}")
else:
    for f, rgb in zip(dets, frames_rgb()):
        i = f["i"]
        if i not in poses or not f["dets"]:
            continue
        pm = np.load(slam / "frames" / f"f_{i:05d}.npz")
        X, C = pm["X"].astype(np.float64), pm["C"].astype(np.float32)
        for k, d in enumerate(f["dets"]):
            mk = mask_of(d)
            m = mk & (C > args.conf)
            lowconf = m.sum() < 4
            if lowconf:
                # MASt3R does not trust this object's depth (small, fast, or both): its pixels still give
                # the direction, and the terrain gives the range (below), within wider bounds.
                m = mk
                if m.sum() < 1:
                    continue
            Xo = X[m]
            z = Xo[:, 2]
            zm = np.median(z)
            keep = (z > 0.85 * zm) & (z < 1.18 * zm) if not lowconf else np.ones(len(z), bool)   # the object's depth, not what bleeds in at its edges
            if keep.sum() < 1:
                continue
            Pw = world(poses[i], Xo[keep])
            lifted[(i, k)] = {"p": np.median(Pw, 0), "pts": Pw, "rgb": rgb[m][keep], "lowconf": lowconf}
if args.cache and not STAGE1_CACHE.exists():
    pickle.dump({k: {**v, "rgb": None} for k, v in lifted.items()}, open(STAGE1_CACHE, "wb"), protocol=4)   # (rgb is not used after this stage)
print(f"lifted {len(lifted)} of {sum(len(f['dets']) for f in dets)} detections")

# ---- 2. metric frame -------------------------------------------------------------------------------
kf = np.load(slam / "keyframes.npz")
det_by_frame = {f["i"]: f["dets"] for f in dets}
map_pts, map_rgb, far_pts = [], [], []
for k, fid in enumerate(kf["frame_id"]):
    fid = int(fid)
    if fid not in poses:
        continue
    X, C, rgb = kf["X"][k].astype(np.float64), kf["C"][k].astype(np.float32), kf["rgb"][k]
    # The map is the edge's own terrain (it is not sent): each detection's body is cut out so a moving car leaves no
    # smear, but only its own pixels: cutting the whole dilated box took the ground between the parked cars of a lot
    # with it, and the cars there had nothing to stand on.
    objects = np.zeros((H, W), np.uint8)
    for d in det_by_frame.get(fid, []):
        objects |= mask_of(d, full=args.map_mask == "full").astype(np.uint8)
    objects = cv2.dilate(objects, np.ones((7, 7) if args.map_mask == "full" else (3, 3), np.uint8)).astype(bool)
    ok = (C > args.conf) & ~objects
    map_pts.append(world(poses[fid], X[ok]))
    map_rgb.append(rgb[ok])
    # Every point, any confidence: MASt3R's far field (the top of an oblique frame, 300-600 m out) sits at its
    # confidence floor, yet its depth is within ~20 % (cars there measure ~1.2x the near cars' box size per
    # metre of range); the far terrain tier is built from these, coarse.
    far_pts.append(world(poses[fid], X[~objects & (C > 0)]))
map_pts, map_rgb = np.concatenate(map_pts), np.concatenate(map_rgb)
far_pts = np.concatenate(far_pts)

Rs = {i: M[:3, :3] / np.cbrt(np.linalg.det(M[:3, :3])) for i, M in poses.items()}
cams = {i: M[:3, 3] for i, M in poses.items()}
up_prior = np.mean([-R[:, 1] for R in Rs.values()], 0)
up_prior /= np.linalg.norm(up_prior)
rng = np.random.default_rng(0)
sample = map_pts[rng.choice(len(map_pts), min(200_000, len(map_pts)), replace=False)]
cam_arr = np.array(list(cams.values()))
tau = 0.01 * np.median(np.linalg.norm(sample - cam_arr.mean(0), axis=1))
best = (0, None)
for _ in range(3000):
    a, b, c = sample[rng.choice(len(sample), 3, replace=False)]
    n = np.cross(b - a, c - a)
    if np.linalg.norm(n) < 1e-12:
        continue
    n /= np.linalg.norm(n)
    if abs(n @ up_prior) < np.cos(np.radians(40)):
        continue
    inl = np.abs((sample - a) @ n) < tau
    if inl.sum() > best[0]:
        best = (inl.sum(), inl)
inl = sample[best[1]]
centroid = inl.mean(0)
U = np.linalg.svd(inl - centroid, full_matrices=False)[2][2]
if U @ up_prior < 0:
    U = -U
c0 = cams[min(cams)]
f0 = Rs[min(Rs)][:, 2]
N = f0 - (f0 @ U) * U
N /= np.linalg.norm(N)
E = np.cross(N, U)
O = c0 - ((c0 - centroid) @ U) * U
B = np.stack([E, N, U])

# Scale from the cars: ground-plane length of each car's visible surface, median over sightings. Only big
# sightings count (a small object's pointmap depth smears: its length comes out several times too long). A
# box-only sighting has no mask: its central 60 % window measures ~0.77 of the mask's length on the big cars
# (boxes >= 80 px) of the Labrador clip (oblique view, cars 100-400 m out), so box sightings need a box >= --box-min-px
# and count as length / --box-gain: approximate, depends on the view (a wrong gain is a wrong scale by the same factor).
# Masks decide when there are enough of them (a seg model or a seg-mask pass in detect.py gives them to the big cars).
lengths, box_lengths = [], []
for (i, k), L in lifted.items():
    d = det_by_frame[i][k]
    x1, y1, x2, y2 = d["box"]
    boxed = "poly" not in d
    if d["cls"] != 2 or L["lowconf"] or len(L["pts"]) < (12 if boxed else 80) or (boxed and max(x2 - x1, y2 - y1) < args.box_min_px) \
            or x1 < 8 or y1 < 8 or x2 > meta["src_w"] - 8 or y2 > meta["src_h"] - 8:
        continue
    q = (L["pts"] - O) @ B[:2].T
    q -= q.mean(0)
    ev, evec = np.linalg.eigh(np.cov(q.T))
    major = q @ evec[:, 1]
    (box_lengths if boxed else lengths).append(np.percentile(major, 97) - np.percentile(major, 3))
scale_from = "masks"
if len(lengths) < 30:
    lengths, scale_from = lengths + [v / args.box_gain for v in box_lengths], "masks and boxes" if lengths else "boxes"
scale = args.car_length / float(np.median(lengths))
print(f"ground plane: {best[0]} of {len(sample)} sample points within {tau:.4f}; car sightings {len(lengths)} ({scale_from}), "
      f"median car length {np.median(lengths):.4f} SLAM units -> scale {scale:.3f} m/unit")


def enu(P):
    return scale * ((np.asarray(P) - O) @ B.T)


# ---- cameras ---------------------------------------------------------------------------------------
cam_rows = []
for i in sorted(poses):
    pe = enu(cams[i])
    Re = B @ Rs[i]
    fwd, right = Re[:, 2], Re[:, 0]
    yaw = np.degrees(np.arctan2(fwd[0], fwd[1])) % 360
    pitch = np.degrees(np.arcsin(np.clip(fwd[2], -1, 1)))
    roll = np.degrees(np.arcsin(np.clip(-right[2] / max(np.cos(np.radians(pitch)), 1e-6), -1, 1)))
    cam_rows.append((round(i * TICK_HZ / args.fps), *pe, yaw, pitch, roll))
with open(out / "cameras.csv", "w") as fh:
    fh.write("tick,e,n,u,yaw,pitch,roll\n")
    for r in cam_rows:
        fh.write(",".join([str(r[0])] + [f"{v:.3f}" for v in r[1:]]) + "\n")

# ---- terrain: the static map's lowest surface per cell ------------------------------------------
# One surface per 0.5 m cell, the 20th percentile of the map's heights there (the ground under clutter).
# A bridge deck over a road is beyond it (the deck's cars go to the road below): tried as columns of
# occupied voxels, that was worse on streets (walls and eaves read as ground), so not used.
from scipy import ndimage  # noqa: E402

Pmap = enu(map_pts)
g_lo = Pmap[:, :2].min(0) - 5
gw, gh = (np.ceil((Pmap[:, :2].max(0) + 5 - g_lo) / args.cell)).astype(int)
gi = np.clip(((Pmap[:, 0] - g_lo[0]) / args.cell).astype(int), 0, gw - 1)
gj = np.clip(((Pmap[:, 1] - g_lo[1]) / args.cell).astype(int), 0, gh - 1)
flat = gj * gw + gi
order = np.lexsort((Pmap[:, 2], flat))
counts = np.bincount(flat, minlength=gw * gh)
starts = np.r_[0, np.cumsum(counts)[:-1]]
hf = np.full(gw * gh, np.nan)
has = counts >= 3
hf[has] = Pmap[order, 2][starts[has] + (0.2 * counts[has]).astype(int)]
hf = hf.reshape(gh, gw)
dist, (ii, jj) = ndimage.distance_transform_edt(np.isnan(hf), return_indices=True)
hf = hf[ii, jj]
hf[dist * args.cell > 6.0] = np.nan          # do not invent terrain more than 6 m from any observation
hf = np.where(np.isnan(hf), np.nan, ndimage.median_filter(np.nan_to_num(hf, nan=-1e3), size=3))
hf[hf < -100] = np.nan


def height_field(P, cell, lo, w, h, fill_m):
    """The 20th percentile of the heights per cell (>= 3 points), nearest-filled up to fill_m, median-smoothed."""
    gi = np.clip(((P[:, 0] - lo[0]) / cell).astype(int), 0, w - 1)
    gj = np.clip(((P[:, 1] - lo[1]) / cell).astype(int), 0, h - 1)
    flat = gj * w + gi
    order = np.lexsort((P[:, 2], flat))
    counts = np.bincount(flat, minlength=w * h)
    starts = np.r_[0, np.cumsum(counts)[:-1]]
    f = np.full(w * h, np.nan)
    ok = counts >= 3
    f[ok] = P[order, 2][starts[ok] + (0.2 * counts[ok]).astype(int)]
    f = f.reshape(h, w)
    dist, (ii, jj) = ndimage.distance_transform_edt(np.isnan(f), return_indices=True)
    f = f[ii, jj]
    f[dist * cell > fill_m] = np.nan
    f = np.where(np.isnan(f), np.nan, ndimage.median_filter(np.nan_to_num(f, nan=-1e3), size=3))
    f[f < -100] = np.nan
    return f


# The far tier: every keyframe point, in coarse cells, where the confident map has no ground (its 0.5 m cells end
# ~100-150 m out on an oblique flight). Its heights are good to ~1 m, which a shallow ray stretches into metres of
# range: the trackers below weigh it so (ray-aligned measurement noise).
Pfar = enu(far_pts)
Pfar = Pfar[(Pfar[:, 2] > -50) & (Pfar[:, 2] < 150)]
f_lo = np.minimum(g_lo, Pfar[:, :2].min(0) - 5)
fw, fh = (np.ceil((np.maximum(Pmap[:, :2].max(0), Pfar[:, :2].max(0)) + 5 - f_lo) / args.far_cell)).astype(int)
hf_far = height_field(Pfar, args.far_cell, f_lo, fw, fh, 30.0)


def terrain(e, n, tier=False):
    """Ground height at (e, n): the confident fine map, else the far tier; with tier=True also which (0 fine, 1 far, -1 none)."""
    e, n = np.asarray(e), np.asarray(n)
    i = np.clip(((e - g_lo[0]) / args.cell).astype(int), 0, gw - 1)
    j = np.clip(((n - g_lo[1]) / args.cell).astype(int), 0, gh - 1)
    z = hf[j, i]
    i2 = np.clip(((e - f_lo[0]) / args.far_cell).astype(int), 0, fw - 1)
    j2 = np.clip(((n - f_lo[1]) / args.far_cell).astype(int), 0, fh - 1)
    z2 = hf_far[j2, i2]
    out = np.where(np.isfinite(z), z, z2)
    if tier:
        return out, np.where(np.isfinite(z), 0, np.where(np.isfinite(z2), 1, -1))
    return out


# Height of the visible surface's median above the ground, by class (m).
H_C = {0: 0.9, 1: 0.7, 2: 0.75, 3: 0.7, 5: 1.5, 7: 1.3}
cam_enu = {i: enu(cams[i]) for i in cams}


def on_terrain(c, p, hc, wide=False):
    """Where the ray from camera c towards p meets the terrain at height hc above it (None: misses)."""
    d = p - c
    r0 = np.linalg.norm(d)
    d = d / r0
    r = np.arange((0.15 if wide else 0.3) * r0, (6.0 if wide else 3.0) * r0, 0.05)
    q = c + r[:, None] * d
    h = terrain(q[:, 0], q[:, 1])
    below = q[:, 2] <= h + hc
    if not below.any():
        return None
    k = int(np.argmax(below))
    if k == 0:
        return None
    # Linear refinement between the last sample above and the first below.
    a0 = q[k - 1, 2] - (h[k - 1] + hc)
    a1 = q[k, 2] - (h[k] + hc)
    w = a0 / (a0 - a1) if np.isfinite(a0) and a0 != a1 else 1.0
    rr = r[k - 1] + w * (r[k] - r[k - 1])
    return c + rr * d, rr / r0


ranged = 0
# A person on a motorcycle or bicycle is its rider, not a pedestrian: the detector boxes both. The
# two-wheeler carries the object; the rider's box is dropped when most of it lies inside one.
riders = 0
for f in dets:
    wheels = [d["box"] for d in f["dets"] if d["cls"] in (1, 3)]
    for k, d in enumerate(f["dets"]):
        if d["cls"] != 0 or (f["i"], k) not in lifted or not wheels:
            continue
        x1, y1, x2, y2 = d["box"]
        area = max(1.0, (x2 - x1) * (y2 - y1))
        for wx1, wy1, wx2, wy2 in wheels:
            ix = max(0.0, min(x2, wx2) - max(x1, wx1)); iy = max(0.0, min(y2, wy2) - max(y1, wy1 - 0.6 * (wy2 - wy1)))
            if ix * iy / area > 0.4:
                del lifted[(f["i"], k)]
                riders += 1
                break
print(f"riders dropped: {riders}")

ranged_far = 0
RANGING_CACHE = run / "lift_ranging.pkl"
cached_ranging = pickle.load(open(RANGING_CACHE, "rb")) if args.cache and RANGING_CACHE.exists() else None
for (i, k), L in lifted.items():
    if cached_ranging is not None:
        L.update(cached_ranging[(i, k)])
        ranged += L["ranged"]
        ranged_far += L["ranged"] and L["tier"] == 1
        continue
    cl = det_by_frame[i][k]["cls"]
    pe = enu(L["p"])
    hit = on_terrain(cam_enu[i], pe, H_C[cl], wide=L["lowconf"])
    lo_r, hi_r = (0.2, 5.0) if L["lowconf"] else (0.5, 2.0)
    if hit is not None and lo_r < hit[1] < hi_r:
        L["enu"], L["ratio"], L["ranged"] = hit[0], hit[1], True
        L["tier"] = int(terrain(hit[0][0], hit[0][1], tier=True)[1])
        ranged += 1
        ranged_far += L["tier"] == 1
    else:
        # No terrain hit: the frame's own depth, low confidence or not, with the range error that implies (below).
        # Deleting these lost 62 % of the Labrador clip's detections, nearly all the far lot.
        L["enu"], L["ratio"], L["ranged"], L["tier"] = pe, 1.0, False, -1
if args.cache and cached_ranging is None:
    pickle.dump({key: {x: L[x] for x in ("enu", "ratio", "ranged", "tier")} for key, L in lifted.items()}, open(RANGING_CACHE, "wb"), protocol=4)
print(f"terrain {gw}x{gh} cells of {args.cell} m + far tier {fw}x{fh} of {args.far_cell} m; {ranged} of {len(lifted)} detections "
      f"ranged on it ({ranged_far} on the far tier), {len(lifted) - ranged} placed at their own depth "
      f"({sum(L['lowconf'] and not L['ranged'] for L in lifted.values())} of them low pointmap confidence)")

# ---- 3. causal tracks ------------------------------------------------------------------------------
class KF:
    """Constant velocity on the ground (e, n), one per object; the height is the terrain's. Each measurement brings
    its own noise R (meas_cov): an oblique camera knows an object's bearing far better than its range."""
    def __init__(self, p, accel, sigma, R=None):
        self.x = np.r_[p[:2], 0, 0].astype(float)
        self.P = np.diag([sigma ** 2, sigma ** 2, 16, 16])
        if R is not None:
            self.P[:2, :2] = R
        self.q, self.r = accel ** 2, sigma ** 2

    def predict(self, dt):
        F = np.eye(4); F[:2, 2:] = dt * np.eye(2)
        G = np.r_[0.5 * dt * dt, 0.5 * dt * dt, dt, dt]
        self.x = F @ self.x
        self.P = F @ self.P @ F.T + np.diag(G * G) * self.q

    def update(self, z, gate=13.8, R=None):
        Hm = np.hstack([np.eye(2), np.zeros((2, 2))])
        S = Hm @ self.P @ Hm.T + (self.r * np.eye(2) if R is None else R)
        y = z[:2] - Hm @ self.x
        m2 = float(y @ np.linalg.solve(S, y))
        if m2 > gate:
            return False
        K = self.P @ Hm.T @ np.linalg.inv(S)
        self.x = self.x + K @ y
        self.P = (np.eye(4) - K @ Hm) @ self.P
        return True


# The camera's focal length (source px), from the first frame's pointmap (constant on a calibrated SLAM run).
_X0 = np.load(slam / "frames" / f"f_{min(poses):05d}.npz")["X"].astype(np.float64)
_uu = np.arange(W)[None, :] - W / 2
_ok0 = (np.abs(_uu) > 40) & (_X0[..., 2] > 1e-3) & (np.abs(_X0[..., 0]) > 1e-6)
F_SRC = float(np.median((_uu * _X0[..., 2] / np.where(_ok0, _X0[..., 0], np.nan))[_ok0])) * SW
# Ranging error of a ground height (m): the confident fine map, the far tier.
SIGMA_H = {0: 0.15, 1: 1.0}


def ray_terms(L, i, d):
    """(across, along, unit horizontal ray): 1-sigma errors (m) of a measurement across the line of sight (the box
    centre's pixel error at that range) and along it (a terrain height error stretched by the ray's shallow angle,
    or a bare depth's ~8 %), capped at 30 % of the range."""
    v = L["enu"] - cam_enu[i]
    r = float(np.linalg.norm(v)); hd = float(np.linalg.norm(v[:2]))
    ray = v[:2] / max(hd, 1e-6)
    tan_el = max(np.tan(np.radians(2.0)), -v[2] / max(hd, 1e-6))
    x1, y1, x2, y2 = d["box"]
    lat = r * max(1.5, 0.1 * max(x2 - x1, y2 - y1)) / F_SRC
    along = SIGMA_H[L["tier"]] / tan_el if L["ranged"] else 0.08 * r
    return lat, max(lat, min(along, 0.3 * r)), ray


def meas_cov(L, i, d, floor):
    lat, along, ray = ray_terms(L, i, d)
    lat = max(lat, floor)
    along = max(along, lat)
    return lat ** 2 * np.eye(2) + (along ** 2 - lat ** 2) * np.outer(ray, ray)


def meas_cov3(L, i, d, floor):
    """meas_cov in 3D: a ranging error slides the measured point along its own ray, down as well as out (a terrain
    height error at a shallow angle is metres of range and that ray's drop over them), so the along-ray term lies on
    the 3D ray, its horizontal part the 2D model's."""
    lat, along, _ = ray_terms(L, i, d)
    lat = max(lat, floor)
    v = L["enu"] - cam_enu[i]
    r3 = v / max(float(np.linalg.norm(v)), 1e-6)
    along3 = max(along / max(float(np.hypot(r3[0], r3[1])), 0.05), lat)
    return lat ** 2 * np.eye(3) + (along3 ** 2 - lat ** 2) * np.outer(r3, r3)


# False dismounts. At the detector's 0.1 floor a VisDrone model calls roof vents, chimneys, AC units and poles "pedestrian";
# on the Labrador clip that was ~37 dismount contacts per frame against ~1.5 people in the eye-audit inventory. A person
# track is reported only when what the reconstruction measured of it is what a person can be, judged causally on the
# track's history so far (the edge cannot see the rest of the clip). The track itself keeps being followed and starts
# reporting the moment its evidence is enough:
#  - evidence: the summed confidence of its person detections. A pedestrian stays in view for seconds and is detected at
#    every frame; a vent flickers in and out of the detector's floor, and every track it opens leaves a "lost" contact at
#    the receiver for t_depart. PERSON_MIN_CONF_SUM = about 1 s of frames at the ~0.3 a far person scores.
#  - silhouette: a standing adult is 1.75 m tall and ~0.5 m across, so its box is taller than wide (the real people of
#    the Labrador inventory, 250-460 m out, measure 2.1-2.6 in the detector's tight boxes; vents, chimneys and AC units
#    1.0-1.5). The height a camera looking down sees shrinks with cos(elevation), so the aspect is corrected for it, and
#    the track's median over its detections so far must reach PERSON_MIN_ASPECT.
#  - ground: people stand on the ground, not on a roof. Where the confident fine map surrounds the object (PERCH_COVER
#    of the disc around it is mapped), its surface may not stand more than PERSON_MAX_PERCH_M above the low ground
#    (PERSON_PERCH_PCT-th percentile) within PERSON_PERCH_R_M: a 20 % slope over that radius is 3 m, a house's roof
#    ridge 5-9 m. The far tier is not asked (its heights are good to ~1 m, and the ground rises towards the horizon).
# A verdict that turns against a reporting track takes PERSON_HOLD_S to count, so a borderline track does not flicker.
# A rider (a person on a two-wheeler) is its vehicle's class, not a person, and is not asked.
PERSON_MIN_CONF_SUM = 9.0
PERSON_MIN_ASPECT = 1.6
PERSON_MAX_PERCH_M, PERSON_PERCH_R_M, PERSON_PERCH_PCT, PERCH_COVER = 3.0, 15.0, 10, 0.8
PERSON_HOLD_S = 2.0
# False vehicles near the camera. Close up a car is a big, clear box and the detector is sure of it (every audited
# vehicle whose box was >= 60 px had a median confidence >= 0.78 on the Labrador clip); the big boxes it is unsure of
# are roofs, sheds, walls and bushes (tracks it found 60-200 px wide at a median confidence of 0.16-0.32). The same
# within VEH_NEAR_M of the camera, where a road vehicle (a two-wheeler too) is never small: the seven vehicle tracks
# there under 0.45 were, on their crops, blurred patches, a bush, rubble and a roof corner (the eye audit has only one
# vehicle that close, so this part is judged on the crops). A vehicle track that has not moved, whose median box is
# VEH_NEAR_PX or more or whose median range is under VEH_NEAR_M, and whose median confidence stays under
# VEH_NEAR_CONF, is not reported. Far vehicles are small and legitimately uncertain, and are not asked.
VEH_NEAR_PX, VEH_NEAR_M, VEH_NEAR_CONF = 60.0, 100.0, 0.45
_r_cells = int(round(PERSON_PERCH_R_M / args.cell))
_gg = np.arange(-_r_cells, _r_cells + 1)
PERCH_DISC = (_gg[:, None] ** 2 + _gg[None, :] ** 2) <= _r_cells ** 2


def height_above_surroundings(e, n):
    """How far the confident fine map's surface at (e, n) stands above the low ground around it (m); None where the fine
    map does not surround the object (no surface there, or too little of the disc around it mapped)."""
    i = int(np.clip((e - g_lo[0]) / args.cell, 0, gw - 1)); j = int(np.clip((n - g_lo[1]) / args.cell, 0, gh - 1))
    if not np.isfinite(hf[j, i]):
        return None
    win = np.full(PERCH_DISC.shape, np.nan)
    j0, j1, i0, i1 = max(j - _r_cells, 0), min(j + _r_cells + 1, gh), max(i - _r_cells, 0), min(i + _r_cells + 1, gw)
    win[j0 - (j - _r_cells):j1 - (j - _r_cells), i0 - (i - _r_cells):i1 - (i - _r_cells)] = hf[j0:j1, i0:i1]
    near = win[PERCH_DISC]
    near = near[np.isfinite(near)]
    if len(near) < PERCH_COVER * PERCH_DISC.sum():
        return None
    return float(hf[j, i] - np.percentile(near, PERSON_PERCH_PCT))


def silhouette(d, L, i):
    """A detection's box height over width, as a person standing at that range would show it at zero elevation: the
    height a camera looking down at elevation el sees shrinks by cos(el) (not below 0.5: from straight above there is
    no silhouette to speak of)."""
    x1, y1, x2, y2 = d["box"]
    v = L["enu"] - cam_enu[i]
    el = np.arctan2(-v[2], max(float(np.hypot(v[0], v[1])), 1e-6))
    return (y2 - y1) / max(x2 - x1, 1.0) / max(np.cos(el), 0.5)


def person_veto(s, x):
    """Why a person track fails now (None: it passes), from its history so far."""
    if s["cls"][0] < PERSON_MIN_CONF_SUM:
        return "evidence"
    if np.median(s["asp"]) < PERSON_MIN_ASPECT:
        return "silhouette"
    perch = height_above_surroundings(x[0], x[1])
    if perch is not None and perch > PERSON_MAX_PERCH_M:
        return "ground"
    return None


def new_track(p, cl, t, tid2, R=None):
    veh = COARSE[cl] == 1
    return {"kf": KF(p, 2.0 if veh else 0.8, 0.35 if veh else 0.25, R), "last_t": t, "first_t": t, "hits": 0, "misses": 0,
            "coarse": COARSE[cl], "cls": defaultdict(float), "live2d": tid2,
            "win": [], "swin": [], "Is": np.zeros((2, 2)), "bs": np.zeros(2), "Is3": np.zeros((3, 3)), "bs3": np.zeros(3), "moving": False, "flip_since": None,
            "asp": [], "vbox": [], "person_ok": False, "person_bad_since": None, "want3": None, "last": None, "p3": p}


def knob(name, default):
    """A tuning constant, overridable from the environment for sweeps (MOTION_CHI2=50 lift.py ...)."""
    return float(os.environ.get(name, default))


# Moving or not is a test, not a filter's velocity: far objects are uncertain by metres along the line of sight, which
# a constant-velocity filter turns into ghost speeds (37 % of the Labrador clip's parked cars reached the receiver as
# moving). Each track keeps a static estimate (the information-weighted mean of its measurements since it last moved);
# it moves when a test says so for MOTION_HOLD_S and stops when the test fails for as long. A static track reports its
# static estimate and no velocity; a moving one the filter's state. Two tests:
#  - In the image (flow_tests), where an oblique camera is precise: the drone's own motion moves every pixel, but a
#    parked car moves with its neighbours. A track moves when, over the last 0.5 s or 1 s, its box centre has shifted
#    against the median shift of its nearest still neighbours by more than FLOW_PX (the box centre's jitter is 1-2 px,
#    the parked cars' residual is below 1 px at the median and 6 px at the 99th percentile) and by more than the
#    MOTION_MPS that shift is worth at the track's range. Independent of the ranging, which is what ghosts are made of.
#  - In 3D (motion_test), when a track has too few still neighbours (a lone car on a road, a convoy): a weighted
#    straight-line fit over MOTION_WIN_S whose velocity is significant (chi2 > MOTION_CHI2 on 2 dof, every
#    measurement's ray-aligned noise) and faster than MOTION_MPS. The noise model is optimistic, the errors of one
#    object over 2 s being correlated (the same terrain cell under it, the same pose drift) and not the ~60
#    independent looks the fit counts: a parked car's chi2 passes 20 one time in five while a mover's is in the
#    hundreds to thousands, so the threshold sits where the parked cars' tail ends.
MOTION_WIN_S, MOTION_CHI2, MOTION_MPS, MOTION_HOLD_S, MOTION_MIN_S = (
    knob("MOTION_WIN_S", 2.0), knob("MOTION_CHI2", 100.0), knob("MOTION_MPS", 0.7), knob("MOTION_HOLD_S", 0.5), knob("MOTION_MIN_S", 0.5))
# (window s, box shift px at a 1920 px wide frame) of the image test; a track moves if any window passes. The nearest
# FLOW_K still neighbours within FLOW_R px give the shift of the ground, at least FLOW_MIN of them.
FLOW_WINS = ((0.5, knob("FLOW_PX_SHORT", 5.0)), (1.0, knob("FLOW_PX_LONG", 8.0)))
FLOW_K, FLOW_R, FLOW_MIN = 7, 250.0, 3
# The static estimate averages the last STATIC_WIN_S only: ~150 looks for a parked car, and a slow mover the test has
# not caught yet lags by at most half of it.
STATIC_WIN_S = knob("STATIC_WIN_S", 5.0)


def motion_test(win):
    """Weighted line fit p(t) = a + v (t - tm) over [(t, z, Rinv)]: (v, chi2 of v), or None when too short. The chi2 is
    divided by the fit's own reduced chi2 where that exceeds 1: a step in the window (a ranging jump) is no velocity."""
    if len(win) < 8 or win[-1][0] - win[0][0] < MOTION_MIN_S:
        return None
    tm = np.mean([w[0] for w in win])
    A = np.zeros((4, 4)); b = np.zeros(4)
    for t_, z, Ri, *_ in win:
        J = np.hstack([np.eye(2), (t_ - tm) * np.eye(2)])
        A += J.T @ Ri @ J; b += J.T @ Ri @ z
    try:
        cov = np.linalg.inv(A)
    except np.linalg.LinAlgError:
        return None
    x = cov @ b
    v = x[2:]
    chi2 = float(v @ np.linalg.solve(cov[2:, 2:], v))
    res = sum(float((z - x[:2] - (t_ - tm) * v) @ Ri @ (z - x[:2] - (t_ - tm) * v)) for t_, z, Ri, *_ in win)
    return v, chi2 / max(1.0, res / (2 * len(win) - 4))


fbox = {}   # frame -> {track id: box centre (source px)}, the last second or two


def flow_tests(i, tids):
    """{track id: True/False/None} for the tracks seen in frame i: the image test above (None: too few still neighbours,
    or the track is too young for the window)."""
    out = dict.fromkeys(tids)
    cur = fbox[i]
    px = meta["src_w"] / 1920.0
    for wsec, thr in FLOW_WINS:
        wf = max(1, round(wsec * args.fps))
        todo = {t for t in tids if t in cur}
        for dj in range(4):          # the nearest frame with data, a miss or two of the detector allowed
            j = i - wf - dj
            if j not in fbox or not todo:
                continue
            ids = [t for t in fbox[j] if t in cur]
            if len(ids) < FLOW_MIN + 1:
                continue
            Xi = np.array([cur[t] for t in ids]); Xj = np.array([fbox[j][t] for t in ids])
            D = Xi - Xj
            still = np.array([t in tracks and not tracks[t]["moving"] for t in ids])
            for n, t in enumerate(ids):
                if t not in todo:
                    continue
                dist = np.hypot(*(Xi - Xi[n]).T)
                dist[n] = np.inf
                dist[~still | (dist > FLOW_R * px)] = np.inf
                nb = np.argsort(dist)[:FLOW_K]
                nb = nb[np.isfinite(dist[nb])]
                if len(nb) < FLOW_MIN:
                    continue
                shift = float(np.hypot(*(D[n] - np.median(D[nb], 0))))
                rng = float(np.linalg.norm(tracks[t]["p3"] - cam_enu[i]))
                mps = shift * rng / (F_SRC * (i - j) / args.fps)
                out[t] = bool(out[t]) or (shift > thr * px and mps > MOTION_MPS)
                todo.discard(t)
    return out


def to_image(P, i):
    """ENU point -> source pixel (u, v) in frame i and its depth (m); the frame's own camera."""
    v = (B @ Rs[i]).T @ (np.asarray(P, float) - cam_enu[i])
    z = max(float(v[2]), 1e-3)
    return np.array([F_SRC * v[0] / z + meta["src_w"] / 2, F_SRC * v[1] / z + meta["src_h"] / 2]), float(v[2])


def static_refit(s):
    """The static estimate's information sums over its window, in 2D (motion test, re-identification) and in 3D (what a
    still object reports). Every measurement lies on its own ray, at the terrain's height where that ray meets it, so
    their 3D mean with ray-aligned weights stays on the rays and reprojects onto the object's pixels. The ground's
    height under the 2D mean does not: on the Labrador clip a far parked car's mean range lands on another terrain cell
    (a roof edge, the 4 m far tier) metres above or below, and it projected a median 7 px (p90 20-280 px, by range) off
    its box vertically, while across the line of sight it was off by 2 px."""
    s["Is"] = sum(w[2] for w in s["swin"]); s["bs"] = sum(w[2] @ w[1] for w in s["swin"])
    s["Is3"] = sum(w[4] for w in s["swin"]); s["bs3"] = sum(w[4] @ w[3] for w in s["swin"])


def static_est(s):
    """A track's static estimate: (position, covariance), the information-weighted mean of its last STATIC_WIN_S."""
    P = np.linalg.inv(s["Is"])
    return P @ s["bs"], P


def box_iou(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0])); iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    i_ = ix * iy
    return i_ / max(1e-6, (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - i_)


# Re-identification is decided in the image, where an oblique camera is precise: a far car's position is uncertain
# by metres along the line of sight but by a pixel or two across it, and an error along the line of sight barely
# moves its projection. A new 2D id is a lost track when the track's prediction lands in the new box (grown 30 %,
# >= 4 px) and is not implausible in 3D; a parked object may have been hidden for REID_STATIC_S, a mover REID_MOVING_S.
# "Parked" is the motion test's verdict, not the filter's velocity (far objects carry ghost speeds of a few m/s, which
# made a parked car a mover for 1.5 s only), and a parked object is predicted where it stood: its static estimate
# (information-weighted, so its covariance is the averaged noise, not the filter's), compared with the new
# measurement's own ray-aligned noise plus REID_SLACK_M of drift (a car pulling out a metre is still the car).
REID_STATIC_S, REID_MOVING_S = 30.0, 1.5
REID_CHI2, REID_SLACK_M = 25.0, 0.5
# Two live tracks whose boxes overlap this much for 5 frames are one object (the detector's tiles and full-frame
# pass, or ByteTrack, opened a second id on it).
SAME_BOX_IOU = 0.7
# A new track is tentative. The detector runs at a 0.1 floor, and a third of the tracks reported on the Labrador clip
# live under a second: a flicker (a roof, a hedge, a shadow, a car seen for half a second) that nothing re-identifies
# later. Once reported, the edge confirms it in 3 looks and the receiver keeps its lost contact for t_depart (60 s) next
# to the real cars, and a flicker beside a parked car drags it into a group. A real object keeps being detected, so it
# earns its report: a track is reported after REPORT_AFTER_S, or at once when the detector is sure of it
# (REPORT_FAST_CONF). A track re-identified into an older one keeps the older one's age.
REPORT_AFTER_S, REPORT_FAST_CONF = 0.7, 0.5
# Two tracks that sit in the same place in 3D are one object only if their boxes also overlap: far objects are ranged
# on the far terrain tier's 4 m cells, so different cars of a lot (and a mover passing a parked car on the same ray)
# land on the very same coordinates, and without the image test they were fused into one track (40 tracks of the
# Labrador clip held disjoint boxes in the same frame, 12.8k times), a moving car included.
SAME_OBJECT_IOU = 0.3
tracks = {}      # track id -> state
alias = {}       # 2D id -> track id
next_id = 1
rows = []
obs = defaultdict(list)  # track id -> [(frame, det index)]
dt = 1.0 / args.fps
merges = rejects = dups = riders_fast = split = 0
vetoed = defaultdict(set)   # why a person track was held back -> track ids
near = defaultdict(int)  # (older, younger) -> consecutive frames closer than one object's size
# Closer than this, two tracks of a coarse class are one object (centre to centre, m).
SAME_OBJECT_M = {0: 0.35, 1: 1.2}
# A dismount sustaining this speed is riding something the detector missed (m/s).
RIDER_MPS = 3.5
for f in dets:
    i = f["i"]
    t = i * dt
    seen = set()
    boxes_now = {}
    pending = []
    live2d = {x["id"] for x in f["dets"]}
    for k, d in enumerate(f["dets"]):
        L = lifted.get((i, k))
        if L is None:
            continue
        p = L["enu"]
        cl = d["cls"]
        tid2 = d["id"]
        if tid2 not in alias:
            # A new 2D id: the same object as a recently lost track of its coarse class? Judged by where the track's
            # prediction lands in this frame (above), with a loose 3D gate against a track far along the same ray.
            cand, best_d = None, 1e9
            x1, y1, x2, y2 = d["box"]
            grow = max(4.0, 0.3 * max(x2 - x1, y2 - y1))
            Rm0 = meas_cov(L, i, d, 0.35 if COARSE[cl] == 1 else 0.25)
            for tid, s in tracks.items():
                gap = t - s["last_t"]
                moving = s["moving"]
                if s["coarse"] != COARSE[cl] or gap <= 0 or gap > (REID_MOVING_S if moving else REID_STATIC_S) or tid in seen or s["live2d"] in live2d:
                    continue
                if moving:
                    pred = s["kf"].x[:2] + s["kf"].x[2:] * gap
                    cov = s["kf"].P[:2, :2] + (gap * 1.0) ** 2 * np.eye(2)
                else:
                    pred, cov = static_est(s)
                    cov = cov + REID_SLACK_M ** 2 * np.eye(2)
                uv, z = to_image(np.r_[pred, p[2]], i)
                if z <= 0 or not (x1 - grow <= uv[0] <= x2 + grow and y1 - grow <= uv[1] <= y2 + grow):
                    continue
                dd = pred - p[:2]
                if float(dd @ np.linalg.solve(cov + Rm0, dd)) > REID_CHI2:
                    continue
                dist = float(np.hypot(uv[0] - (x1 + x2) / 2, uv[1] - (y1 + y2) / 2))
                if dist < best_d:
                    cand, best_d = tid, dist
            if cand is not None:
                alias[tid2] = cand
                merges += 1
            else:
                alias[tid2] = next_id
                tracks[next_id] = new_track(p, cl, t, tid2, meas_cov(L, i, d, 0.35 if COARSE[cl] == 1 else 0.25))
                next_id += 1
        tid = alias[tid2]
        if tid in seen and box_iou(boxes_now[tid], d["box"]) < SAME_OBJECT_IOU:
            # The track already took a detection in this frame, somewhere else in the image: this 2D id is another
            # object (a re-identification or a merge fused two), not a second look at the same one.
            alias[tid2] = tid = next_id
            tracks[tid] = new_track(p, cl, t, tid2, meas_cov(L, i, d, 0.35 if COARSE[cl] == 1 else 0.25))
            next_id += 1
            split += 1
        s = tracks[tid]
        s["live2d"] = tid2
        accepted = False
        Rm = meas_cov(L, i, d, np.sqrt(s["kf"].r))
        if s["hits"] > 0:
            s["kf"].predict(t - s["last_t"])
            if not s["kf"].update(p, R=Rm):
                rejects += 1
                s["misses"] += 1
                if s["misses"] >= 4:   # the filter lost it: restart at the measurements
                    s["kf"] = new_track(p, cl, t, tid2, Rm)["kf"]
                    s["misses"] = 0
                    # Positions from before the jump are not part of the static estimate: it starts over here.
                    s["swin"], accepted = [], True
            else:
                s["misses"] = 0
                accepted = True
        else:
            accepted = True
        if s["hits"] == 0 or accepted:
            Ri = np.linalg.inv(Rm)
            entry = (t, p[:2].copy(), Ri, p.copy(), np.linalg.inv(meas_cov3(L, i, d, np.sqrt(s["kf"].r))))
            s["win"] = [w for w in s["win"] if t - w[0] <= MOTION_WIN_S] + [entry]
            s["swin"] = [w for w in s["swin"] if t - w[0] <= STATIC_WIN_S] + [entry]
            static_refit(s)
            mt = motion_test(s["win"])
            s["want3"] = None if mt is None else bool(mt[1] > MOTION_CHI2 and np.hypot(*mt[0]) > MOTION_MPS)
            s["last"] = entry
        s["p3"] = p
        s["last_t"] = t
        s["hits"] += 1
        s["cls"][cl] += d["conf"]
        if cl == 0:
            s["asp"].append(silhouette(d, L, i))
        else:
            x1, y1, x2, y2 = d["box"]
            s["vbox"].append((max(x2 - x1, y2 - y1), d["conf"], float(np.linalg.norm(L["enu"] - cam_enu[i]))))
        if s["coarse"] == 0 and s["hits"] >= 10 and np.hypot(*s["kf"].x[2:]) > RIDER_MPS:
            s["cls"][3] += 2 * d["conf"]
            if not s.get("rider"):
                s["rider"] = True; riders_fast += 1
        seen.add(tid)
        boxes_now[tid] = d["box"]
        obs[tid].append((i, k))
        if s["hits"] >= args.min_hits and (t - s["first_t"] >= REPORT_AFTER_S or d["conf"] >= REPORT_FAST_CONF):
            pending.append((tid, i, d, L, p))

    # The motion decision of every track seen in this frame (the image test needs all of them).
    fbox[i] = {}
    for tid, bx in boxes_now.items():
        fbox[i][tid] = ((bx[0] + bx[2]) / 2, (bx[1] + bx[3]) / 2)
    for j in [j for j in fbox if j < i - 2 * round(max(w for w, _ in FLOW_WINS) * args.fps)]:
        del fbox[j]
    flows = flow_tests(i, seen)
    for tid in seen:
        s = tracks[tid]
        want = flows[tid] if flows[tid] is not None else s["want3"]
        s["want3"] = None
        if want is not None:
            if want != s["moving"]:
                s["flip_since"] = s["flip_since"] if s["flip_since"] is not None else t
                if t - s["flip_since"] >= MOTION_HOLD_S:
                    s["moving"], s["flip_since"] = want, None
                    if not want:   # stopped: the static estimate starts over from the motion window
                        s["swin"] = list(s["win"])
                        static_refit(s)
            else:
                s["flip_since"] = None
        if s["moving"] and s["last"] is not None and s["last"][0] == t:   # the static estimate follows a mover from where it is
            s["swin"] = [s["last"]]
            static_refit(s)
    for tid, i_, d, L, p in pending:
        s = tracks[tid]
        x1, y1, x2, y2 = d["box"]
        bb = [(x1 + x2) / 2 / meta["src_w"], (y1 + y2) / 2 / meta["src_h"], (x2 - x1) / meta["src_w"], (y2 - y1) / meta["src_h"]]
        cls = max(s["cls"], key=s["cls"].get)
        if s["moving"]:
            # A mover's filtered position lags and smooths along the line of sight, where the terrain under it may be
            # metres higher or lower (20-77 px of vertical error at 380 m, enough to lose the match to a parked
            # neighbour): its height is the one of the ray through its box.
            x, Pp = s["kf"].x, s["kf"].P[:2, :2]
            u = float(p[2])
        else:
            # A still object reports its 3D static estimate, height included (static_refit).
            Pp = np.linalg.inv(s["Is"])
            x3 = np.linalg.solve(s["Is3"], s["bs3"])
            x, u = np.r_[x3[:2], 0.0, 0.0], float(x3[2])
        if cls == 0:
            why = person_veto(s, x)
            if why is None:
                s["person_ok"], s["person_bad_since"] = True, None
            else:
                s["person_bad_since"] = s["person_bad_since"] if s["person_bad_since"] is not None else t
                if not s["person_ok"] or t - s["person_bad_since"] >= PERSON_HOLD_S:
                    s["person_ok"] = False
                    vetoed[why].add(tid)
                    continue
        elif not s["moving"] and s["vbox"] and np.median([b[1] for b in s["vbox"]]) < VEH_NEAR_CONF \
                and (np.median([b[0] for b in s["vbox"]]) >= VEH_NEAR_PX or np.median([b[2] for b in s["vbox"]]) < VEH_NEAR_M):
            vetoed["near vehicle"].add(tid)
            continue
        # Error radius (1 sigma) in the reconstruction's frame: the estimate's spread plus the ranging error. The
        # estimate averages frame noise away but not a terrain height's bias (the same cells under a parked car),
        # so the ranging term of this sighting's tier stays (ray_terms).
        ce_m = float(np.hypot(np.sqrt(0.5 * (Pp[0, 0] + Pp[1, 1])), ray_terms(L, i_, d)[1] if L["ranged"] else 0.0))
        if not L["ranged"]:
            ce_m = max(ce_m, 3.0)
        rows.append((round(i_ * TICK_HZ / args.fps), tid, cls, x[0], x[1], u, x[2], x[3], 0.0, min(255, round(255 * d["conf"])), *bb, ce_m))

    # One object tracked twice (ByteTrack opened a new id while the old one lived): the younger
    # track joins the older one once they have stayed closer than one object for 5 frames.
    ids = sorted(seen)
    for a_i, a in enumerate(ids):
        for b in ids[a_i + 1:]:
            sa, sb = tracks[a], tracks[b]
            if sa["coarse"] != sb["coarse"]:
                continue
            iou_ab = box_iou(boxes_now[a], boxes_now[b])
            if (np.linalg.norm(sa["kf"].x[:2] - sb["kf"].x[:2]) < SAME_OBJECT_M[sa["coarse"]] and iou_ab >= SAME_OBJECT_IOU) or iou_ab >= SAME_BOX_IOU:
                near[(a, b)] += 1
            else:
                near.pop((a, b), None)
    for (a, b), cnt in list(near.items()):
        if cnt >= 5 and a in tracks and b in tracks:
            old, young = (a, b) if tracks[a]["first_t"] <= tracks[b]["first_t"] else (b, a)
            for k2, v in alias.items():
                if v == young:
                    alias[k2] = old
            for c2, w in tracks[young]["cls"].items():
                tracks[old]["cls"][c2] += w
            tracks[old]["asp"] += tracks[young]["asp"]
            del tracks[young]
            near.pop((a, b))
            dups += 1

rows = list({(r[0], r[1]): r for r in rows}.values())   # one row per track and tick (a merged duplicate's last)
reported = {r[1] for r in rows}

# ---- 3b. yaw: where each track points, degrees true (0 = north, 90 = east, ENU), NaN where unknown ---------------
# A moving vehicle points along its course (the smoothed velocity, speed > YAW_MOVING; it keeps its last course
# while stopped). A vehicle that has not moved points along the long axis of its footprint, measured in the
# reconstruction: PCA of the ground-projected 3D points of the object's pixels (mask, or the box's central
# window) in the views where it stood still, the axes averaged (doubled angle, weighted by how elongated each
# footprint is), once per track, so the yaw of a parked car is one number for all its rows. The axis has two
# directions: the one nearest the track's first course if it ever moves, else the one in [0, 180). People: NaN.
# Only views at >= --yaw-min-elev count: seen at a grazing angle (the Labrador clip: 5-9 deg for 70 % of the
# views) the points of a small object smear along the line of sight and their long axis IS the camera bearing
# (80-90 % of views within 15 deg of it, also on moving cars whose course is 40+ deg off it), not the car's.
# A static vehicle never seen steeply enough has NaN.
YAW_MOVING, YAW_STOPPED = 1.0, 0.6    # m/s: the smoothed speed above which a track moves / below which it stopped
YAW_SMOOTH_S = 0.5                     # smoothing time constant of the velocity the course is taken from
YAW_SUSTAIN_S = 1.0                    # a track moves once it has been faster than YAW_MOVING this long
YAW_MIN_WEIGHT = 2.0                   # sum of footprint elongations (views x (l1 - l2) / (l1 + l2)) a static axis needs
YAW_MIN_CONCENTRATION = 0.5            # and the views' axes must agree: |mean unit doubled-angle vector|


def footprint_axis(pts):
    """One view: (bearing of the footprint's long axis, 0-180 deg; elongation 0-1) of ground-projected 3D points."""
    q = enu(pts)[:, :2]
    if len(q) < 8:
        return None
    ev, evec = np.linalg.eigh(np.cov((q - q.mean(0)).T))
    return np.degrees(np.arctan2(evec[0, 1], evec[1, 1])) % 180, float((ev[1] - ev[0]) / max(ev[1] + ev[0], 1e-12))


def static_axis(tid, before):
    """The track's footprint axis (0-180 deg) from its views before frame `before`, None when too weak or disagreeing."""
    vx = vy = wsum = 0.0
    for (i, k) in obs[tid]:
        L = lifted[(i, k)]
        if i >= before or L["lowconf"]:
            continue
        dh = np.linalg.norm(L["enu"][:2] - cam_enu[i][:2])
        if np.degrees(np.arctan2(cam_enu[i][2] - L["enu"][2], max(dh, 1e-6))) < args.yaw_min_elev:
            continue
        a = footprint_axis(L["pts"])
        if a is not None:
            vx += a[1] * np.cos(np.radians(2 * a[0])); vy += a[1] * np.sin(np.radians(2 * a[0])); wsum += a[1]
    if wsum < YAW_MIN_WEIGHT or np.hypot(vx, vy) / wsum < YAW_MIN_CONCENTRATION:
        return None
    return float(np.degrees(np.arctan2(vy, vx)) / 2 % 180)


def axis_dir(axis, course):
    """The direction (0-360) of a 0-180 axis nearest `course` (the axis itself when there is none)."""
    if course is None:
        return axis
    d = (course - axis + 180) % 360 - 180
    return axis if abs(d) <= 90 else axis + 180


track_rows = defaultdict(list)
for n, r in enumerate(rows):
    track_rows[r[1]].append(n)
yaws = [float("nan")] * len(rows)
yaw_kind = defaultdict(int)   # tracks by how their yaw was found
for tid, idx in track_rows.items():
    vs, moving, course, last_t, courses, first_move, above = None, False, None, None, [], None, None
    state = []
    for n in idx:
        r = rows[n]
        v = np.array([r[6], r[7]])
        a = 1.0 if last_t is None else (r[0] - last_t) / TICK_HZ / (YAW_SMOOTH_S + (r[0] - last_t) / TICK_HZ)
        vs = v if vs is None else vs + a * (v - vs)
        last_t = r[0]
        sp = float(np.hypot(*vs))
        if sp > YAW_MOVING:
            above = above or (r[0], r[3], r[4])     # a run above the speed: since when, from where
        else:
            above = None
        # Moving: above the speed for YAW_SUSTAIN_S and having really gone somewhere (a filter's velocity noise does
        # not move a parked car), then until it drops below YAW_STOPPED.
        if not moving and above and (r[0] - above[0]) / TICK_HZ >= YAW_SUSTAIN_S and np.hypot(r[3] - above[1], r[4] - above[2]) >= YAW_SUSTAIN_S * YAW_MOVING:
            moving = True
            first_move = round(above[0] * args.fps / TICK_HZ)
        elif moving and sp < YAW_STOPPED:
            moving = False
        if moving:
            course = float(np.degrees(np.arctan2(vs[0], vs[1])) % 360)
            if len(courses) < 10:
                courses.append(course)
        state.append(course)
    c0 = None
    if courses:
        c = np.radians(courses)
        c0 = float(np.degrees(np.arctan2(np.sin(c).mean(), np.cos(c).mean())) % 360)
    axis = static_axis(tid, first_move if first_move is not None else 1 << 30) if any(c is None for c in state) else None
    still = c0 if axis is None else axis_dir(axis, c0)
    yaw_kind["person" if rows[idx[-1]][2] == 0 else "moved" if c0 is not None else "static, axis measured" if axis is not None else "static, unknown"] += 1
    for n, c in zip(idx, state):
        y = c if c is not None else still if still is not None else float("nan")
        yaws[n] = float("nan") if rows[n][2] == 0 else y % 360
yaw_of = {(r[0], r[1]): y for r, y in zip(rows, yaws)}
first_yaw = {tid: yaws[idx[0]] for tid, idx in track_rows.items()}
with open(out / "tracks3d.csv", "w") as fh:
    fh.write("tick,id,class,e,n,u,ve,vn,vu,conf,bu,bv,bw,bh,ce,yaw\n")
    for r, y in zip(rows, yaws):
        fh.write(f"{r[0]},{r[1]},{r[2]}," + ",".join(f"{v:.3f}" for v in r[3:9]) + f",{r[9]}," + ",".join(f"{v:.4f}" for v in r[10:14]) + f",{r[14]:.2f}," + ("nan" if np.isnan(y) else f"{y:.1f}") + "\n")
print(f"yaw: {sum(not np.isnan(y) for y in yaws)} of {len(yaws)} rows; tracks by source {dict(yaw_kind)}")
print(f"tracks: {next_id - 1} ({merges} 2D id switches re-joined in 3D, {split} 2D ids split off a track that held another object, {dups} duplicates merged, {riders_fast} fast dismounts relabelled riders, "
      f"{rejects} gated-out measurements), reported {len(reported)}, rows {len(rows)}")
print("tracks held back at least once, by the first test they failed: "
      + ", ".join(f"{why} {len(ids)}" for why, ids in sorted(vetoed.items())) + f"; never reported: {len(set().union(*vetoed.values()) - reported)}")

# ---- 4. appearance chips ---------------------------------------------------------------------------
row_at = {(r[0], r[1]): r for r in rows}
pick = {}
for tid in reported:
    first = obs[tid][0][0]
    best_score, best_k = -1, None
    for (i, k) in obs[tid]:
        if i - first > 2 * args.fps:
            break
        d = det_by_frame[i][k]
        x1, y1, x2, y2 = d["box"]
        edge = x1 < 6 or y1 < 6 or x2 > meta["src_w"] - 6 or y2 > meta["src_h"] - 6
        sc = (x2 - x1) * (y2 - y1) * d["conf"] * (0.2 if edge else 1.0) * (1.0 if lifted[(i, k)]["ranged"] else 0.5)
        if sc > best_score:
            best_score, best_k = sc, (i, k)
    pick[tid] = best_k
need = {ik[0] for ik in pick.values()}
full = {}
cap = cv2.VideoCapture(str(run / "clip.mp4"))
for i in range(meta["frames"]):
    ok, img = cap.read()
    if not ok:
        break
    if i in need:
        full[i] = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


def bilinear(A, x, y):
    x0 = np.clip(np.floor(x).astype(int), 0, W - 2); y0 = np.clip(np.floor(y).astype(int), 0, H - 2)
    fx = np.clip(x - x0, 0, 1)[..., None]; fy = np.clip(y - y0, 0, 1)[..., None]
    A = A.reshape(H, W, -1)
    return ((A[y0, x0] * (1 - fx) + A[y0, x0 + 1] * fx) * (1 - fy) + (A[y0 + 1, x0] * (1 - fx) + A[y0 + 1, x0 + 1] * fx) * fy)


# Class size priors (length, width, height in m): the measured footprint is clamped to them.
DIMS = {0: ((0.4, 0.9), (0.4, 0.9), 1.75), 1: ((1.5, 2.1), (0.5, 0.9), 1.6), 2: ((3.6, 5.4), (1.6, 2.1), 1.5), 3: ((1.6, 2.4), (0.6, 1.0), 1.5),
        5: ((8.0, 13.0), (2.3, 2.6), 3.1), 7: ((4.5, 10.0), (1.8, 2.6), 2.8)}


def frame_focal(X):
    """The focal length (pointmap pixels) a frame's own pointmap implies, centre at the image centre."""
    uu = np.arange(W)[None, :] - W / 2
    ok = (np.abs(uu) > 40) & (X[..., 2] > 1e-3) & (np.abs(X[..., 0]) > 1e-6)
    return float(np.median((uu * X[..., 2] / np.where(ok, X[..., 0], np.nan))[ok]))


def box_alpha(crop, box):
    """Alpha (0/255) of a box-only chip: GrabCut of the crop, seeded with a rounded rectangle in the box (core
    foreground, rim probably foreground, rest of the box probably background, outside definitely background);
    that rounded rectangle itself when GrabCut fails or finds an implausible share of the box."""
    h, w = crop.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w]
    u = np.abs((xx + 0.5 - (box[0] + box[2]) / 2) / max((box[2] - box[0]) / 2, 1.0))
    v = np.abs((yy + 0.5 - (box[1] + box[3]) / 2) / max((box[3] - box[1]) / 2, 1.0))
    rr = u ** 4 + v ** 4                                    # < 1 inside the rounded rectangle (superellipse)
    soft = (rr <= 1.0).astype(np.uint8) * 255
    if min(h, w) < 12:
        return soft
    gc = np.full((h, w), cv2.GC_BGD, np.uint8)
    gc[(u <= 1.0) & (v <= 1.0)] = cv2.GC_PR_BGD
    gc[rr <= 0.9] = cv2.GC_PR_FGD
    gc[rr <= 0.25] = cv2.GC_FGD
    try:
        cv2.grabCut(np.ascontiguousarray(crop), gc, None, np.zeros((1, 65)), np.zeros((1, 65)), 3, cv2.GC_INIT_WITH_MASK)
    except cv2.error:
        return soft
    fg = ((gc == cv2.GC_FGD) | (gc == cv2.GC_PR_FGD)).astype(np.uint8)
    n, lab = cv2.connectedComponents(fg)
    core = lab[rr <= 0.25]
    core = core[core > 0]
    if len(core) == 0:
        return soft
    fg = (lab == np.bincount(core).argmax()).astype(np.uint8)
    if not 0.5 * (soft > 0).sum() <= fg.sum() <= 1.1 * (soft > 0).sum():
        return soft
    return cv2.dilate(fg, np.ones((3, 3), np.uint8)) * 255


chips = {}
chip_bytes = []
for tid, (i, k) in pick.items():
    d = det_by_frame[i][k]
    L = lifted[(i, k)]
    # The mask at the source resolution; the pointmap interpolated under each pixel. A box-only detection has no
    # mask: its pixels are the box's central 60 %, and its chip gets an alpha of its own (box_alpha).
    m = np.zeros((meta["src_h"], meta["src_w"]), np.uint8)
    boxed = "poly" not in d
    if boxed:
        x1, y1, x2, y2 = d["box"]
        cx, cy, hw, hh = (x1 + x2) / 2, (y1 + y2) / 2, (x2 - x1) / 2 * BOX_INNER, (y2 - y1) / 2 * BOX_INNER
        m[max(0, int(round(cy - hh))):int(round(cy + hh)) + 1, max(0, int(round(cx - hw))):int(round(cx + hw)) + 1] = 1
        me = m
    else:
        cv2.fillPoly(m, [np.round(np.asarray(d["poly"])).astype(np.int32)], 1)
        me = cv2.erode(m, np.ones((3, 3), np.uint8)) if m.sum() > 200 else m
    vs, us = np.nonzero(me)
    pm = np.load(slam / "frames" / f"f_{i:05d}.npz")
    Xpm = pm["X"].astype(np.float64)
    X = bilinear(Xpm, us / SW - CW, vs / SH - CH)
    Cc = bilinear(pm["C"].astype(np.float64), us / SW - CW, vs / SH - CH)[:, 0]
    zm = np.median(X[:, 2])
    ok = (Cc > args.conf) & (X[:, 2] > 0.85 * zm) & (X[:, 2] < 1.18 * zm)
    if ok.sum() < 4:
        ok = np.ones(len(X), bool)       # an untrusted depth: the texture still projects, the size is the prior's
    P = enu(world(poses[i], X[ok]))
    c = enu(L["p"])
    ratio = L["ratio"]
    # The shape is the frame's; its size follows the terrain range (the frame's depth may be off by a factor).
    q = (P - c) * ratio
    r = row_at.get((round(i * TICK_HZ / args.fps), tid))
    speed = np.hypot(r[6], r[7]) if r else 0.0
    ry = yaw_of.get((round(i * TICK_HZ / args.fps), tid), first_yaw.get(tid, float("nan")))   # (the chip frame may precede the first row)
    if not np.isnan(ry):
        yaw0 = np.radians(ry)                  # the track's yaw (tracks3d.csv): course, or its footprint axis over all its views
    elif speed > 1.5:
        yaw0 = np.arctan2(r[6], r[7])          # bearing of the velocity, from north
    else:
        ev, evec = np.linalg.eigh(np.cov(q[:, :2].T))
        yaw0 = np.arctan2(evec[0, 1], evec[1, 1])
    # Object frame: x along the heading, y to its left, z up; origin at the surface median, which sits
    # at the class's centre height above the ground (lift: the ray meets the terrain at that height).
    fwd3 = np.array([np.sin(yaw0), np.cos(yaw0), 0.0])
    left3 = np.array([-np.cos(yaw0), np.sin(yaw0), 0.0])
    Rz = np.stack([fwd3, left3, [0, 0, 1.0]], 1)          # object -> ENU (columns)
    loc = q @ Rz
    cls = max(tracks[tid]["cls"], key=tracks[tid]["cls"].get) if tid in tracks else d["cls"]
    (lmin, lmax), (wmin, wmax), height = DIMS[cls]
    gain = args.box_gain if boxed else 1.0      # a box-only window measures that fraction of the object's size
    length = float(np.clip((np.percentile(loc[:, 0], 95) - np.percentile(loc[:, 0], 5)) / gain, lmin, lmax))
    width = float(np.clip((np.percentile(loc[:, 1], 95) - np.percentile(loc[:, 1], 5)) / gain, wmin, wmax))
    if width > length:
        length, width = width, length
    # The crop: the box padded by 15 %, the mask as alpha (box-only: box_alpha), at most 128 px on the long side.
    x1, y1, x2, y2 = d["box"]
    px, py = 0.15 * (x2 - x1) + 2, 0.15 * (y2 - y1) + 2
    cx0, cy0 = int(max(0, x1 - px)), int(max(0, y1 - py))
    cx1, cy1 = int(min(meta["src_w"], x2 + px)), int(min(meta["src_h"], y2 + py))
    crop = full[i][cy0:cy1, cx0:cx1]
    if boxed:
        alpha = box_alpha(crop, (x1 - cx0, y1 - cy0, x2 - cx0, y2 - cy0))
    else:
        alpha = (cv2.dilate(m, np.ones((3, 3), np.uint8))[cy0:cy1, cx0:cx1] * 255).astype(np.uint8)
    sc = min(1.0, 128.0 / max(crop.shape[:2]))
    tw, th = max(2, round(crop.shape[1] * sc)), max(2, round(crop.shape[0] * sc))
    rgba = np.dstack([cv2.resize(crop, (tw, th), interpolation=cv2.INTER_AREA), cv2.resize(alpha, (tw, th), interpolation=cv2.INTER_AREA)])
    okw, webp = cv2.imencode(".webp", cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA), [cv2.IMWRITE_WEBP_QUALITY, 80])
    # Projection, object metres -> crop uv: object -> SLAM-frame ENU around the frame's own placement
    # (sizes / ratio) -> SLAM world -> camera -> source pixels (this frame's focal) -> crop.
    A = np.eye(4); A[:3, :3] = Rz / ratio; A[:3, 3] = c
    Bm = np.eye(4); Bm[:3, :3] = B.T / scale; Bm[:3, 3] = O
    Mp = poses[i]; s3 = np.cbrt(np.linalg.det(Mp[:3, :3])); Rm = Mp[:3, :3] / s3
    Cm = np.eye(4); Cm[:3, :3] = Rm.T / s3; Cm[:3, 3] = -Rm.T @ Mp[:3, 3] / s3
    f_src = frame_focal(Xpm) * SW
    Kp = np.array([[f_src, 0, (W / 2 + CW) * SW, 0], [0, f_src, (H / 2 + CH) * SH, 0], [0, 0, 1, 0]])
    Cr = np.array([[1 / (cx1 - cx0), 0, -cx0 / (cx1 - cx0)], [0, 1 / (cy1 - cy0), -cy0 / (cy1 - cy0)], [0, 0, 1]])
    Mproj = Cr @ Kp @ Cm @ Bm @ A
    Mproj /= np.linalg.norm(Mproj[2, :3])
    # The camera centre in the object frame (which faces it saw), in true metres.
    cam_obj = Rz.T @ (cam_enu[i] - c) * ratio
    nbytes = len(webp) + 12 * 2 + 3 * 2 + 8   # crop + projection (f16) + size + header
    chip_bytes.append(nbytes)
    first = obs[tid][0][0]
    chips[tid] = {"cls": int(cls), "frame": int(i), "tick": round(i * TICK_HZ / args.fps), "ready_tick": round(min(obs[tid][-1][0], first + 2 * args.fps) * TICK_HZ / args.fps),
                  "yaw0": round(float(np.degrees(yaw0)) % 360, 1), "size": [round(length, 2), round(width, 2), height], "base": -H_C[cls],
                  "proj": [round(float(v), 6) for v in Mproj.reshape(-1)], "cam": [round(float(v), 2) for v in cam_obj],
                  "webp": __import__("base64").b64encode(webp.tobytes()).decode(), "px": [tw, th], "wire_bytes": nbytes}
(out / "chips.json").write_text(json.dumps(chips))
print(f"chips: {len(chips)}, texture px median {int(np.median([max(c['px']) for c in chips.values()]))}, wire bytes median {int(np.median(chip_bytes))} (p90 {int(np.percentile(chip_bytes, 90))})")

# ---- 5. static map ---------------------------------------------------------------------------------
Pm = Pmap
keep = (Pm[:, 2] > -5) & (Pm[:, 2] < 80)
Pm, Cm = Pm[keep], map_rgb[keep]
vox = np.floor(Pm / args.voxel).astype(np.int64)
_, idx, inv = np.unique(vox, axis=0, return_index=True, return_inverse=True)
inv = inv.reshape(-1)
cnt = np.bincount(inv)
Pv = np.stack([np.bincount(inv, Pm[:, a]) / cnt for a in range(3)], 1)
Cv = np.stack([np.bincount(inv, Cm[:, a].astype(np.float64)) / cnt for a in range(3)], 1)
keepv = cnt >= 2
Pv, Cv = Pv[keepv].astype(np.float32), np.clip(Cv[keepv], 0, 255).astype(np.uint8)
buf = np.zeros(len(Pv), dtype=[("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("r", "u1"), ("g", "u1"), ("b", "u1"), ("a", "u1")])
buf["x"], buf["y"], buf["z"] = Pv[:, 0], Pv[:, 1], Pv[:, 2]
buf["r"], buf["g"], buf["b"], buf["a"] = Cv[:, 0], Cv[:, 1], Cv[:, 2], 255
buf.tofile(out / "map.bin")
lo, hi = Pv.min(0), Pv.max(0)
(out / "map.json").write_text(json.dumps({"points": int(len(Pv)), "stride": 16, "voxel_m": args.voxel, "min": lo.tolist(), "max": hi.tolist(), "frame": "ENU metres, origin on the ground below the first camera"}))

f_pm = None
pm0 = np.load(slam / "frames" / f"f_{min(poses):05d}.npz")["X"].astype(np.float64)
uu = np.arange(W)[None, :] - W / 2
okf = (np.abs(uu) > 40) & (pm0[..., 2] > 1e-3) & (np.abs(pm0[..., 0]) > 1e-6)
f_pm = float(np.median((uu * pm0[..., 2] / np.where(okf, pm0[..., 0], np.nan))[okf]))
f_src = f_pm * SW
summary = {"fps": args.fps, "width": meta["src_w"], "height": meta["src_h"], "frames": meta["frames"], "f_px": round(f_src, 1),
           "hfov_deg": round(float(np.degrees(2 * np.arctan(meta["src_w"] / 2 / f_src))), 1), "scale_m_per_unit": round(scale, 4),
           "car_length_m": args.car_length, "car_sightings": len(lengths), "ground_inlier_frac": round(best[0] / len(sample), 3),
           "camera_alt_agl_m": round(float(np.median([r[3] for r in cam_rows])), 2), "camera_path_m": round(float(np.linalg.norm(enu(cams[max(cams)]) - enu(cams[min(cams)]))), 1),
           "tracks_3d": next_id - 1, "duplicates_merged": dups, "riders_relabelled": riders_fast, "tracks_reported": len(reported), "id_switches_rejoined": merges, "rows": len(rows),
           "chips": len(chips), "chip_bytes_median": int(np.median(chip_bytes)), "map_points": int(len(Pv)), "map_extent_m": (hi - lo).round(1).tolist(),
           "basis": {"E": E.tolist(), "N": N.tolist(), "U": U.tolist(), "O": O.tolist()}}
(out / "summary.json").write_text(json.dumps(summary, indent=1))
print(json.dumps({k: v for k, v in summary.items() if k != "basis"}))
