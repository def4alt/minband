"""Drone footage -> MinBand track log (the CSV ios/MinBand/GroundTruthLog.swift writes).

What a drone edge would do without ARKit: detect objects in each frame, remove the camera's own
motion, put every detection on the ground in metres, and track it with a constant-velocity Kalman
filter at the frame rate. The output feeds `tools/eval` (`--gt`) and the sim's track replay
(`TRACKS=` in server/src/sim.ts) exactly like a phone log.

  python track.py detect  VIDEO --model M.onnx --start 15 --end 105 --out DIR   # slow: appearance detector
  python track.py detect  VIDEO --model mil.onnx --tag mil --out DIR             # a second model, own cache
  python track.py mti     VIDEO --out DIR                                        # motion detector (mti.py)
  python track.py track   DIR [--sources det,mti] [--out OUT]                    # ground fit, fusion, tracker, CSV
  python track.py preview DIR VIDEO                                              # annotated mp4

Stages:
- detect: every `--every`-th frame (default: ~5 Hz; the phone runs its detector at ~12 Hz and its
  tracker at 30 Hz) the tiled detector (detect.py) finds people and vehicles, and an ORB + RANSAC
  homography maps the frame onto a reference frame (the first one), so a hovering or drifting drone
  does not turn into moving objects. Direct registration to the reference is tried first; when the
  view has moved too far, the previous frame's chain is used. Caches: detections.npy (after NMS),
  detections-raw.npy (before NMS) and detect.json; with --tag T, detections-T.npy and detect-T.json.
- mti: class-agnostic moving-target indication on the same frames (mti.py): candidates in
  detections-mti.npy, statistics in mti.json. Size, aspect and persistence filters run in `track`,
  where the ground model is known.
- ground: a flat-ground pinhole camera (focal length from the camera's field of view) whose pitch and
  height are fitted from people's box widths: 1/t = (sin(pitch) + y_c cos(pitch)) / h is linear in the
  normalised image row y_c, where t = W f / w_px is the range a person of width W must be at. Without
  enough people: the same fit on vehicles' box widths (an assumed ~3 m, much looser), and without
  either, an assumed pitch and height (stated in summary.json). Bootstrap spread of the scale is
  reported. The walkers' median speed is printed as a check (people walk at ~1.3-1.4 m/s).
- fusion: per detection frame, appearance sources are merged (overlap), then each MTI mover that
  overlaps an appearance box (or lies within a gate of its foot point on the ground) is absorbed by it:
  where the appearance class exists it wins. The rest become unclassified movers (class 100).
- track: two-stage (high, then low confidence) Hungarian association in metres with class groups
  (dismounts, vehicles, movers); a mover track is promoted to a dismount or vehicle when an
  appearance detection associates with it, and a classified track can be kept alive by motion
  detections. Birth after 3 hits; coasting up to 2 s with a gate that grows with the time since the
  last detection; a lost track can be re-acquired (same id) for 3 s instead of a new one being born,
  and one older than 5 s for 20 s where it was last seen, with a tight gate (it stopped: a walker who
  waits, a car that parks); a track is reported once it is 1 s old, so short spurious tracks never
  reach the link; a static mode (measured motion over 3 s below what jitter explains) averages a
  parked object's position, reports zero velocity, coasts 4 s, and leaves it on a large innovation;
  a dismount track and a two-wheeler detection associate (a rider is boxed either way). Motion-only
  tracks that are not objects are tracked but not reported: one in lockstep with a confirmed track
  at most half its size (the far end of a low-sun shadow, a fragment), and one that slides against
  the camera's own motion at a fraction of its speed (parallax of a tree top or a roof edge; the
  camera's motion comes from the plane-to-image homographies). KF state reported at every video
  frame. --legacy-tracker restores the original tracker; round1.py runs the round-1 one (d59938b).
- registration: ORB to the reference while it holds; otherwise an optical-flow chain from the
  previous frame (low-texture thermal, views that leave the reference), with hysteresis.
"""
import argparse, json, math, os, sys
import numpy as np
import cv2
from scipy.optimize import linear_sum_assignment

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TICK_HZ = 120
MOVER = 100
ARMOURED = 101
VEHICLES = {1, 2, 3, 5, 7, ARMOURED}  # bicycle, car, motorcycle, bus, truck (COCO), armoured (MinBand)
TWO_WHEELERS = {1, 3}                 # a rider on one is boxed as a person on some frames, a two-wheeler on others
DISMOUNT_G, VEHICLE_G, MOVER_G = 0, 1, 2
CLASS_NAME = {0: 'dismount', 1: 'bicycle', 2: 'car', 3: 'motorcycle', 5: 'bus', 7: 'truck', MOVER: 'mover', ARMOURED: 'armoured'}


def group(k: int) -> int:
    return MOVER_G if k == MOVER else (VEHICLE_G if k in VEHICLES else DISMOUNT_G)


def cache_files(d: str, name: str):
    """(rows .npy, meta .json) of a detection cache: det (appearance, the default), mti, or a tag."""
    if name == 'det': return os.path.join(d, 'detections.npy'), os.path.join(d, 'detect.json')
    if name == 'mti': return os.path.join(d, 'detections-mti.npy'), os.path.join(d, 'mti.json')
    return os.path.join(d, f'detections-{name}.npy'), os.path.join(d, f'detect-{name}.json')


# ---- registration --------------------------------------------------------------------------------

class Registrar:
    """Homographies from each frame to the reference frame (the first one).

    Direct registration to the reference (ORB + RANSAC) while it holds; otherwise the chain: sparse
    optical flow (Shi-Tomasi + pyramidal LK, forward-backward checked, RANSAC) from the previous
    registered frame, which works on low-texture thermal and keeps going after the view has left the
    reference. With hysteresis: once on the chain, back to the reference only on a strong match, so
    the scene does not jump back and forth by the chain's accumulated drift."""

    def __init__(self, ref: np.ndarray, scale: float = 0.0, ref_min: int = 150, back_min: int = 300):
        self.scale = scale or min(1.0, 1920 / ref.shape[1])
        self.orb = cv2.ORB_create(4000)
        self.bf = cv2.BFMatcher(cv2.NORM_HAMMING)
        self.ref_min, self.back_min = ref_min, back_min
        g = self.gray(ref)
        self.ref = self.orb.detectAndCompute(g, None)
        self.prev_g = g; self.prev_H = np.eye(3); self.mode = 'ref'

    def gray(self, frame):
        g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        return cv2.resize(g, None, fx=self.scale, fy=self.scale, interpolation=cv2.INTER_AREA) if self.scale != 1 else g

    def match(self, a, b):
        """Homography mapping ORB points of `a` onto `b` (full-resolution pixels), inlier count."""
        (ka, da), (kb, db) = a, b
        if da is None or db is None or len(ka) < 50 or len(kb) < 50: return None, 0
        m = [p for p, q in (x for x in self.bf.knnMatch(da, db, k=2) if len(x) == 2) if p.distance < 0.75 * q.distance]
        if len(m) < 40: return None, 0
        src = np.float32([ka[x.queryIdx].pt for x in m]) / self.scale
        dst = np.float32([kb[x.trainIdx].pt for x in m]) / self.scale
        H, inl = cv2.findHomography(src, dst, cv2.RANSAC, 3.0)
        return H, int(inl.sum()) if inl is not None else 0

    def flow(self, cur_g, prev_g):
        """Homography mapping `cur_g` onto `prev_g` by sparse optical flow (full-resolution pixels)."""
        p = cv2.goodFeaturesToTrack(prev_g, 2000, 0.005, 8, blockSize=7)
        if p is None or len(p) < 30: return None, 0
        q, st, _ = cv2.calcOpticalFlowPyrLK(prev_g, cur_g, p, None, winSize=(21, 21), maxLevel=4)
        b, st2, _ = cv2.calcOpticalFlowPyrLK(cur_g, prev_g, q, None, winSize=(21, 21), maxLevel=4)
        ok = (st[:, 0] == 1) & (st2[:, 0] == 1) & (np.linalg.norm((b - p).reshape(-1, 2), axis=1) < 1.0)
        if ok.sum() < 30: return None, int(ok.sum())
        H, inl = cv2.findHomography(q[ok].reshape(-1, 2) / self.scale, p[ok].reshape(-1, 2) / self.scale, cv2.RANSAC, 2.0)
        return H, int(inl.sum()) if inl is not None else 0

    def __call__(self, frame):
        """(H, inliers, how); H is None when neither the reference nor the chain registers."""
        g = self.gray(frame)
        H, n = self.match(self.orb.detectAndCompute(g, None), self.ref)
        need = self.ref_min if self.mode == 'ref' else self.back_min
        if H is not None and n >= need:
            how = 'ref'
        else:
            Hc, n2 = self.flow(g, self.prev_g)
            if Hc is None or n2 < 30: return None, max(n, n2), 'lost'
            H, n, how = self.prev_H @ Hc, n2, 'chain'
        self.mode = how
        self.prev_g, self.prev_H = g, H
        return H, n, how


