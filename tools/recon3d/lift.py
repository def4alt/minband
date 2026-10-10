"""Stage 3 of tools/recon3d: detections into the reconstruction, in metres, as 3D tracks.

    conda run -n YOLO-CUDA-13 python tools/recon3d/lift.py <run> [--car-length 4.5]

Inputs (from slam_export.py and detect.py): <run>/slam/{meta,poses}.json, slam/frames/*.npz,
slam/keyframes.npz, detections.json, clip.mp4.

1. Lift. A detection's mask selects its own pixels in the frame's pointmap (MASt3R-SLAM gives every
   frame a pixel-aligned 3D pointmap); the confident ones at the object's depth, in the world frame,
   are the object's visible surface. Their median gives the object's *direction* from the camera.
   Its *range* comes from the reconstruction: the ray meets the static map's terrain (a height field,
   lowest surface per 0.5 m cell) at the class's centre height above it. One frame's depth jitters
   by metres along the ray at a shallow look-down angle; the fused multi-view terrain does not
   (DESIGN.md 5: the ray against the server's own terrain, done on the edge). No flat ground.
2. Metric frame. The ground plane is fitted to the static map (RANSAC, normals near the cameras' up);
   up is its normal, north the first camera's heading, origin the ground below the first camera.
   Monocular SLAM has no scale, so the scale comes from the cars: the median ground-plane length of
   the car surfaces = `--car-length` (4.5 m). Stated assumption, like tools/footage's person width.
3. Track. ByteTrack's 2D ids give the association; per object a causal constant-velocity Kalman
   filter on the ground gives position and velocity (no smoothing with future frames: the edge
   cannot); the height is the terrain under the filtered position plus the class's centre height.
   A 2D track born near where a lost one of the same coarse class was heading is the same object
   (3D re-identification of ByteTrack's id switches).
4. Appearance. Per object, from its best view in its first 2 s: its footprint measured in the
   reconstruction (length and width, clamped to class priors), the masked image crop (WebP with
   alpha, <= 128 px), and the projection from the object's frame (x along its heading, z up) into
   that crop. The page draws a solid of that size with the drone's own view of the object projected
   onto the faces the drone saw: the operator sees that car, not a blob, and its pixels cross the
   link once while its motion keeps crossing as contact reports.
5. Static map. Keyframe pointmaps fused in the metric frame, with every detected object masked out,
   so moving cars leave no smears; voxel-filtered.

Writes <run>/scene/: tracks3d.csv (tick,id,class,e,n,u,ve,vn,vu,conf,bu,bv,bw,bh,ce), cameras.csv
(tick,e,n,u,yaw,pitch,roll), chips.json, map.bin (+ map.json), summary.json.
"""
import argparse
import json
import pathlib
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
ap.add_argument("--min-hits", type=int, default=6, help="2D hits before a track is reported")
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


def to_pm(xy):
    """Source pixels -> pointmap pixels."""
    xy = np.asarray(xy, np.float64)
    return np.stack([xy[..., 0] / SW - CW, xy[..., 1] / SH - CH], -1)


def mask_of(d):
    m = np.zeros((H, W), np.uint8)
    if "poly" in d:
        cv2.fillPoly(m, [np.round(to_pm(d["poly"])).astype(np.int32)], 1)
        if m.sum() > 30:
            m = cv2.erode(m, np.ones((3, 3), np.uint8))
    else:
        (x1, y1), (x2, y2) = to_pm([d["box"][:2], d["box"][2:]])
        cx, cy, hw, hh = (x1 + x2) / 2, (y1 + y2) / 2, (x2 - x1) / 4, (y2 - y1) / 4
        m[int(cy - hh):int(cy + hh) + 1, int(cx - hw):int(cx + hw) + 1] = 1
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
print(f"lifted {len(lifted)} of {sum(len(f['dets']) for f in dets)} detections")

# ---- 2. metric frame -------------------------------------------------------------------------------
kf = np.load(slam / "keyframes.npz")
det_by_frame = {f["i"]: f["dets"] for f in dets}
map_pts, map_rgb = [], []
for k, fid in enumerate(kf["frame_id"]):
    fid = int(fid)
    if fid not in poses:
        continue
    X, C, rgb = kf["X"][k].astype(np.float64), kf["C"][k].astype(np.float32), kf["rgb"][k]
    objects = np.zeros((H, W), np.uint8)
    for d in det_by_frame.get(fid, []):
        objects |= mask_of(d).astype(np.uint8)
    objects = cv2.dilate(objects, np.ones((7, 7), np.uint8)).astype(bool)
    ok = (C > args.conf) & ~objects
    map_pts.append(world(poses[fid], X[ok]))
    map_rgb.append(rgb[ok])
map_pts, map_rgb = np.concatenate(map_pts), np.concatenate(map_rgb)

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

