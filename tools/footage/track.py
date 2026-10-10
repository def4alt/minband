"""Drone footage -> MinBand track log (the CSV ios/MinBand/GroundTruthLog.swift writes).

What a drone edge would do without ARKit: detect objects in each frame, remove the camera's own
motion, put every detection on the ground in metres, and track it with a constant-velocity Kalman
filter at the frame rate. The output feeds `tools/eval` (`--gt`) and the sim's track replay
(`TRACKS=` in server/src/sim.ts) exactly like a phone log.

  python track.py detect  VIDEO --model M.onnx --start 15 --end 105 --out DIR   # slow: detector
  python track.py track   DIR                                                  # ground fit, tracker, CSV
  python track.py preview DIR VIDEO                                            # annotated mp4

Stages:
- detect: every `--every`-th frame (default 6, ~5 Hz at 29.97 fps; the phone runs its detector at
  ~12 Hz and its tracker at 30 Hz) the tiled detector (detect.py) finds people and vehicles, and an
  ORB + RANSAC homography maps the frame onto a reference frame (the first one), so a hovering or
  drifting drone does not turn into moving objects. Direct registration to the reference is tried
  first; when the view has moved too far, the previous frame's chain is used.
- ground: a flat-ground pinhole camera (focal length from the camera's field of view) whose pitch and
  height are fitted from people's box widths: 1/t = (sin(pitch) + y_c cos(pitch)) / h is linear in the
  normalised image row y_c, where t = W f / w_px is the range a person of width W must be at. The
  walkers' median speed is printed as a check (people walk at ~1.3-1.4 m/s).
- track: two-stage (high, then low confidence) Hungarian association in metres with class groups
  (dismounts, vehicles), birth after 3 hits, coasting for up to 1 s without a detection, KF state
  reported at every video frame.
"""
import argparse, json, math, os, sys
import numpy as np
import cv2
from scipy.optimize import linear_sum_assignment

TICK_HZ = 120
VEHICLES = {1, 2, 3, 5, 7}  # bicycle, car, motorcycle, bus, truck (COCO)


# ---- detect --------------------------------------------------------------------------------------

class Registrar:
    """Homographies from each frame to the reference frame, at `scale` for speed."""

    def __init__(self, ref: np.ndarray, scale: float = 0.5):
        self.scale = scale
        self.orb = cv2.ORB_create(4000)
        self.bf = cv2.BFMatcher(cv2.NORM_HAMMING)
        self.ref = self.features(ref)
        self.prev = self.ref; self.prev_H = np.eye(3)

    def features(self, frame):
        g = cv2.cvtColor(cv2.resize(frame, None, fx=self.scale, fy=self.scale, interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
        return self.orb.detectAndCompute(g, None)

    def match(self, a, b):
        """Homography mapping points of `a` onto `b` (full-resolution pixels), inlier count."""
        (ka, da), (kb, db) = a, b
        if da is None or db is None or len(ka) < 50 or len(kb) < 50: return None, 0
        m = [p for p, q in (x for x in self.bf.knnMatch(da, db, k=2) if len(x) == 2) if p.distance < 0.75 * q.distance]
        if len(m) < 40: return None, 0
        src = np.float32([ka[x.queryIdx].pt for x in m]) / self.scale
        dst = np.float32([kb[x.trainIdx].pt for x in m]) / self.scale
        H, inl = cv2.findHomography(src, dst, cv2.RANSAC, 3.0)
        return H, int(inl.sum()) if inl is not None else 0

    def __call__(self, frame):
        cur = self.features(frame)
        H, n = self.match(cur, self.ref)
        how = 'ref'
        if H is None or n < 150:  # chain through the previous registered frame
            Hp, n2 = self.match(cur, self.prev)
            if Hp is None: raise RuntimeError('registration failed')
            H, n, how = self.prev_H @ Hp, n2, 'chain'
        self.prev, self.prev_H = cur, H
        return H, n, how


def cmd_detect(a):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from detect import TiledDetector
    det = TiledDetector(a.model, tile=a.tile, overlap=64, conf=a.conf)
    cap = cv2.VideoCapture(a.video)
    fps = cap.get(cv2.CAP_PROP_FPS); w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)); h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    f0 = int(round(a.start * fps)); f1 = int(round(a.end * fps))
    cap.set(cv2.CAP_PROP_POS_FRAMES, f0)
    os.makedirs(a.out, exist_ok=True)
    reg = None; rows = []; homs = {}; n = f0
    while n <= f1:
        ok, frame = cap.read()
        if not ok: break
        if (n - f0) % a.every == 0:
            if reg is None:
                reg = Registrar(frame); H, ninl, how = np.eye(3), -1, 'ref'
                cv2.imwrite(os.path.join(a.out, 'reference.jpg'), frame)
            else:
                H, ninl, how = reg(frame)
            homs[n] = H.tolist()
            for x1, y1, x2, y2, c, k in det(frame): rows.append([n, x1, y1, x2, y2, c, k])
            print(f'frame {n} ({(n - f0) / fps:6.1f} s): {sum(1 for r in rows if r[0] == n):3d} boxes, H {how} {ninl} inliers', flush=True)
        n += 1
    np.save(os.path.join(a.out, 'detections.npy'), np.array(rows, np.float64).reshape(-1, 7))
    json.dump({'video': os.path.basename(a.video), 'fps': fps, 'width': w, 'height': h, 'start_frame': f0, 'end_frame': n - 1,
               'every': a.every, 'model': os.path.basename(a.model), 'tile': a.tile, 'conf': a.conf, 'homographies': homs},
              open(os.path.join(a.out, 'detect.json'), 'w'))