def frame_schedule(cap, a, d: str):
    """f0, f1, every: from the arguments, else from a cache already in `d` (so all caches share frames)."""
    fps = cap.get(cv2.CAP_PROP_FPS); nframes = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    prior = None
    for name in ('detect.json', 'mti.json'):
        p = os.path.join(d, name)
        if os.path.exists(p): prior = json.load(open(p)); break
    if a.start is None and prior: f0 = prior['start_frame']
    else: f0 = int(round((a.start or 0) * fps))
    if a.end is None and prior: f1 = prior['end_frame']
    else: f1 = min(nframes - 1, int(round(a.end * fps))) if a.end is not None else nframes - 1
    every = a.every or (prior['every'] if prior else max(1, int(round(fps / 5))))
    return fps, f0, f1, every


# ---- detect (appearance) -------------------------------------------------------------------------

def cmd_detect(a):
    from detect import TiledDetector
    det = TiledDetector(a.model, tile=a.tile, overlap=64, conf=a.conf, threads=a.threads)
    cap = cv2.VideoCapture(a.video)
    os.makedirs(a.out, exist_ok=True)
    fps, f0, f1, every = frame_schedule(cap, a, a.out)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)); h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.set(cv2.CAP_PROP_POS_FRAMES, f0)
    reg = None; rows = []; raw_rows = []; homs = {}; n = f0; lost = 0; end_reason = 'end'
    while n <= f1:
        ok, frame = cap.read()
        if not ok: end_reason = 'decode'; break
        if (n - f0) % every == 0:
            if reg is None:
                reg = Registrar(frame); H, ninl, how = np.eye(3), -1, 'ref'
                cv2.imwrite(os.path.join(a.out, 'reference.jpg' if a.tag == 'det' else f'reference-{a.tag}.jpg'), frame)
            else:
                H, ninl, how = reg(frame)
            if H is None:  # a cut or a view change too large to register: the ground frame ends here
                lost += 1
                print(f'frame {n}: registration lost ({ninl} inliers)', flush=True)
                if lost >= 3: end_reason = 'registration'; break
                n += 1; continue
            lost = 0
            homs[n] = H.tolist()
            kept, raw = det(frame, raw=True)
            for x1, y1, x2, y2, c, k in kept: rows.append([n, x1, y1, x2, y2, c, k])
            for x1, y1, x2, y2, c, k in raw: raw_rows.append([n, x1, y1, x2, y2, c, k])
            print(f'frame {n} ({(n - f0) / fps:6.1f} s): {len(kept):3d} boxes, H {how} {ninl} inliers', flush=True)
        n += 1
    npy, js = cache_files(a.out, a.tag)
    np.save(npy, np.array(rows, np.float64).reshape(-1, 7))
    np.save(npy.replace('.npy', '-raw.npy') if a.tag != 'det' else os.path.join(a.out, 'detections-raw.npy'),
            np.array(raw_rows, np.float64).reshape(-1, 7))
    last = max(homs) if homs else f0
    json.dump({'video': os.path.basename(a.video), 'fps': fps, 'width': w, 'height': h, 'start_frame': f0,
               'end_frame': min(f1, last + every - 1), 'end_reason': end_reason,
               'every': every, 'model': os.path.basename(a.model), 'names': det.names, 'tile': det.tile_for(w, h), 'conf': a.conf,
               'nms': 'class-wise, IoU 0.5, xywh (fixed)', 'homographies': homs}, open(js, 'w'))


# ---- mti (motion) --------------------------------------------------------------------------------

def cmd_mti(a):
    from mti import MTI, MOVER as MOVER_ID, to_work, score, overlay_mask
    cap = cv2.VideoCapture(a.video)
    os.makedirs(a.out, exist_ok=True)
    fps, f0, f1, every = frame_schedule(cap, a, a.out)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)); h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    k = max(1, int(round(a.baseline * fps)))
    m = MTI(work_width=a.work_width, z_seed=a.z_seed, z_grow=a.z_grow, min_area=a.min_area)
    s = m.scale_for(w)
    # Homographies to the reference: reuse the appearance cache's when there is one (same frames).
    dj = os.path.join(a.out, 'detect.json')
    homs = {int(n): H for n, H in json.load(open(dj))['homographies'].items()} if os.path.exists(dj) else None
    det_frames = list(range(f0, f1 + 1, every))
    if homs is not None: det_frames = [n for n in det_frames if n in homs]
    need = set(det_frames) | {n - k for n in det_frames} | {n + k for n in det_frames}
    # Static overlays (watermark, HUD, logo): masked out of the differences (mti.overlay_mask).
    overlay, ov_frac = None, 0.0
    if homs is not None and len(det_frames) >= 8:
        sample = [det_frames[int(i)] for i in np.linspace(0, len(det_frames) - 1, min(24, len(det_frames)))]
        grays = []
        for n in sample:
            cap.set(cv2.CAP_PROP_POS_FRAMES, n); ok, fr = cap.read()
            if ok: grays.append(to_work(fr, s))
        overlay = overlay_mask(grays, [homs[n] for n in sample[:len(grays)]], s)
        ov_frac = float(overlay.mean())
        cv2.imwrite(os.path.join(a.out, 'overlay-mask.png'), cv2.resize(overlay.astype(np.uint8) * 255, (w, h), interpolation=cv2.INTER_NEAREST))
        print(f'overlay mask: {ov_frac * 100:.1f} % of the frame', flush=True)
    start = max(0, f0 - k)
    cap.set(cv2.CAP_PROP_POS_FRAMES, start)
    buf, rows, extra, stats, own_homs = {}, [], [], {}, {}
    reg = None; n = start; todo = list(det_frames)
    while todo and n <= todo[-1] + k:
        ok, frame = cap.read()
        if not ok: break
        if n in need: buf[n] = to_work(frame, s)
        if homs is None and n in det_frames:
            if reg is None: reg = Registrar(frame); own_homs[n] = np.eye(3).tolist()
            else:
                H, _, _ = reg(frame)
                if H is not None: own_homs[n] = H.tolist()
        while todo and n >= todo[0] + k:  # frame todo[0] + k is in: difference todo[0]
            c = todo.pop(0)
            if c - k in buf and c + k in buf and (homs is not None or c in own_homs):
                r, st, _ = m.detect(buf[c - k], buf[c], buf[c + k], overlay=overlay)
                stats[c] = st
                for x1, y1, x2, y2, area, zp, zm in r:
                    rows.append([c, x1 / s, y1 / s, x2 / s, y2 / s, score(zp, a.z_seed), MOVER_ID])
                    extra.append([c, area / (s * s), zp, zm])
                print(f'frame {c} ({(c - f0) / fps:6.1f} s): {len(r):3d} movers, inliers {st["inliers_b"]}/{st["inliers_f"]}, '
                      f'rms {st["rms_b"]:.2f}/{st["rms_f"]:.2f} px', flush=True)
            for old in [x for x in buf if x < (todo[0] - k if todo else n)]: del buf[old]
        n += 1
    npy, js = cache_files(a.out, 'mti')
    np.save(npy, np.array(rows, np.float64).reshape(-1, 7))
    np.save(os.path.join(a.out, 'detections-mti-extra.npy'), np.array(extra, np.float64).reshape(-1, 4))
    meta = {'video': os.path.basename(a.video), 'fps': fps, 'width': w, 'height': h, 'start_frame': f0, 'end_frame': f1,
            'every': every, 'baseline_frames': k, 'baseline_s': a.baseline, 'work_scale': s, 'z_seed': a.z_seed,
            'z_grow': a.z_grow, 'min_area_work_px': a.min_area, 'overlay_fraction': ov_frac, 'stats': {str(c): v for c, v in stats.items()}}
    if homs is None: meta['homographies'] = own_homs
    json.dump(meta, open(js, 'w'))


# ---- ground model --------------------------------------------------------------------------------