# Scale from the cars: ground-plane length of each car's visible surface, median over sightings.
lengths = []
for (i, k), L in lifted.items():
    d = det_by_frame[i][k]
    x1, y1, x2, y2 = d["box"]
    if d["cls"] != 2 or L["lowconf"] or len(L["pts"]) < 80 or x1 < 8 or y1 < 8 or x2 > meta["src_w"] - 8 or y2 > meta["src_h"] - 8:
        continue
    q = (L["pts"] - O) @ B[:2].T
    q -= q.mean(0)
    ev, evec = np.linalg.eigh(np.cov(q.T))
    major = q @ evec[:, 1]
    lengths.append(np.percentile(major, 97) - np.percentile(major, 3))
scale = args.car_length / float(np.median(lengths))
print(f"ground plane: {best[0]} of {len(sample)} sample points within {tau:.4f}; car sightings {len(lengths)}, "
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


def terrain(e, n):
    i = np.clip(((np.asarray(e) - g_lo[0]) / args.cell).astype(int), 0, gw - 1)
    j = np.clip(((np.asarray(n) - g_lo[1]) / args.cell).astype(int), 0, gh - 1)
    return hf[j, i]


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

for (i, k), L in lifted.items():
    cl = det_by_frame[i][k]["cls"]
    pe = enu(L["p"])
    hit = on_terrain(cam_enu[i], pe, H_C[cl], wide=L["lowconf"])
    lo_r, hi_r = (0.2, 5.0) if L["lowconf"] else (0.5, 2.0)
    if hit is not None and lo_r < hit[1] < hi_r:
        L["enu"], L["ratio"], L["ranged"] = hit[0], hit[1], True
        ranged += 1
    else:
        L["enu"], L["ratio"], L["ranged"] = pe, 1.0, False
for key in [key for key, L in lifted.items() if L["lowconf"] and not L["ranged"]]:
    del lifted[key]   # neither a trusted depth nor a terrain hit: no position
print(f"terrain {gw}x{gh} cells of {args.cell} m; {ranged} of {len(lifted)} detections ranged on it "
      f"({sum(L['lowconf'] for L in lifted.values())} from the direction alone, low pointmap confidence)")

# ---- 3. causal tracks ------------------------------------------------------------------------------
class KF:
    """Constant velocity on the ground (e, n), one per object; the height is the terrain's."""
    def __init__(self, p, accel, sigma):
        self.x = np.r_[p[:2], 0, 0].astype(float)
        self.P = np.diag([sigma ** 2, sigma ** 2, 16, 16])
        self.q, self.r = accel ** 2, sigma ** 2

    def predict(self, dt):
        F = np.eye(4); F[:2, 2:] = dt * np.eye(2)
        G = np.r_[0.5 * dt * dt, 0.5 * dt * dt, dt, dt]
        self.x = F @ self.x
        self.P = F @ self.P @ F.T + np.diag(G * G) * self.q

    def update(self, z, gate=13.8):
        Hm = np.hstack([np.eye(2), np.zeros((2, 2))])
        S = Hm @ self.P @ Hm.T + self.r * np.eye(2)
        y = z[:2] - Hm @ self.x
        m2 = float(y @ np.linalg.solve(S, y))
        if m2 > gate:
            return False
        K = self.P @ Hm.T @ np.linalg.inv(S)
        self.x = self.x + K @ y
        self.P = (np.eye(4) - K @ Hm) @ self.P
        return True


def new_track(p, cl, t, tid2):
    veh = COARSE[cl] == 1
    return {"kf": KF(p, 2.0 if veh else 0.8, 0.35 if veh else 0.25), "last_t": t, "first_t": t, "hits": 0, "misses": 0,
            "coarse": COARSE[cl], "cls": defaultdict(float), "live2d": tid2}


tracks = {}      # track id -> state
alias = {}       # 2D id -> track id
next_id = 1
rows = []
obs = defaultdict(list)  # track id -> [(frame, det index)]
dt = 1.0 / args.fps
merges = rejects = dups = riders_fast = 0
near = defaultdict(int)  # (older, younger) -> consecutive frames closer than one object's size
# Closer than this, two tracks of a coarse class are one object (centre to centre, m).
SAME_OBJECT_M = {0: 0.35, 1: 1.2}
# A dismount sustaining this speed is riding something the detector missed (m/s).
RIDER_MPS = 3.5
for f in dets:
    i = f["i"]
    t = i * dt
    seen = set()
    live2d = {x["id"] for x in f["dets"]}
    for k, d in enumerate(f["dets"]):
        L = lifted.get((i, k))
        if L is None:
            continue
        p = L["enu"]
        cl = d["cls"]
        tid2 = d["id"]
        if tid2 not in alias:
            # A new 2D id: the same object as a recently lost track of its coarse class heading here?
            cand, best_d = None, 1e9
            for tid, s in tracks.items():
                gap = t - s["last_t"]
                if s["coarse"] != COARSE[cl] or gap <= 0 or gap > 1.5 or tid in seen or s["live2d"] in live2d:
                    continue
                pred = s["kf"].x[:2] + s["kf"].x[2:] * gap
                dist = np.linalg.norm(pred - p[:2])
                if dist < 1.5 + 1.0 * gap and dist < best_d:
                    cand, best_d = tid, dist
            if cand is not None:
                alias[tid2] = cand
                merges += 1
            else:
                alias[tid2] = next_id
                tracks[next_id] = new_track(p, cl, t, tid2)
                next_id += 1
        tid = alias[tid2]
        s = tracks[tid]
        s["live2d"] = tid2
        if s["hits"] > 0:
            s["kf"].predict(t - s["last_t"])
            if not s["kf"].update(p):
                rejects += 1
                s["misses"] += 1
                if s["misses"] >= 4:   # the filter lost it: restart at the measurements
                    s["kf"] = new_track(p, cl, t, tid2)["kf"]
                    s["misses"] = 0
            else:
                s["misses"] = 0
        s["last_t"] = t
        s["hits"] += 1
        s["cls"][cl] += d["conf"]
        if s["coarse"] == 0 and s["hits"] >= 10 and np.hypot(*s["kf"].x[2:]) > RIDER_MPS:
            s["cls"][3] += 2 * d["conf"]
            if not s.get("rider"):
                s["rider"] = True; riders_fast += 1
        seen.add(tid)
        obs[tid].append((i, k))
        if s["hits"] >= args.min_hits:
            x1, y1, x2, y2 = d["box"]
            bb = [(x1 + x2) / 2 / meta["src_w"], (y1 + y2) / 2 / meta["src_h"], (x2 - x1) / meta["src_w"], (y2 - y1) / meta["src_h"]]
            cls = max(s["cls"], key=s["cls"].get)
            x = s["kf"].x
            ground = terrain(x[0], x[1])
            u = float(ground) + H_C[cls] if np.isfinite(ground) else float(p[2])
            # Error radius (1 sigma) in the reconstruction's frame: the filter's position spread plus the
            # ranging error, a terrain height error of 0.15 m stretched by the ray's shallow angle.
            ce_m = float(np.hypot(np.sqrt(0.5 * (s["kf"].P[0, 0] + s["kf"].P[1, 1])), 0.15 / max(np.tan(np.radians(5)), (u - cam_enu[i][2]) / -max(1e-3, np.linalg.norm(x[:2] - cam_enu[i][:2])))))
            if not L["ranged"]:
                ce_m = max(ce_m, 3.0)
            rows.append((round(i * TICK_HZ / args.fps), tid, cls, x[0], x[1], u, x[2], x[3], 0.0, min(255, round(255 * d["conf"])), *bb, ce_m))

    # One object tracked twice (ByteTrack opened a new id while the old one lived): the younger
    # track joins the older one once they have stayed closer than one object for 5 frames.
    ids = sorted(seen)
    for a_i, a in enumerate(ids):
        for b in ids[a_i + 1:]:
            sa, sb = tracks[a], tracks[b]
            if sa["coarse"] != sb["coarse"]:
                continue
            if np.linalg.norm(sa["kf"].x[:2] - sb["kf"].x[:2]) < SAME_OBJECT_M[sa["coarse"]]:
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
            del tracks[young]
            near.pop((a, b))
            dups += 1

with open(out / "tracks3d.csv", "w") as fh:
    fh.write("tick,id,class,e,n,u,ve,vn,vu,conf,bu,bv,bw,bh,ce\n")
    for r in rows:
        fh.write(f"{r[0]},{r[1]},{r[2]}," + ",".join(f"{v:.3f}" for v in r[3:9]) + f",{r[9]}," + ",".join(f"{v:.4f}" for v in r[10:14]) + f",{r[14]:.2f}\n")
reported = {r[1] for r in rows}
print(f"tracks: {next_id - 1} ({merges} 2D id switches re-joined in 3D, {dups} duplicates merged, {riders_fast} fast dismounts relabelled riders, "
      f"{rejects} gated-out measurements), reported {len(reported)}, rows {len(rows)}")

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


chips = {}
chip_bytes = []
for tid, (i, k) in pick.items():
    d = det_by_frame[i][k]
    L = lifted[(i, k)]
    # The mask at the source resolution; the pointmap interpolated under each pixel.
    m = np.zeros((meta["src_h"], meta["src_w"]), np.uint8)
    if "poly" in d:
        cv2.fillPoly(m, [np.round(np.asarray(d["poly"])).astype(np.int32)], 1)
    else:
        x1, y1, x2, y2 = map(int, d["box"]); m[y1:y2, x1:x2] = 1
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
    if speed > 1.5:
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
    length = float(np.clip(np.percentile(loc[:, 0], 95) - np.percentile(loc[:, 0], 5), lmin, lmax))
    width = float(np.clip(np.percentile(loc[:, 1], 95) - np.percentile(loc[:, 1], 5), wmin, wmax))
    if width > length:
        length, width = width, length
    # The crop: the box padded by 15 %, the mask as alpha, at most 128 px on the long side.
    x1, y1, x2, y2 = d["box"]
    px, py = 0.15 * (x2 - x1) + 2, 0.15 * (y2 - y1) + 2
    cx0, cy0 = int(max(0, x1 - px)), int(max(0, y1 - py))
    cx1, cy1 = int(min(meta["src_w"], x2 + px)), int(min(meta["src_h"], y2 + py))
    crop = full[i][cy0:cy1, cx0:cx1]
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