# ---- ground model ---------------------------------------------------------------------------------

class Ground:
    """Flat ground seen by a pinhole camera pitched down by `pitch`, `h` metres up, no roll."""

    def __init__(self, f, cx, cy, pitch, h):
        self.f, self.cx, self.cy, self.pitch, self.h = f, cx, cy, pitch, h

    def range_t(self, v):
        yc = (v - self.cy) / self.f
        den = math.sin(self.pitch) + yc * math.cos(self.pitch)
        return self.h / den if den > 1e-3 else float('nan')

    def to_ground(self, u, v):
        """Reference-image pixel -> (lateral X, forward Y) metres on the ground."""
        t = self.range_t(v)
        xc, yc = (u - self.cx) / self.f, (v - self.cy) / self.f
        return t * xc, t * (math.cos(self.pitch) - yc * math.sin(self.pitch))


def warp(H, pts):
    p = cv2.perspectiveTransform(np.asarray(pts, np.float64).reshape(-1, 1, 2), np.asarray(H, np.float64))
    return p.reshape(-1, 2)


def fit_ground(dets, homs, f, cx, cy, person_w):
    """Fit pitch and height from people's box widths in the reference image (see the module doc)."""
    ys, inv_t = [], []
    for n, x1, y1, x2, y2, c, k in dets:
        if int(k) != 0 or c < 0.35: continue
        (ul, vb), (ur, _) = warp(homs[int(n)], [(x1, y2), (x2, y2)])
        w = abs(ur - ul)
        if w < 4: continue
        ys.append((vb - cy) / f); inv_t.append(w / (person_w * f))
    ys, inv_t = np.array(ys), np.array(inv_t)
    # Robust line fit: least squares, then refit on the central 80 % of residuals.
    A = np.column_stack([np.ones_like(ys), ys])
    coef = np.linalg.lstsq(A, inv_t, rcond=None)[0]
    for _ in range(3):
        r = np.abs(A @ coef - inv_t); keep = r <= np.quantile(r, 0.8)
        coef = np.linalg.lstsq(A[keep], inv_t[keep], rcond=None)[0]
    a_, b_ = coef  # 1/t = sin(p)/h + y_c cos(p)/h
    pitch = math.atan2(a_, b_); h = 1 / math.hypot(a_, b_)
    return pitch, h, len(ys)


# ---- tracker --------------------------------------------------------------------------------------

class KF:
    """Constant-velocity Kalman filter in metres: state x, y, vx, vy."""

    def __init__(self, xy, sigma_a, sigma_z):
        self.x = np.array([xy[0], xy[1], 0.0, 0.0]); self.P = np.diag([sigma_z ** 2] * 2 + [4.0, 4.0])
        self.sa, self.sz = sigma_a, sigma_z

    def predict(self, dt):
        F = np.eye(4); F[0, 2] = F[1, 3] = dt
        G = np.array([[dt * dt / 2, 0], [0, dt * dt / 2], [dt, 0], [0, dt]])
        self.x = F @ self.x; self.P = F @ self.P @ F.T + G @ G.T * self.sa ** 2

    def update(self, z):
        Hm = np.eye(2, 4); S = Hm @ self.P @ Hm.T + np.eye(2) * self.sz ** 2
        K = self.P @ Hm.T @ np.linalg.inv(S)
        self.x = self.x + K @ (np.asarray(z) - Hm @ self.x); self.P = (np.eye(4) - K @ Hm) @ self.P