class Ground:
    """Flat ground seen by a pinhole camera pitched down by `pitch`, `h` metres up, no roll."""

    def __init__(self, f, cx, cy, pitch, h):
        self.f, self.cx, self.cy, self.pitch, self.h = f, cx, cy, pitch, h

    def range_t(self, v):
        yc = (v - self.cy) / self.f
        den = math.sin(self.pitch) + yc * math.cos(self.pitch)
        return self.h / den if den > 1e-3 else float('nan')

    def gsd(self, v):
        """Metres per pixel across the view at image row v."""
        return self.range_t(v) / self.f

    def to_ground(self, u, v):
        """Reference-image pixel -> (lateral X, forward Y) metres on the ground."""
        t = self.range_t(v)
        xc, yc = (u - self.cx) / self.f, (v - self.cy) / self.f
        return t * xc, t * (math.cos(self.pitch) - yc * math.sin(self.pitch))


def camera_centres(homs, g):
    """Camera centre (X, Y, height) on the ground model at each registered frame: the plane-to-image
    homography of frame n (the reference camera's, through the homography onto the reference) gives
    the camera's pose by the standard planar decomposition K^-1 M = [r1 r2 t]."""
    K = np.array([[g.f, 0, g.cx], [0, g.f, g.cy], [0, 0, 1.0]]); Ki = np.linalg.inv(K)
    p = g.pitch
    R0 = np.array([[1.0, 0, 0], [0, -math.sin(p), -math.cos(p)], [0, math.cos(p), -math.sin(p)]])  # right, down, optical
    t0 = -R0 @ np.array([0, 0, g.h])
    G = K @ np.column_stack([R0[:, 0], R0[:, 1], t0])  # ground (X, Y, 1) -> reference image
    out = {}
    for n, Hn in homs.items():
        try: A = Ki @ np.linalg.inv(np.asarray(Hn, np.float64)) @ G
        except np.linalg.LinAlgError: continue
        A = A * 2 / (np.linalg.norm(A[:, 0]) + np.linalg.norm(A[:, 1]))
        for sgn in (1, -1):
            r1, r2, t = sgn * A[:, 0], sgn * A[:, 1], sgn * A[:, 2]
            U, _, Vt = np.linalg.svd(np.column_stack([r1, r2, np.cross(r1, r2)]))
            C = -(U @ Vt).T @ t
            if C[2] > 0: out[n] = C; break
    return out


def warp(H, pts):
    p = cv2.perspectiveTransform(np.asarray(pts, np.float64).reshape(-1, 1, 2), np.asarray(H, np.float64))
    return p.reshape(-1, 2)


def width_samples(dets, homs, f, cy, classes, min_conf, obj_w):
    """(normalised row, 1/range) pairs: a box `obj_w` metres wide at w px is at range obj_w f / w."""
    ys, inv_t = [], []
    for n, x1, y1, x2, y2, c, k in dets:
        if int(k) not in classes or c < min_conf or int(n) not in homs: continue
        (ul, vb), (ur, _) = warp(homs[int(n)], [(x1, y2), (x2, y2)])
        w = abs(ur - ul)
        if w < 4: continue
        ys.append((vb - cy) / f); inv_t.append(w / (obj_w * f))
    return np.array(ys), np.array(inv_t)


def line_fit(ys, inv_t):
    """Robust line 1/t = a + b y_c: least squares, then refit on the central 80 % of residuals."""
    A = np.column_stack([np.ones_like(ys), ys])
    coef = np.linalg.lstsq(A, inv_t, rcond=None)[0]
    for _ in range(3):
        r = np.abs(A @ coef - inv_t); keep = r <= np.quantile(r, 0.8)
        coef = np.linalg.lstsq(A[keep], inv_t[keep], rcond=None)[0]
    a_, b_ = coef  # 1/t = sin(p)/h + y_c cos(p)/h
    return math.atan2(a_, b_), 1 / math.hypot(a_, b_)


def fit_ground(dets, homs, f, cx, cy, a):
    """Pitch and height, and how they were obtained. See the module doc."""
    tries = [('people', {0}, 0.35, a.person_width, 20), ('vehicles', VEHICLES, 0.35, a.vehicle_width, 10)]
    # The method with the most boxes fits (people first on a tie): a handful of person boxes must not
    # outvote a thousand vehicles (a consensus-filtered run keeps few pedestrians).
    samples = []
    for method, classes, cmin, obj_w, nmin in tries:
        ys, inv_t = width_samples(dets, homs, f, cy, classes, cmin, obj_w)
        if len(ys) >= nmin: samples.append((len(ys), method, ys, inv_t, obj_w))
    samples.sort(key=lambda x: -x[0])
    for _, method, ys, inv_t, obj_w in samples:
        pitch, h = line_fit(ys, inv_t)
        fixed = False
        if not (math.radians(10) <= pitch <= math.radians(90)) or np.ptp(ys) < 0.05:
            # Rows too few or too close to fit a pitch: assume it and fit only the scale (height).
            pitch, fixed = math.radians(a.assume_pitch), True
            r = np.median(inv_t / (math.sin(pitch) + ys * math.cos(pitch)))
            h = 1 / r
        # Bootstrap the scale (GSD at the image centre) over the boxes.
        rng = np.random.default_rng(0); gs = []
        for _ in range(200):
            i = rng.integers(0, len(ys), len(ys))
            try:
                if fixed: hb, pb = 1 / np.median(inv_t[i] / (math.sin(pitch) + ys[i] * math.cos(pitch))), pitch
                else: pb, hb = line_fit(ys[i], inv_t[i])
                gs.append(hb / math.sin(pb) / f if math.sin(pb) > 0.05 else np.nan)
            except np.linalg.LinAlgError:
                pass
        gs = np.array(gs)[np.isfinite(gs)]
        spread = [float(np.quantile(gs, 0.05)), float(np.quantile(gs, 0.95))] if len(gs) else None
        return pitch, h, {'method': method + (' (pitch assumed)' if fixed else ''), 'boxes': int(len(ys)),
                          'object_width_m': obj_w, 'gsd_centre_m_px_90ci_bootstrap': spread}
    pitch = math.radians(a.assume_pitch); h = a.assume_height
    return pitch, h, {'method': f'assumed: pitch {a.assume_pitch} deg, height {a.assume_height} m (no people or vehicles to fit)',
                      'boxes': 0, 'object_width_m': None, 'gsd_centre_m_px_90ci_bootstrap': None}


# ---- MTI filters on the ground -------------------------------------------------------------------

def expand(b, f):
    """Box grown by `f` of its larger side on every side."""
    m = f * max(b[2] - b[0], b[3] - b[1], 1.0)
    return (b[0] - m, b[1] - m, b[2] + m, b[3] + m)


def merge_adjacent(rows, f):
    """Union of MTI blobs in the same frame whose boxes, grown by `f` of the larger one's size, touch:
    a vehicle and its own shadow (a low sun casts it metres away), or a body the threshold split."""
    if not len(rows) or f <= 0: return rows
    out = []
    for n in np.unique(rows[:, 0]):
        bs = [list(r) for r in rows[rows[:, 0] == n]]
        merged = True
        while merged and len(bs) > 1:
            merged = False
            for i in range(len(bs)):
                for j in range(i + 1, len(bs)):
                    a_, b_ = bs[i], bs[j]
                    big = a_ if (a_[3] - a_[1]) * (a_[4] - a_[2]) >= (b_[3] - b_[1]) * (b_[4] - b_[2]) else b_
                    e = expand(big[1:5], f)
                    o = b_ if big is a_ else a_
                    if o[1] < e[2] and o[3] > e[0] and o[2] < e[3] and o[4] > e[1]:
                        bs[i] = [n, min(a_[1], b_[1]), min(a_[2], b_[2]), max(a_[3], b_[3]), max(a_[4], b_[4]), max(a_[5], b_[5]), a_[6]]
                        del bs[j]; merged = True; break
                if merged: break
        out.extend(bs)
    return np.array(out, np.float64).reshape(-1, rows.shape[1])