class Track:
    def __init__(self, tid, xy, k, conf, t, vehicle):
        self.id, self.vehicle = tid, vehicle
        self.kf = KF(xy, 3.0 if vehicle else 1.5, 0.25 if vehicle else 0.15)
        self.hits, self.last, self.born = 1, t, t
        self.classes = {int(k): 1}; self.conf = conf

    def cls(self): return max(self.classes, key=self.classes.get)


def cmd_track(a):
    meta = json.load(open(os.path.join(a.dir, 'detect.json')))
    dets = np.load(os.path.join(a.dir, 'detections.npy'))
    homs = {int(k): v for k, v in meta['homographies'].items()}
    W, Hh, fps = meta['width'], meta['height'], meta['fps']
    # Focal length from the camera's horizontal field of view (DJI Zenmuse X3 in 16:9 UHD: ~85 deg).
    f = (W / 2) / math.tan(math.radians(a.hfov / 2))
    pitch, h, nfit = fit_ground(dets, homs, f, W / 2, Hh / 2, a.person_width)
    g = Ground(f, W / 2, Hh / 2, pitch, h)
    print(f'ground: f {f:.0f} px, pitch {math.degrees(pitch):.1f} deg, height {h:.1f} m (from {nfit} person boxes); '
          f'GSD at centre {g.range_t(Hh / 2) * math.cos(0) / f * 100:.1f} cm/px')

    # Detections on the ground, grouped by frame.
    byframe = {}
    for n, x1, y1, x2, y2, c, k in dets:
        (u, v), = warp(homs[int(n)], [((x1 + x2) / 2, y2)])  # feet / tyres: bottom centre of the box
        X, Y = g.to_ground(u, v)
        if not (math.isfinite(X) and math.isfinite(Y)): continue
        byframe.setdefault(int(n), []).append((X, Y, float(c), int(k)))
    origin = np.median(np.array([(d[0], d[1]) for v in byframe.values() for d in v]), axis=0)

    tracks, out, next_id = [], [], 1
    det_frames = sorted(homs)
    f0, f1 = meta['start_frame'], meta['end_frame']
    dt = 1 / fps
    for n in range(f0, f1 + 1):
        t = (n - f0) / fps
        for tr in tracks: tr.kf.predict(dt)
        if n in homs:
            ds = byframe.get(n, [])
            hi = [d for d in ds if d[2] >= a.high]; lo = [d for d in ds if a.low <= d[2] < a.high]
            unmatched = list(range(len(tracks)))
            for group in (hi, lo):
                if not group or not unmatched: continue
                C = np.full((len(unmatched), len(group)), 1e6)
                for i, ti in enumerate(unmatched):
                    tr = tracks[ti]; px, py = tr.kf.x[:2]
                    for j, (X, Y, c, k) in enumerate(group):
                        if (k in VEHICLES) != tr.vehicle: continue
                        d = math.hypot(X - px, Y - py)
                        if d <= (a.gate_vehicle if tr.vehicle else a.gate_person): C[i, j] = d
                ri, ci = linear_sum_assignment(C)
                used = set()
                for i, j in zip(ri, ci):
                    if C[i, j] >= 1e6: continue
                    tr = tracks[unmatched[i]]; X, Y, c, k = group[j]
                    tr.kf.update((X, Y)); tr.hits += 1; tr.last = t; tr.conf = 0.8 * tr.conf + 0.2 * c
                    tr.classes[k] = tr.classes.get(k, 0) + 1; used.add(i); group[j] = None
                unmatched = [ti for i, ti in enumerate(unmatched) if i not in used]
            for d in hi:  # births from unmatched high-confidence detections
                if d is None: continue
                X, Y, c, k = d
                tracks.append(Track(next_id, (X, Y), k, c, t, k in VEHICLES)); next_id += 1
            # Tentative tracks that missed die at once; confirmed ones coast up to `coast` s.
            tracks = [tr for tr in tracks if (t - tr.last) <= (a.coast if tr.hits >= 3 else 0.0)]
        for tr in tracks:
            if tr.hits < 3 or t - tr.last > a.coast: continue
            x, y, vx, vy = tr.kf.x
            # MinBand frame: x right, y up, z toward the camera; forward on the ground is -z.
            out.append((round(t * TICK_HZ), tr.id, tr.cls(), x - origin[0], 0.0, -(y - origin[1]), vx, 0.0, -vy, int(min(255, tr.conf * 255))))
    path = os.path.join(a.dir, 'tracks.csv')
    with open(path, 'w') as fh:
        fh.write('tick,id,class,x,y,z,vx,vy,vz,conf\n')
        for r in out: fh.write(f'{r[0]},{r[1]},{r[2]},{r[3]:.4f},{r[4]:.4f},{r[5]:.4f},{r[6]:.4f},{r[7]:.4f},{r[8]:.4f},{r[9]}\n')
    # Checks: speeds of moving dismounts, how many tracks of each kind, extent.
    arr = np.array(out, np.float64)
    ids = np.unique(arr[:, 1]); per = {}
    for i in ids:
        m = arr[arr[:, 1] == i]; per[int(i)] = (int(m[0, 2]), len(m) / fps, float(np.median(np.hypot(m[:, 6], m[:, 8]))))
    walkers = [s for c, d, s in per.values() if c == 0 and d >= 3 and s > 0.4]
    movers = [s for c, d, s in per.values() if c in VEHICLES and d >= 3 and s > 1.0]
    frames = len(np.unique(arr[:, 0]))
    summary = {
        'ground': {'f_px': f, 'hfov_deg': a.hfov, 'pitch_deg': math.degrees(pitch), 'height_m': h, 'person_width_m': a.person_width, 'fit_boxes': nfit,
                   'gsd_centre_cm': g.range_t(Hh / 2) / f * 100},
        'tracks': len(per), 'dismounts': sum(1 for c, _, _ in per.values() if c == 0), 'vehicles': sum(1 for c, _, _ in per.values() if c in VEHICLES),
        'mean_entities_per_frame': len(arr) / max(frames, 1),
        'walker_median_speed_mps': float(np.median(walkers)) if walkers else None, 'walkers': len(walkers),
        'moving_vehicle_median_speed_mps': float(np.median(movers)) if movers else None, 'moving_vehicles': len(movers),
        'extent_m': [float(np.ptp(arr[:, 3])), float(np.ptp(arr[:, 5]))], 'duration_s': (f1 - f0 + 1) / fps, 'rows': len(arr),
        # Where the drone was in the log's frame (its nadir is the ground model's origin): TRACKS_CAMERA for the sim.
        'camera_m': [float(-origin[0]), float(h), float(origin[1])],
    }
    json.dump(summary, open(os.path.join(a.dir, 'summary.json'), 'w'), indent=2)
    print(json.dumps(summary, indent=2)); print(f'wrote {path}')