def mti_filter(rows, homs, g, a, fps):
    """Size, aspect and persistence-with-net-motion filters for MTI candidates (see mti.py).
    Returns (kept rows with conf replaced by the persistence score, per-stage counts)."""
    counts = {'candidates': len(rows)}
    rows = merge_adjacent(rows, a.mti_merge)
    counts['after_merge'] = len(rows)
    cand = []  # (n, X, Y, row index)
    for i, (n, x1, y1, x2, y2, c, k) in enumerate(rows):
        n = int(n)
        if n not in homs: continue
        (u, v), = warp(homs[n], [((x1 + x2) / 2, (y1 + y2) / 2)])
        X, Y = g.to_ground(u, v)
        if not (math.isfinite(X) and math.isfinite(Y)): continue
        gsd = g.gsd(v)
        wm, hm = (x2 - x1) * gsd, (y2 - y1) * gsd
        big, small = max(wm, hm), max(min(wm, hm), 1e-6)
        if not (a.mti_min_m <= big <= a.mti_max_m): continue
        if big / small > a.mti_max_aspect: continue
        cand.append((n, X, Y, i))
    counts['after_size_aspect'] = len(cand)
    # Persistence: greedy constant-velocity tracklets across consecutive detection frames, in metres.
    byn = {}
    for n, X, Y, i in cand: byn.setdefault(n, []).append((X, Y, i))
    frames = sorted(homs)
    tls = []  # each: list of (t, X, Y, i)
    accepted = {}
    every_dt = (frames[1] - frames[0]) / fps if len(frames) > 1 else 0.2
    for n in frames:
        t = n / fps
        ds = byn.get(n, [])
        live = [tl for tl in tls if t - tl[-1][0] <= (a.mti_max_gap + 1) * every_dt + 1e-6]
        C = np.full((len(live), len(ds)), 1e6)
        for r, tl in enumerate(live):
            lt, lx, ly, _ = tl[-1]; dt = t - lt
            if len(tl) >= 2:
                pt, px, py, _ = tl[-2]; vx, vy = (lx - px) / (lt - pt), (ly - py) / (lt - pt)
                ex, ey = lx + vx * dt, ly + vy * dt; gate = a.mti_gate + 0.5 * math.hypot(vx, vy) * dt
            else:
                ex, ey = lx, ly; gate = a.mti_gate + a.mti_vmax * dt
            for j, (X, Y, i) in enumerate(ds):
                d = math.hypot(X - ex, Y - ey)
                if d <= gate: C[r, j] = d
        used = set()
        if len(live) and len(ds):
            for r, j in zip(*linear_sum_assignment(C)):
                if C[r, j] >= 1e6: continue
                live[r].append((t, ds[j][0], ds[j][1], ds[j][2])); used.add(j)
                tl = live[r]
                win = [p for p in tl if t - p[0] <= a.mti_window + 1e-6]
                if len(win) >= a.mti_min_hits:
                    disp = math.hypot(win[-1][1] - win[0][1], win[-1][2] - win[0][2])
                    if disp >= max(a.mti_min_disp, a.mti_vmin * (win[-1][0] - win[0][0])):
                        accepted[ds[j][2]] = (min(len(win), 10), tl[0][0])
        for j, (X, Y, i) in enumerate(ds):
            if j not in used: tls.append([(t, X, Y, i)])
    counts['after_persistence'] = len(accepted)
    out = []
    for i, (hits, t0) in accepted.items():
        r = list(rows[i][:7])
        # Score: the seed strength, raised to the high band once the tracklet is persistent.
        r[5] = max(0.35, min(0.95, 0.5 * r[5] + 0.06 * hits))
        out.append(r + [t0, hits])  # + when its motion tracklet began (s, video time) and its length
    out.sort(key=lambda r: r[0])
    return np.array(out, np.float64).reshape(-1, 9), counts


# ---- fusion --------------------------------------------------------------------------------------

def overlap(b, c):
    """Intersection area over the smaller box's area."""
    iw = min(b[2], c[2]) - max(b[0], c[0]); ih = min(b[3], c[3]) - max(b[1], c[1])
    if iw <= 0 or ih <= 0: return 0.0
    amin = min((b[2] - b[0]) * (b[3] - b[1]), (c[2] - c[0]) * (c[3] - c[1]))
    return iw * ih / max(amin, 1e-6)


def fuse_frame(app, mti, foot_xy, mti_xy, a):
    """app: list of dicts (appearance, all sources), mti: list of dicts. Returns the fused list.
    Appearance boxes from different sources that overlap merge (the higher confidence one's class
    wins); an MTI box that overlaps an appearance box (grown by `fuse_expand`), or whose centre is
    within the gate of its foot point on the ground, is absorbed (the appearance class wins).
    Sources in `confirm_sources` (a specialist model, e.g. the military one) only label: a box only
    they see is dropped unless motion or another model confirms it, and their class replaces the
    group's only at `label_conf` or above (an armoured label only on a vehicle)."""
    gated = set(a.confirm_sources.split(',')) if a.confirm_sources else set()
    app = sorted(app, key=lambda d: -d['conf'])
    kept = []
    for d in app:
        dup = next((e for e in kept if e['src'] != d['src'] and overlap(e['box'], d['box']) > 0.5), None)
        if dup is not None:
            dup['agree'].add(d['src']); dup['members'].append(d); d['merged_into'] = dup
            dup['agreed'] = dup.get('agreed', True) or d.get('agreed', True); continue
        d['agree'] = {d['src']}; d['members'] = [d]; kept.append(d)
    out = list(kept)
    for m in mti:
        hit = None
        for e in kept:
            if overlap(expand(e['box'], a.fuse_expand), m['box']) > 0.3: hit = e; break
            gate = a.fuse_gate_vehicle if group(e['cls']) == VEHICLE_G else a.fuse_gate_person
            if foot_xy(e) is not None and mti_xy(m) is not None and math.dist(foot_xy(e), mti_xy(m)) <= gate: hit = e; break
        if hit is not None: hit['agree'].add('mti'); m['merged_into'] = hit; continue
        m['agree'] = {'mti'}; out.append(m)
    if gated:
        for e in kept:
            if e['agree'] <= gated:  # only the specialist saw it: no corroboration, not tracked
                e['dropped'] = True; out.remove(e); continue
            spec = [m for m in e['members'] if m['src'] in gated and m['conf'] >= a.label_conf]
            other = [m for m in e['members'] if m['src'] not in gated]
            lab = max(spec, key=lambda m: m['conf']) if spec else None
            if lab is not None and (lab['cls'] != ARMOURED or group(e['cls']) == VEHICLE_G or not other):
                e['cls'] = lab['cls']
            elif e['src'] in gated and other:  # a weak specialist box led the group: the other model's class
                e['cls'] = max(other, key=lambda m: m['conf'])['cls']
            elif e['src'] in gated:  # a weak specialist box confirmed by motion only: a mover, unlabelled
                e['cls'] = MOVER
    return out


# ---- tracker -------------------------------------------------------------------------------------

class KF:
    """Constant-velocity Kalman filter in metres: state x, y, vx, vy."""

    def __init__(self, xy, sigma_a, sigma_z, v0=(0.0, 0.0)):
        self.x = np.array([xy[0], xy[1], v0[0], v0[1]]); self.P = np.diag([sigma_z ** 2] * 2 + [4.0, 4.0])
        self.sa = sigma_a

    def predict(self, dt):
        F = np.eye(4); F[0, 2] = F[1, 3] = dt
        G = np.array([[dt * dt / 2, 0], [0, dt * dt / 2], [dt, 0], [0, dt]])
        self.x = F @ self.x; self.P = F @ self.P @ F.T + G @ G.T * self.sa ** 2

    def update(self, z, sz):
        Hm = np.eye(2, 4); S = Hm @ self.P @ Hm.T + np.eye(2) * sz ** 2
        K = self.P @ Hm.T @ np.linalg.inv(S)
        self.x = self.x + K @ (np.asarray(z) - Hm @ self.x); self.P = (np.eye(4) - K @ Hm) @ self.P


SIGMA_A = {DISMOUNT_G: 1.5, VEHICLE_G: 3.0, MOVER_G: 3.0}   # process noise, m/s^2
SIGMA_Z = {DISMOUNT_G: 0.15, VEHICLE_G: 0.25, MOVER_G: 0.5}  # measurement noise, m (a motion blob's centre is loose)
SIGMA_A_STATIC = 0.05                                        # a parked object: the filter averages, it does not chase jitter
GROWTH = {DISMOUNT_G: 1.0, VEHICLE_G: 3.0, MOVER_G: 3.0}     # gate growth per second without a detection, m/s


class Track:
    def __init__(self, tid, xy, k, conf, t, born=None):
        self.id = tid
        self.g = group(k)
        self.kf = KF(xy, SIGMA_A[self.g], SIGMA_Z[self.g])
        self.hits, self.last, self.born = 1, t, (t if born is None else born)
        self.need = self.birth_hits = 3          # hits before confirmed (cmd_track sets them at birth)
        self.last_sure = t                       # last detection both models agreed on (--sure-hold)
        self.classes = {} if k == MOVER else {int(k): 1}
        self.conf = conf
        self.hist = [(t, xy[0], xy[1])]  # measurements, for the static test
        self.static = False; self.far = 0
        self.lost_at = None
        self.comp = {}; self.companion = False  # lockstep partners (offsets over time), see cmd_track
        self.comp_seen = None                   # last time the lockstep test passed (companion hold)
        self.plx = []; self.parallax = False    # parallax test samples (t, consistent), see cmd_track
        self.sizes = {False: [], True: []}      # recent box sizes on the ground (m): motion blobs, appearance boxes

    def add_size(self, s, k):
        if s is not None and math.isfinite(s):
            app = k != MOVER; self.sizes[app] = (self.sizes[app] + [s])[-15:]

    def size(self):
        """The object's size: its appearance boxes when it has them (a motion blob smears along the motion
        and a classified track also takes motion blobs), else its motion blobs."""
        v = self.sizes[True] or self.sizes[False]
        return float(np.median(v)) if v else None

    def cls(self):
        return max(self.classes, key=self.classes.get) if self.classes else MOVER

    def rider(self, k):
        """A dismount track and a two-wheeler detection, or the reverse: the same rider, labelled either way."""
        gk = group(k)
        return (self.g == DISMOUNT_G and int(k) in TWO_WHEELERS) or (self.g == VEHICLE_G and gk == DISMOUNT_G and self.cls() in TWO_WHEELERS)

    def accepts(self, k, a=None):
        """Same group; a mover track takes any class (promotion); a classified track takes movers; with
        `riders`, a dismount and a two-wheeler take each other."""
        gk = group(k)
        return gk == self.g or self.g == MOVER_G or gk == MOVER_G or (a is not None and a.riders and self.rider(k))

    def gate(self, k, a, t=None, det_dt=0.0):
        """Vehicle gate if either side is a vehicle, person gate if either is a dismount, else (two movers)
        vehicle; grown for the time without a detection (coasting, re-acquisition), up to 3x."""
        gs = {self.g, group(k)}
        if getattr(a, 'riders', False) and self.rider(k): gs = {DISMOUNT_G}
        base = a.gate_vehicle if VEHICLE_G in gs else (a.gate_person if DISMOUNT_G in gs else a.gate_vehicle)
        if t is None or not a.gate_growth: return base
        gap = max(0.0, t - self.last - det_dt)
        kind = MOVER_G if MOVER_G in gs and len(gs) == 1 else (VEHICLE_G if VEHICLE_G in gs else DISMOUNT_G)
        return min(3 * base, base + a.gate_growth * GROWTH[kind] * gap)

    def velocity_fit(self, t, window):
        h = [p for p in self.hist if t - p[0] <= window]
        if len(h) < 4 or h[-1][0] - h[0][0] < 0.5 * window: return None
        T = np.array([p[0] for p in h]); X = np.array([p[1] for p in h]); Y = np.array([p[2] for p in h])
        A = np.column_stack([T - T.mean(), np.ones_like(T)])
        vx = np.linalg.lstsq(A, X, rcond=None)[0][0]; vy = np.linalg.lstsq(A, Y, rcond=None)[0][0]
        return vx, vy

    def update(self, xy, k, conf, t, a=None, agreed=True):
        gk = group(k)
        if gk != MOVER_G:
            if self.g == MOVER_G:  # promotion: the appearance detector has classified the mover
                self.g = gk; self.kf.sa = SIGMA_A_STATIC if self.static else SIGMA_A[gk]
            self.classes[int(k)] = self.classes.get(int(k), 0) + 1
        innov = math.hypot(xy[0] - self.kf.x[0], xy[1] - self.kf.x[1])
        self.kf.update(xy, SIGMA_Z[gk])
        self.hits += 1; self.last = t; self.conf = 0.8 * self.conf + 0.2 * conf
        if agreed: self.last_sure = t
        if agreed and self.need > self.birth_hits: self.need = self.birth_hits  # both models now agree: a normal birth
        self.hist.append((t, xy[0], xy[1]))
        if a is None or not a.static_mode: return
        self.hist = [p for p in self.hist if t - p[0] <= a.static_window]
        vmax = a.static_v_person if self.g == DISMOUNT_G else a.static_v_vehicle
        v = self.velocity_fit(t, a.static_window)
        if not self.static:
            # Static mode: the motion measured over the window is below what jitter alone explains.
            if v is not None and self.g != MOVER_G and math.hypot(*v) < vmax:
                self.static = True; self.kf.sa = SIGMA_A_STATIC; self.kf.x[2:] = 0.0; self.far = 0
        else:
            r_exit = 0.8 * (a.gate_person if self.g == DISMOUNT_G else a.gate_vehicle)
            self.far = self.far + 1 if innov > 0.5 * r_exit else 0
            if self.far >= 2 or innov > r_exit or (v is not None and math.hypot(*v) > 1.5 * vmax):
                self.static = False; self.kf.sa = SIGMA_A[self.g]; self.far = 0
                if v is not None: self.kf.x[2:] = v
                self.kf.P[2:, 2:] += np.eye(2) * 1.0

    def state(self):
        x, y, vx, vy = self.kf.x
        return (x, y, 0.0, 0.0) if self.static else (x, y, vx, vy)


def load_caches(d, sources):
    caches = {}
    for name in sources:
        npy, js = cache_files(d, name)
        if not os.path.exists(npy): sys.exit(f'no {os.path.basename(npy)} in {d} (run track.py {"mti" if name == "mti" else "detect"} first)')
        caches[name] = (np.load(npy), json.load(open(js)))
    return caches