# ---- preview --------------------------------------------------------------------------------------

def cmd_preview(a):
    """Detection frames with boxes, at 5 fps, 1920 wide: a quick visual check of the detector."""
    meta = json.load(open(os.path.join(a.dir, 'detect.json')))
    dets = np.load(os.path.join(a.dir, 'detections.npy'))
    cap = cv2.VideoCapture(a.video)
    homs = sorted(int(k) for k in meta['homographies'])
    out = cv2.VideoWriter(os.path.join(a.dir, 'preview.mp4'), cv2.VideoWriter_fourcc(*'mp4v'), meta['fps'] / meta['every'], (1920, 1080))
    for n in homs:
        cap.set(cv2.CAP_PROP_POS_FRAMES, n); ok, fr = cap.read()
        if not ok: break
        for _, x1, y1, x2, y2, c, k in dets[dets[:, 0] == n]:
            cv2.rectangle(fr, (int(x1), int(y1)), (int(x2), int(y2)), (40, 40, 255) if k == 0 else (255, 200, 40), 3)
        out.write(cv2.resize(fr, (1920, 1080), interpolation=cv2.INTER_AREA))
    out.release(); print('wrote preview.mp4')


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    s = p.add_subparsers(dest='cmd', required=True)
    d = s.add_parser('detect'); d.add_argument('video'); d.add_argument('--model', required=True); d.add_argument('--out', required=True)
    d.add_argument('--start', type=float, default=0); d.add_argument('--end', type=float, default=60); d.add_argument('--every', type=int, default=6)
    d.add_argument('--tile', type=int, default=640); d.add_argument('--conf', type=float, default=0.15)
    t = s.add_parser('track'); t.add_argument('dir')
    t.add_argument('--hfov', type=float, default=85.0, help='camera horizontal field of view, degrees')
    t.add_argument('--person-width', type=float, default=0.55, help='box width of a person on the ground, metres')
    t.add_argument('--high', type=float, default=0.35); t.add_argument('--low', type=float, default=0.15)
    t.add_argument('--gate-person', type=float, default=2.5); t.add_argument('--gate-vehicle', type=float, default=6.0)
    t.add_argument('--coast', type=float, default=1.0)
    v = s.add_parser('preview'); v.add_argument('dir'); v.add_argument('video')
    a = p.parse_args()
    {'detect': cmd_detect, 'track': cmd_track, 'preview': cmd_preview}[a.cmd](a)


if __name__ == '__main__':
    main()