def cmd_track(a):
    sources = [s for s in a.sources.split(',') if s]
    caches = load_caches(a.dir, sources)
    out_dir = a.out or a.dir
    os.makedirs(out_dir, exist_ok=True)
    hsrc = next((s for s in sources if 'homographies' in caches[s][1]), None)
    if hsrc is None:  # MTI alone on top of an appearance run's frames
        hsrc = 'det'; caches_h = json.load(open(cache_files(a.dir, 'det')[1]))
    else:
        caches_h = caches[hsrc][1]
    meta = caches_h
    homs = {int(k): v for k, v in meta['homographies'].items()}
    W, Hh, fps = meta['width'], meta['height'], meta['fps']
    # Rows: the 7 cache columns, the source index, and the birth flag (an 8th cache column written by
    # consensus.py --keep: 1 where two models agreed; 1 everywhere for a plain detector cache).
    app_rows = np.concatenate([np.column_stack([caches[s][0][:, :7], np.full(len(caches[s][0]), i),
                                                caches[s][0][:, 7] if caches[s][0].shape[1] > 7 else np.ones(len(caches[s][0]))])
                               for i, s in enumerate(sources) if s != 'mti'] or [np.zeros((0, 9))])
    f = (W / 2) / math.tan(math.radians(a.hfov / 2))
    pitch, h, fitinfo = fit_ground(app_rows[:, :7], homs, f, W / 2, Hh / 2, a)
    g = Ground(f, W / 2, Hh / 2, pitch, h)
    print(f'ground: f {f:.0f} px, pitch {math.degrees(pitch):.1f} deg, height {h:.1f} m ({fitinfo["method"]}, {fitinfo["boxes"]} boxes); '
          f'GSD at centre {g.gsd(Hh / 2) * 100:.1f} cm/px')

    # The camera's own ground velocity (for the parallax test): its centre at each registered frame,
    # differentiated by a straight-line fit over +-0.5 s.
    cams = camera_centres(homs, g)
    cam_n = np.array(sorted(cams)); cam_t = (cam_n - meta['start_frame']) / fps
    cam_xy = np.array([cams[n][:2] for n in cam_n]).reshape(-1, 2)

    def cam_velocity(t):
        m = np.abs(cam_t - t) <= 0.5
        if m.sum() < 3: return None
        A = np.column_stack([cam_t[m] - t, np.ones(m.sum())])
        return np.linalg.lstsq(A, cam_xy[m], rcond=None)[0][0]

    mti_counts = None
    mti_rows = np.zeros((0, 9))
    if 'mti' in caches:
        mti_rows, mti_counts = mti_filter(caches['mti'][0], homs, g, a, fps)
        print(f'mti: {mti_counts}')

    def ground_of(u, v, n):
        (uu, vv), = warp(homs[n], [(u, v)])
        X, Y = g.to_ground(uu, vv)
        return (X, Y) if math.isfinite(X) and math.isfinite(Y) else None

    def size_of(box, n):
        """Box diagonal on the ground (m) at its centre's range: rotation-invariant, for size ratios."""
        (u1, v1), (u2, v2) = warp(homs[n], [(box[0], box[1]), (box[2], box[3])])
        s = math.hypot(u2 - u1, v2 - v1) * g.gsd((v1 + v2) / 2)
        return s if math.isfinite(s) and s > 0 else None

    # Detections per frame, fused.
    byframe, detlog = {}, []
    app_by = {}; mti_by = {}
    ov_path = os.path.join(a.dir, 'overlay-mask.png')
    ov = cv2.imread(ov_path, cv2.IMREAD_GRAYSCALE) > 0 if os.path.exists(ov_path) and a.overlay else None
    ov_dropped = 0
    for r in app_rows:
        if int(r[0]) not in homs or r[5] < a.low: continue
        if ov is not None:
            x1, y1, x2, y2 = (int(round(v)) for v in r[1:5])
            box = ov[max(0, y1):max(0, y2) + 1, max(0, x1):max(0, x2) + 1]
            if box.size and box.mean() >= 0.5: ov_dropped += 1; continue  # on a static overlay (watermark, HUD)
        app_by.setdefault(int(r[0]), []).append(r)
    for r in mti_rows:
        if int(r[0]) in homs: mti_by.setdefault(int(r[0]), []).append(r)
    for n in sorted(homs):
        app = [{'src': sources[int(r[7])], 'box': r[1:5], 'conf': float(r[5]), 'cls': int(r[6]), 'n': n, 'agreed': bool(r[8])} for r in app_by.get(n, [])]
        mti = [{'src': 'mti', 'box': r[1:5], 'conf': float(r[5]), 'cls': MOVER, 'n': n,
                't0': float(r[7]) - meta['start_frame'] / fps, 'hits0': int(r[8])} for r in mti_by.get(n, [])]
        for d in app: d['xy'] = ground_of((d['box'][0] + d['box'][2]) / 2, d['box'][3], n)  # feet / tyres
        for d in mti: d['xy'] = ground_of((d['box'][0] + d['box'][2]) / 2, (d['box'][1] + d['box'][3]) / 2, n)  # blob centre
        for d in app + mti: d['size'] = size_of(d['box'], n)
        fused = fuse_frame(app, mti, lambda d: d['xy'], lambda d: d['xy'], a)
        for d in app + mti: d['tid'] = 0; detlog.append(d)
        byframe[n] = [d for d in fused if d['xy'] is not None]
    origin = np.median(np.array([d['xy'] for v in byframe.values() for d in v]), axis=0) if any(byframe.values()) else np.zeros(2)

    tracks, lost, out, next_id, alltracks = [], [], [], 1, {}

    def established(tr):
        """Old enough to be held for a long re-acquisition at the place it was last seen."""
        return a.long_reacquire > 0 and (tr.lost_at if tr.lost_at is not None else tr.last) - tr.born >= a.established

    def coast_of(tr):
        """A parked object the detector misses for a few seconds is still there: it may coast longer."""
        return max(a.coast, a.static_coast) if tr.static else a.coast
    f0, f1 = meta['start_frame'], meta['end_frame']
    dt = 1 / fps
    det_dt = meta['every'] / fps
    reacquired = 0
    for n in range(f0, f1 + 1):
        t = (n - f0) / fps
        for tr in tracks + lost: tr.kf.predict(dt)
        if n in homs:
            ds = byframe.get(n, [])
            hi = [d for d in ds if d['conf'] >= a.high]; lo = [d for d in ds if a.low <= d['conf'] < a.high]
            unmatched = list(range(len(tracks)))
            for grp in (hi, lo):
                if not grp or not unmatched: continue
                C = np.full((len(unmatched), len(grp)), 1e6)
                for i, ti in enumerate(unmatched):
                    tr = tracks[ti]; px, py = tr.kf.x[:2]
                    for j, d in enumerate(grp):
                        if not tr.accepts(d['cls'], a): continue
                        dd = math.hypot(d['xy'][0] - px, d['xy'][1] - py)
                        if dd <= tr.gate(d['cls'], a, t, det_dt): C[i, j] = dd
                ri, ci = linear_sum_assignment(C)
                used = set()
                for i, j in zip(ri, ci):
                    if C[i, j] >= 1e6: continue
                    tr = tracks[unmatched[i]]; d = grp[j]
                    tr.update(d['xy'], d['cls'], d['conf'], t, a, d.get('agreed', True)); tr.add_size(d['size'], d['cls']); d['tid'] = tr.id
                    used.add(i); grp[j] = None
                unmatched = [ti for i, ti in enumerate(unmatched) if i not in used]
            rest = [d for d in hi if d is not None]
            # Re-acquisition: a new detection near a recently lost confirmed track continues it (same id)
            # instead of being born again: one object, one id, fewer births.
            if rest and lost:
                C = np.full((len(lost), len(rest)), 1e6)
                for i, tr in enumerate(lost):
                    px, py = tr.kf.x[:2]
                    for j, d in enumerate(rest):
                        if not tr.accepts(d['cls'], a): continue
                        dd = math.hypot(d['xy'][0] - px, d['xy'][1] - py)
                        if (not established(tr) or t - tr.lost_at <= a.reacquire) and dd <= tr.gate(d['cls'], a, t, det_dt): C[i, j] = dd
                        # An established object may also have stopped where it was last seen (a walker who
                        # waits, a car that parks) and be missed for a while: a fixed, tight gate there.
                        if established(tr):
                            sx, sy = (tr.kf.x[0], tr.kf.x[1]) if tr.static else tr.hist[-1][1:3]
                            ds = math.hypot(d['xy'][0] - sx, d['xy'][1] - sy)
                            if ds <= a.stop_gate * tr.gate(d['cls'], a): C[i, j] = min(C[i, j], ds)
                back = set()
                for i, j in zip(*linear_sum_assignment(C)):
                    if C[i, j] >= 1e6: continue
                    tr = lost[i]; d = rest[j]
                    tr.kf.x[:2] = d['xy']; tr.kf.P[:2, :2] = np.eye(2) * SIGMA_Z[tr.g] ** 2
                    tr.update(d['xy'], d['cls'], d['conf'], t, a); tr.add_size(d['size'], d['cls']); d['tid'] = tr.id; tr.lost_at = None
                    tracks.append(tr); back.add(i); rest[j] = None; reacquired += 1
                lost = [tr for i, tr in enumerate(lost) if i not in back]
            for d in rest:  # births from unmatched high-confidence detections
                if d is None: continue
                # With --birth-agreed a track is born only where both models agreed (consensus.py --keep);
                # a box one model alone saw may still start one if --solo-hits > 0, and that track must
                # then be seen --solo-hits times in a row before it is confirmed (a parked object the
                # second model never learned, e.g. a museum tank; a roof vent rarely lasts that long).
                agreed = d.get('agreed', True) or d['src'] == 'mti'
                if a.birth_agreed and not agreed and not a.solo_hits: continue
                tr = Track(next_id, d['xy'], d['cls'], d['conf'], t, born=d.get('t0')); tr.add_size(d['size'], d['cls'])
                tr.birth_hits = a.birth_hits; tr.need = a.birth_hits if agreed or not a.birth_agreed else max(a.birth_hits, a.solo_hits)
                if d.get('hits0'): tr.hits = max(tr.hits, min(3, d['hits0']))  # a persistent motion tracklet
                d['tid'] = next_id; tracks.append(tr); alltracks[next_id] = tr; next_id += 1
            # Tentative tracks that missed die at once; confirmed ones coast up to `coast` s, then wait
            # `reacquire` s in the lost pool.
            keep = []
            for tr in tracks:
                if (t - tr.last) <= (coast_of(tr) if tr.hits >= tr.need else 0.0): keep.append(tr)
                elif tr.hits >= tr.need and a.reacquire > 0: tr.lost_at = t; lost.append(tr)
            tracks = keep
            lost = [tr for tr in lost if t - tr.lost_at <= (a.long_reacquire if established(tr) else a.reacquire)]
            # Companions: a motion-only track that keeps a constant offset from a confirmed track and is
            # much smaller than it is that object's other part: the far end of a long low-sun shadow (a
            # low sun throws a vehicle's shadow tip 10-15 m), or a fragment. Still tracked (so it does
            # not respawn), not reported. Size, not distance, separates it from the next vehicle of a
            # convoy or the next walker of a group, which keep the same lockstep but are about as large.
            # Sizes are box diagonals on the ground; a classified partner's come from its appearance
            # boxes. While a mover has been near a larger partner too briefly for the lockstep test it is
            # held back (pending), and a found companion stays one for `companion_hold` s, so a shadow
            # does not reach the link before or between the windows that recognise it.
            conf_tr = [tr for tr in tracks if tr.hits >= tr.need]
            for tr in conf_tr:
                if tr.classes or a.companion_dist <= 0: tr.companion = False; continue
                ts = tr.size()
                for o in conf_tr:
                    if o is tr or (not o.classes and o.id > tr.id): continue
                    os_ = o.size()
                    if ts is None or os_ is None or ts > a.companion_size * os_: continue
                    dx, dy = tr.kf.x[0] - o.kf.x[0], tr.kf.x[1] - o.kf.x[1]
                    if math.hypot(dx, dy) <= a.companion_dist: tr.comp.setdefault(o.id, []).append((t, dx, dy))
                pending = False
                for pid in list(tr.comp):
                    hq = [q for q in tr.comp[pid] if t - q[0] <= a.companion_window]
                    tr.comp[pid] = hq
                    if not hq: del tr.comp[pid]; continue
                    if hq[-1][0] != t: continue
                    if len(hq) >= 4 and hq[-1][0] - hq[0][0] >= 0.6 * a.companion_window:
                        sd = math.hypot(np.std([q[1] for q in hq]), np.std([q[2] for q in hq]))
                        if sd <= a.companion_std: tr.comp_seen = t
                    elif a.companion_pending:
                        pending = True
                tr.companion = pending or (tr.comp_seen is not None and t - tr.comp_seen <= a.companion_hold)
            # Parallax: under a moving camera, the top of a tall static object (tree, roof edge, mast)
            # slides over the ground plane against the camera's motion, at h / (H - h) of its speed, and
            # the motion detector sees it move. A motion-only track whose velocity stays within
            # `parallax_cos` of anti-parallel to the camera's and below `parallax_k` of its speed, over a
            # window, is tracked but not reported; only while the camera moves faster than
            # `parallax_vcam`. Something driving against the camera's direction that slowly is
            # suppressed too; the appearance detector still reports it if it sees it.
            vc = cam_velocity(t) if a.parallax_k > 0 else None
            for tr in conf_tr:
                if tr.classes or vc is None: tr.parallax = False; continue
                sc = math.hypot(*vc); v = tr.kf.x[2:]; sv = math.hypot(*v)
                if sc >= a.parallax_vcam and sv >= 0.1 * sc:  # a (nearly) still track says nothing about direction
                    ok = sv <= a.parallax_k * sc and -(v @ vc) / (sv * sc) >= a.parallax_cos
                    tr.plx = [q for q in tr.plx if t - q[0] <= a.companion_window] + [(t, ok)]
                hq = [q for q in tr.plx if t - q[0] <= a.companion_window]
                tr.parallax = len(hq) >= 4 and hq[-1][0] - hq[0][0] >= 0.6 * a.companion_window and np.mean([q[1] for q in hq]) >= 0.75
                if tr.parallax: tr.companion = True
        for tr in tracks:
            # Reported once confirmed (3 hits) and `min_age` s old: short spurious tracks never reach the link.
            if tr.hits < tr.need or t - tr.last > coast_of(tr) or t - tr.born < a.min_age or tr.companion: continue
            # With --sure-hold a track that only one model has seen for that long is held, not reported:
            # a roof vent the aerial model keeps calling a car at 0.5 while the COCO model never does.
            if a.sure_hold > 0 and t - tr.last_sure > max(a.sure_hold, coast_of(tr)): continue
            x, y, vx, vy = tr.state()
            # MinBand frame: x right, y up, z toward the camera; forward on the ground is -z.
            out.append((round(t * TICK_HZ), tr.id, tr.cls(), x - origin[0], 0.0, -(y - origin[1]), vx, 0.0, -vy, int(min(255, tr.conf * 255))))
    path = os.path.join(out_dir, 'tracks.csv')
    with open(path, 'w') as fh:
        fh.write('tick,id,class,x,y,z,vx,vy,vz,conf\n')
        for r in out: fh.write(f'{r[0]},{r[1]},{r[2]},{r[3]:.4f},{r[4]:.4f},{r[5]:.4f},{r[6]:.4f},{r[7]:.4f},{r[8]:.4f},{r[9]}\n')

    # Detections absorbed by fusion inherit the track of the detection that absorbed them.
    for d in detlog:
        if d['tid'] == 0 and 'merged_into' in d: d['tid'] = d['merged_into'].get('tid', 0)
    arr = np.array(out, np.float64).reshape(-1, 10)
    per = {}
    for i in np.unique(arr[:, 1]):
        m = arr[arr[:, 1] == i]
        per[int(i)] = (int(np.bincount(m[:, 2].astype(int)).argmax()), len(m) / fps, float(np.median(np.hypot(m[:, 6], m[:, 8]))))
    # Detection log for audit.py: frame, source, class, conf, box, track id, track duration (s) in the output.
    # Columns: frame, source index (summary.json sources), class, conf, x1 y1 x2 y2, track id (0 = none),
    # that track's duration in the output (s), absorbed by fusion (1) or dropped as uncorroborated (2),
    # sources agreeing on the object.
    src_ids = {s: i for i, s in enumerate(sources)}
    def agree(d): return len((d['merged_into'] if 'merged_into' in d else d).get('agree', ()))
    log = np.array([[d['n'], src_ids[d['src']], d['cls'], d['conf'], *d['box'], d['tid'], per.get(d['tid'], (0, 0.0, 0))[1],
                     1.0 if 'merged_into' in d else (2.0 if d.get('dropped') else 0.0), agree(d)] for d in detlog], np.float64).reshape(-1, 12)
    np.save(os.path.join(out_dir, 'detlog.npy'), log)
    mover_tids = {d['tid'] for d in detlog if d['cls'] == MOVER and d['tid']}
    walkers = [s for c, d, s in per.values() if c == 0 and d >= 3 and s > 0.4]
    movers = [s for c, d, s in per.values() if c in VEHICLES and d >= 3 and s > 1.0]
    mover_cls = [s for c, d, s in per.values() if c == MOVER and d >= 1]
    frames = len(np.unique(arr[:, 0])) if len(arr) else 0
    summary = {
        'sources': sources,
        'ground': {'f_px': f, 'hfov_deg': a.hfov, 'pitch_deg': math.degrees(pitch), 'height_m': h, 'person_width_m': a.person_width,
                   'fit': fitinfo, 'fit_boxes': fitinfo['boxes'], 'gsd_centre_cm': g.gsd(Hh / 2) * 100},
        'tracks': len(per), 'dismounts': sum(1 for c, _, _ in per.values() if c == 0),
        'vehicles': sum(1 for c, _, _ in per.values() if c in VEHICLES), 'armoured': sum(1 for c, _, _ in per.values() if c == ARMOURED),
        'movers': sum(1 for c, _, _ in per.values() if c == MOVER),
        # Tracks with both motion-only and appearance detections (born as movers and promoted, or kept alive by motion).
        'motion_and_appearance_tracks': sum(1 for tid in mover_tids if tid in per and alltracks[tid].classes),
        'mean_entities_per_frame': len(arr) / max(frames, 1),
        'walker_median_speed_mps': float(np.median(walkers)) if walkers else None, 'walkers': len(walkers),
        'moving_vehicle_median_speed_mps': float(np.median(movers)) if movers else None, 'moving_vehicles': len(movers),
        'mover_median_speed_mps': float(np.median(mover_cls)) if mover_cls else None,
        'mti': mti_counts, 'overlay_dropped_appearance': ov_dropped, 'reacquired': reacquired,
        'tracker': {k: getattr(a, k) for k in ('coast', 'reacquire', 'min_age', 'static_mode', 'gate_growth', 'gate_person', 'gate_vehicle', 'high', 'low', 'companion_dist', 'companion_size', 'companion_hold', 'companion_pending', 'parallax_k', 'parallax_cos', 'parallax_vcam', 'long_reacquire', 'established', 'stop_gate', 'static_coast', 'riders', 'birth_agreed', 'solo_hits', 'birth_hits', 'sure_hold')},
        'extent_m': [float(np.ptp(arr[:, 3])), float(np.ptp(arr[:, 5]))] if len(arr) else [0, 0],
        'duration_s': (f1 - f0 + 1) / fps, 'rows': len(arr),
        # Where the drone was in the log's frame (its nadir is the ground model's origin): TRACKS_CAMERA for the sim.
        'camera_m': [float(-origin[0]), float(h), float(origin[1])],
    }
    json.dump(summary, open(os.path.join(out_dir, 'summary.json'), 'w'), indent=2)
    print(json.dumps(summary, indent=2)); print(f'wrote {path}')


# ---- preview -------------------------------------------------------------------------------------

def cmd_preview(a):
    """Detection frames with boxes, at the detection rate, 1920 wide: a quick visual check."""
    meta = json.load(open(os.path.join(a.dir, 'detect.json')))
    dets = np.load(os.path.join(a.dir, 'detections.npy'))
    mp = os.path.join(a.dir, 'detections-mti.npy')
    mti = np.load(mp) if os.path.exists(mp) else np.zeros((0, 7))
    cap = cv2.VideoCapture(a.video)
    homs = sorted(int(k) for k in meta['homographies'])
    W, H = meta['width'], meta['height']; ow = min(1920, W); oh = int(round(H * ow / W)) // 2 * 2
    out = cv2.VideoWriter(os.path.join(a.dir, 'preview.mp4'), cv2.VideoWriter_fourcc(*'mp4v'), meta['fps'] / meta['every'], (ow, oh))
    for n in homs:
        cap.set(cv2.CAP_PROP_POS_FRAMES, n); ok, fr = cap.read()
        if not ok: break
        for _, x1, y1, x2, y2, c, k in dets[dets[:, 0] == n]:
            cv2.rectangle(fr, (int(x1), int(y1)), (int(x2), int(y2)), (40, 40, 255) if k == 0 else (255, 200, 40), 3)
        for _, x1, y1, x2, y2, c, k in mti[mti[:, 0] == n]:
            cv2.rectangle(fr, (int(x1), int(y1)), (int(x2), int(y2)), (255, 0, 255), 2)
        out.write(cv2.resize(fr, (ow, oh), interpolation=cv2.INTER_AREA))
    out.release(); print('wrote preview.mp4')


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    s = p.add_subparsers(dest='cmd', required=True)
    d = s.add_parser('detect'); d.add_argument('video'); d.add_argument('--model', required=True); d.add_argument('--out', required=True)
    d.add_argument('--tag', default='det', help='cache name: det (detections.npy) or e.g. mil (detections-mil.npy)')
    d.add_argument('--start', type=float); d.add_argument('--end', type=float); d.add_argument('--every', type=int, default=0)
    d.add_argument('--tile', type=int, default=0, help='tile side in px; 0 = resolution-aware (detect.py tile_for)'); d.add_argument('--conf', type=float, default=0.15)
    d.add_argument('--threads', type=int, default=0, help='ONNX Runtime intra-op threads (0: all cores)')
    m = s.add_parser('mti'); m.add_argument('video'); m.add_argument('--out', required=True)
    m.add_argument('--start', type=float); m.add_argument('--end', type=float); m.add_argument('--every', type=int, default=0)
    m.add_argument('--baseline', type=float, default=0.3, help='seconds between the frames differenced (each side)')
    m.add_argument('--work-width', type=int, default=1920); m.add_argument('--z-seed', type=float, default=5.0)
    m.add_argument('--z-grow', type=float, default=3.0); m.add_argument('--min-area', type=int, default=6)
    t = s.add_parser('track'); t.add_argument('dir')
    t.add_argument('--sources', default='det', help='caches to fuse: det, mti, or a detect --tag (e.g. det,mti)')
    t.add_argument('--out', help='output directory (default: DIR)')
    t.add_argument('--hfov', type=float, default=85.0, help='camera horizontal field of view, degrees')
    t.add_argument('--person-width', type=float, default=0.55, help='box width of a person on the ground, metres')
    t.add_argument('--vehicle-width', type=float, default=3.0, help='box width of a vehicle (any heading), metres, for the fallback fit')
    t.add_argument('--assume-pitch', type=float, default=45.0, help='degrees, when it cannot be fitted')
    t.add_argument('--assume-height', type=float, default=100.0, help='metres, when nothing can be fitted')
    t.add_argument('--high', type=float, default=0.35); t.add_argument('--low', type=float, default=0.15)
    t.add_argument('--gate-person', type=float, default=2.5); t.add_argument('--gate-vehicle', type=float, default=6.0)
    t.add_argument('--coast', type=float, default=2.0, help='s a confirmed track coasts without a detection')
    t.add_argument('--gate-growth', type=float, default=1.0, help='scale of the gate growth while coasting (0: fixed gates)')
    t.add_argument('--reacquire', type=float, default=3.0, help='s a lost track can be re-acquired by a new detection (0: off)')
    t.add_argument('--min-age', type=float, default=1.0, help='s from birth before a track is reported')
    t.add_argument('--no-static-mode', dest='static_mode', action='store_false', help='no static/moving mode switch')
    t.add_argument('--static-window', type=float, default=3.0); t.add_argument('--static-v-person', type=float, default=0.3)
    t.add_argument('--static-v-vehicle', type=float, default=0.5)
    t.add_argument('--long-reacquire', type=float, default=20.0, help='s an established lost track can be re-acquired where it was last seen (0: off)')
    t.add_argument('--established', type=float, default=5.0, help='s of age before a track is held for --long-reacquire')
    t.add_argument('--stop-gate', type=float, default=0.5, help='fraction of the base gate for re-acquisition where a track was last seen')
    t.add_argument('--static-coast', type=float, default=4.0, help='s a static track coasts (reported) without a detection (at least --coast)')
    t.add_argument('--no-riders', dest='riders', action='store_false', help='a dismount track and a two-wheeler detection never associate')
    t.add_argument('--no-overlay', dest='overlay', action='store_false', help='ignore overlay-mask.png for appearance boxes')
    t.add_argument('--birth-agreed', action='store_true', help='a track is born only from a box flagged agreed (consensus.py --keep); any box continues it')
    t.add_argument('--solo-hits', type=int, default=0, help='with --birth-agreed: a box one model alone saw starts a track that needs this many hits in a row (0: never)')
    t.add_argument('--birth-hits', type=int, default=3, help='detections in a row before a track is confirmed')
    t.add_argument('--sure-hold', type=float, default=0.0, help='s without an agreed box after which a track is tracked but not reported (0: off)')
    t.add_argument('--legacy-tracker', action='store_true', help='the original tracker: coast 1 s, fixed gates, no re-acquisition, no min age, no static mode')
    t.add_argument('--fuse-gate-person', type=float, default=1.5); t.add_argument('--fuse-gate-vehicle', type=float, default=4.0)
    t.add_argument('--confirm-sources', default='mil', help='sources that only label: their boxes need motion or another model to be tracked')
    t.add_argument('--label-conf', type=float, default=0.5, help="a confirm-source's class replaces the group's at this confidence or above")
    t.add_argument('--companion-dist', type=float, default=20.0, help='m; a lockstep motion-only track this close to a much larger one is not reported (0: off)')
    t.add_argument('--companion-size', type=float, default=0.5, help='"much smaller": size ratio (box diagonals on the ground) at or below this')
    t.add_argument('--companion-std', type=float, default=0.6); t.add_argument('--companion-window', type=float, default=1.5)
    t.add_argument('--companion-hold', type=float, default=1.5, help='s the companion flag holds after the last lockstep window')
    t.add_argument('--no-companion-pending', dest='companion_pending', action='store_false',
                   help='report a motion-only track near a larger partner before the lockstep test can decide')
    t.add_argument('--parallax-k', type=float, default=0.6, help='parallax test: track speed below this fraction of the camera speed (0: off)')
    t.add_argument('--parallax-cos', type=float, default=0.5, help='parallax test: cosine to the anti-camera direction at or above this')
    t.add_argument('--parallax-vcam', type=float, default=1.0, help='m/s; the parallax test runs only while the camera moves faster')
    t.add_argument('--mti-merge', type=float, default=0.25, help='merge MTI blobs whose boxes, grown by this fraction of the larger, touch')
    t.add_argument('--fuse-expand', type=float, default=0.5, help='appearance box grown by this fraction of its size when absorbing MTI blobs')
    t.add_argument('--mti-min-m', type=float, default=0.3); t.add_argument('--mti-max-m', type=float, default=25.0)
    t.add_argument('--mti-max-aspect', type=float, default=6.0)
    t.add_argument('--mti-gate', type=float, default=1.5, help='persistence: tracklet gate, metres (+ motion)')
    t.add_argument('--mti-vmax', type=float, default=25.0); t.add_argument('--mti-max-gap', type=int, default=1)
    t.add_argument('--mti-min-hits', type=int, default=3); t.add_argument('--mti-window', type=float, default=2.0)
    t.add_argument('--mti-min-disp', type=float, default=0.5); t.add_argument('--mti-vmin', type=float, default=0.3)
    v = s.add_parser('preview'); v.add_argument('dir'); v.add_argument('video')
    a = p.parse_args()
    if getattr(a, 'legacy_tracker', False):
        a.coast, a.gate_growth, a.reacquire, a.min_age, a.static_mode, a.companion_dist, a.parallax_k = 1.0, 0.0, 0.0, 0.0, False, 0.0, 0.0
        a.long_reacquire, a.static_coast, a.riders = 0.0, 0.0, False
    {'detect': cmd_detect, 'mti': cmd_mti, 'track': cmd_track, 'preview': cmd_preview}[a.cmd](a)


if __name__ == '__main__':
    main()
