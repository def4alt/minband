"""edge/edge.py -- P4 real-time MinBand edge pipeline for a static-camera drone leg.

detect (ClikaTiledDetector) -> track (simple nearest-neighbor, scipy unavailable in
.venv -- see note below) -> ground-project (shared/geom.py) -> emit two UDP streams:

1. Track datagrams (internal seam, NOT a frozen spec -- P5a should treat this as a draft
   to port into the real BLE wire format, core/src/wire.rs's Message::Delta/Keyframe, not
   consume it as-is long term). One JSON line per processed frame, sent to
   127.0.0.1:<--track-port>, shape:
   {"t": <wall ms>, "tick": <frame idx>,
   "pose": {"pos": [x, y, z], "quat": [x, y, z, w]},
   "tracks": [{"id": int, "cls": int,
   "pos": [x, y, z], "vel": [vx, vy, vz], "conf": float, "age": int}, ...]}
   pos/vel are in the single MinBand world frame (shared/geom.py: right-handed, metres,
   +Y up, ground_y=0) -- y is always 0.0 here (ground-plane tracking), kept as a 3-vector
   so a future metric-depth upgrade can fill it in without changing the schema.
   "pose" is the camera's own pose in that same frame (quaternion w-last, as on the wire);
   server/src/edge-bridge.ts forwards it as a Message::Pose so the viewer can draw this
   device's frustum and fly the operator's camera to it.

2. Depth keyframe chunks: wire format FROZEN by the P4 plan, documented in full in
   proto/DEPTH.md. Sent to 127.0.0.1:<--depth-port>.

Scipy finding (P4): still not installed in .venv (`import scipy` raises ModuleNotFoundError
in this venv), so tools/footage/track.py's KF/Track (which import scipy.optimize at module
scope) cannot be imported. Per the phase brief this is not this phase's job to fix --
implemented `_Tracker` below instead: single-hypothesis greedy nearest-neighbor in world
metres, same birth-after-3-hits / coast-then-drop shape as track.py's Track, no Hungarian
assignment (greedy is fine at the target counts this pipeline sees).
"""
import argparse
import json
import math
import socket
import struct
import sys
import time
from pathlib import Path

import cv2
import numpy as np

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "tools" / "footage"))
import clika_runtime as crt
from clika_runtime.modelverse import LoadOptions, ModelRegistry
from shared import geom
from shared.pose_source import static_adapter

from detector import ClikaTiledDetector, check_device

DEFAULT_CLIP = str(_REPO / "tools" / "footage" / "clips" / "2018-03-13.16-00-14.16-03-38.uav1.mp4")
DEFAULT_CACHE = str(_REPO / "tools" / "footage" / "models" / "clika")  # absolute: detector.py's
# own --cache-dir default is relative to cwd, a trap if edge.py is invoked from elsewhere.
# shared/geom.py works in the OpenCV camera frame (+Z forward, +Y down), and so do the depth
# chunks hub/recon.py reassembles -- both stay in it. The wire's Pose does not: the viewer reads
# it into three.js cameras and frustumGeo(), which look down -Z with +Y up. That is also the
# frame ARCore's camera.pose uses, so the phone leg already sends -Z and only this leg needs the
# flip. Without it the clip leg renders pitched 80 deg *up* and its frustum points at the sky.
CV_TO_GL = np.diag([1.0, -1.0, -1.0])

DET_MODEL = "ustc-community/dfine-nano-coco"
DEPTH_MODEL = "depth-anything/Depth-Anything-V2-Small-hf"

DET_RES = 1080          # chosen operating point (reports/CLIKA_SC1.md): detector on a
                         # 1080p-downscaled copy of the frame, tile=0 (auto, 8 tiles), ~1.9 Hz.
DEPTH_RES_W = 640        # depth model fed a 640px-wide downscale, ~5.6 Hz measured.
MAX_GROUND_RANGE_M = 200.0   # ray/ground hit farther than this = treat as horizon-noise,
                             # fall back to depth.
DEPTH_NEAR_M, DEPTH_FAR_M = 3.0, 150.0  # heuristic relative-depth -> metres clamp (no metric
                                        # calibration available -- see _depth_fallback_pos).
POSE_FLOW_W = 480        # width of the greyscale copy _PoseTracker runs optical flow on.

# --- Depth keyframe wire format (frozen, see proto/DEPTH.md) ---------------------------
DEPTH_KIND = 0x01
DEPTH_W, DEPTH_H = 64, 48
ROWS_PER_CHUNK = 2
QUAT_SCALE = 32767.0
POS_SCALE = 10.0  # decimetres per metre
_HEADER = struct.Struct("<BBHBBBBB")  # kind,drone_id,kf_id,row0,nrows,w,h,flags = 9 B


def encode_depth_chunk(drone_id, kf_id, row0, rows_u8, quat_wlast, pos_m, dmin, dmax):
    """One depth-keyframe chunk, <=200 B (155 B at the chosen 64x48/2-row-chunk size)."""
    nrows, w = rows_u8.shape
    flags = 0b01  # bit0 has_position=1 (static adapter always has p); bit1 metric_depth=0
    header = _HEADER.pack(DEPTH_KIND, drone_id & 0xFF, kf_id & 0xFFFF, row0, nrows, w, DEPTH_H, flags)
    q = np.clip(np.round(np.asarray(quat_wlast, dtype=np.float64) * QUAT_SCALE), -32767, 32767).astype("<i2")
    pos = np.clip(np.round(np.asarray(pos_m, dtype=np.float64) * POS_SCALE), -32767, 32767).astype("<i2")
    body = (header + q.tobytes() + pos.tobytes()
            + np.float16(dmin).tobytes() + np.float16(dmax).tobytes()
            + np.ascontiguousarray(rows_u8, dtype=np.uint8).tobytes())
    return body


def decode_depth_chunk(buf):
    kind, drone_id, kf_id, row0, nrows, w, h, flags = _HEADER.unpack(buf[:9])
    off = 9
    quat = np.frombuffer(buf, dtype="<i2", count=4, offset=off) / QUAT_SCALE; off += 8
    pos = np.frombuffer(buf, dtype="<i2", count=3, offset=off) / POS_SCALE; off += 6
    dmin = float(np.frombuffer(buf, dtype="<f2", count=1, offset=off)[0]); off += 2
    dmax = float(np.frombuffer(buf, dtype="<f2", count=1, offset=off)[0]); off += 2
    rows = np.frombuffer(buf, dtype=np.uint8, count=nrows * w, offset=off).reshape(nrows, w)
    return dict(kind=kind, drone_id=drone_id, kf_id=kf_id, row0=row0, nrows=nrows, w=w, h=h,
                flags=flags, quat=quat, pos=pos, dmin=dmin, dmax=dmax, rows=rows)


class DepthKeyframer:
    """Decides when a new depth keyframe is due: 10 s max age, or mean-abs depth change over
    threshold vs the last keyframe. Pose is static (static_adapter never changes K/R/p), so
    the yaw/position-bin trigger the plan also lists never fires in this phase -- noted, not
    implemented, since it would be dead code against this pose source."""

    def __init__(self, max_age_s=10.0, change_frac=0.08):
        self.kf_id = 0
        self.last_emit_t = None
        self.last_map = None
        self.max_age_s = max_age_s
        self.change_frac = change_frac

    def due(self, depth_map, t):
        if self.last_map is None or self.last_emit_t is None:
            return True
        if t - self.last_emit_t >= self.max_age_s:
            return True
        rng = max(float(depth_map.max() - depth_map.min()), 1e-6)
        mean_abs = float(np.mean(np.abs(depth_map.astype(np.float32) - self.last_map.astype(np.float32))))
        return (mean_abs / rng) > self.change_frac

    def emit(self, depth_map, t):
        self.kf_id = (self.kf_id + 1) & 0xFFFF
        self.last_emit_t = t
        self.last_map = depth_map.copy()
        return self.kf_id


class _Track:
    __slots__ = ("id", "cls", "pos", "vel", "last_t", "hits", "misses")

    def __init__(self, tid, cls, pos, t):
        self.id, self.cls = tid, cls
        self.pos = np.array(pos, dtype=np.float64)
        self.vel = np.zeros(3)
        self.last_t = t
        self.hits, self.misses = 1, 0

    def predict(self, t):
        return self.pos + self.vel * (t - self.last_t)

    def update(self, pos, t):
        dt = max(t - self.last_t, 1e-3)
        new_vel = (np.array(pos, dtype=np.float64) - self.pos) / dt
        self.vel = 0.5 * self.vel + 0.5 * new_vel  # light smoothing, no real Kalman gain
        self.pos = np.array(pos, dtype=np.float64)
        self.last_t = t
        self.hits += 1
        self.misses = 0


class _Tracker:
    """Single-hypothesis greedy nearest-neighbor tracker in world metres (ground plane).
    Fallback for tools/footage/track.py's scipy-based KF/Track (scipy not installed, see
    module docstring). Birth after 3 hits, coast up to 5 misses before dropping, same shape
    as track.py's thresholds -- not a byte-for-byte port."""

    GATE_M = 5.0
    BIRTH_HITS = 3
    MAX_MISSES = 5

    def __init__(self):
        self._tracks = []
        self._next_id = 1

    def step(self, detections, t):
        """``detections``: list of (cls, pos3) world positions. Returns confirmed tracks."""
        unmatched_tracks = list(self._tracks)
        unmatched_dets = list(range(len(detections)))
        pairs = []
        for ti, tr in enumerate(self._tracks):
            pred = tr.predict(t)
            for di in unmatched_dets:
                cls, pos = detections[di]
                if cls != tr.cls:
                    continue
                d = float(np.linalg.norm(pred - np.asarray(pos)))
                if d <= self.GATE_M:
                    pairs.append((d, ti, di))
        pairs.sort(key=lambda x: x[0])
        used_t, used_d = set(), set()
        for d, ti, di in pairs:
            if ti in used_t or di in used_d:
                continue
            used_t.add(ti); used_d.add(di)
            cls, pos = detections[di]
            self._tracks[ti].update(pos, t)
        for ti, tr in enumerate(self._tracks):
            if ti not in used_t:
                tr.misses += 1
        for di in range(len(detections)):
            if di not in used_d:
                cls, pos = detections[di]
                self._tracks.append(_Track(self._next_id, cls, pos, t))
                self._next_id += 1
        self._tracks = [tr for tr in self._tracks if tr.misses <= self.MAX_MISSES]
        return [tr for tr in self._tracks if tr.hits >= self.BIRTH_HITS]


class _PoseTracker:
    """Per-frame camera rotation, integrated from frame-to-frame optical flow.

    The clip carries no pose log -- no .SRT or .CSV exists for it -- and
    ``shared/pose_source.py``'s ``refine_with_track_py`` needs scipy, which this venv does not
    have. That left ``static_adapter``'s seeded pose as the only thing wired up, and a constant
    R meant the twin stood still while the footage panned. Rotation, unlike position, is
    recoverable from the imagery alone: track corners between consecutive frames, fit a
    similarity, and read the principal point's own displacement as yaw and pitch and the
    in-plane angle as roll.

    Position is NOT recoverable -- a monocular rotation estimate carries no scale -- so it stays
    at the seeded height from ``_SEED_TABLE``.

    ponytail: this integrates, so it drifts; there is no loop closure and no absolute reference.
    Good for the couple of minutes a clip runs. Upgrade to a real VO/SLAM front-end if the drift
    ever outgrows the 0.15 m voxel the reconstruction quantises to.
    """

    MIN_PTS = 12

    def __init__(self, R_seed, hfov_deg, flow_w=POSE_FLOW_W):
        self._R_seed = np.array(R_seed, dtype=np.float64)
        self.R = self._R_seed.copy()
        self._hfov_deg = hfov_deg
        self._flow_w = flow_w
        self._prev = None
        self._f = None
        self._c = None

    def reset(self):
        """Clip looped: the camera teleports back to frame 0, so drop the integrated rotation
        rather than carrying that discontinuity forward as if it were real motion."""
        self.R = self._R_seed.copy()
        self._prev = None

    def update(self, frame_bgr):
        h, w = frame_bgr.shape[:2]
        fw = min(self._flow_w, w)
        fh = max(1, round(h * fw / w))
        small = cv2.resize(frame_bgr, (fw, fh), interpolation=cv2.INTER_AREA) if fw != w else frame_bgr
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY) if small.ndim == 3 else small
        if self._f is None:
            K = geom.intrinsics(fw, fh, self._hfov_deg)
            self._f, self._c = float(K[0, 0]), (float(K[0, 2]), float(K[1, 2]))
        prev, self._prev = self._prev, gray
        if prev is None:
            return self.R
        pts = cv2.goodFeaturesToTrack(prev, maxCorners=300, qualityLevel=0.01, minDistance=8)
        if pts is None or len(pts) < self.MIN_PTS:
            return self.R  # featureless frame (sky, blur): hold the last rotation
        nxt, status, _ = cv2.calcOpticalFlowPyrLK(prev, gray, pts, None)
        if nxt is None:
            return self.R
        ok = status.ravel() == 1
        if int(ok.sum()) < self.MIN_PTS:
            return self.R
        M, _ = cv2.estimateAffinePartial2D(pts[ok], nxt[ok], method=cv2.RANSAC,
                                           ransacReprojThreshold=2.0)
        if M is None:
            return self.R
        self.R = self.R @ self._delta(M)
        return self.R

    def _delta(self, M):
        """Similarity ``M`` (previous frame -> current) -> camera-frame rotation matrix.

        Under a pure rotation ``dR`` a point moves as ``x1 = K dR^T K^-1 x0``, so for a small
        ``dR`` the principal point's displacement is yaw and pitch and the in-plane angle is
        roll. Axes are shared/geom.py's camera frame: +X right, +Y down, +Z forward, and
        ``R`` is camera-to-world, so the delta composes on the right.
        """
        cx, cy = self._c
        du = M[0, 0] * cx + M[0, 1] * cy + M[0, 2] - cx
        dv = M[1, 0] * cx + M[1, 1] * cy + M[1, 2] - cy
        theta = math.atan2(M[1, 0], M[0, 0])
        omega = np.array([dv / self._f, -du / self._f, -theta], dtype=np.float64)
        return cv2.Rodrigues(omega)[0]


def _ground_project(K, R, p, u, v):
    hit = geom.to_ground(K, R, p, u, v)
    if hit is None:
        return None
    x, z = hit
    # Range from the camera's own nadir, not from the world origin: with --world-offset the two
    # are no longer the same point, and measuring from the origin would reject every hit.
    if ((x - p[0]) ** 2 + (z - p[2]) ** 2) ** 0.5 > MAX_GROUND_RANGE_M:
        return None
    return np.array([x, 0.0, z])


def _depth_fallback_pos(K, R, p, u, v, depth_map, depth_scale):
    """ponytail: Depth-Anything-V2 output here is *relative* depth (flags bit1=0), no metric
    calibration pass exists yet -- this maps the patch's relative value linearly into
    [DEPTH_NEAR_M, DEPTH_FAR_M] along the camera ray. Good enough to keep a track alive near
    the horizon where ground-plane intersection is numerically unreliable; replace with a
    calibrated/metric depth model if real ranging accuracy is needed."""
    d_world = geom.backproject_ray(K, R, u, v)
    d_world = d_world / (np.linalg.norm(d_world) + 1e-9)
    if depth_map is None:
        return p + d_world * 50.0  # no depth yet: park at an assumed mid-range distance
    du, dv = u * depth_scale[0], v * depth_scale[1]
    h, w = depth_map.shape
    u0, u1 = max(0, int(du) - 2), min(w, int(du) + 3)
    v0, v1 = max(0, int(dv) - 2), min(h, int(dv) + 3)
    patch = depth_map[v0:v1, u0:u1]
    if patch.size == 0:
        return p + d_world * 50.0
    val = float(np.median(patch))
    dmin, dmax = float(depth_map.min()), float(depth_map.max())
    norm = (val - dmin) / max(dmax - dmin, 1e-6)  # 0..1, larger raw value assumed = closer
    dist = DEPTH_FAR_M - norm * (DEPTH_FAR_M - DEPTH_NEAR_M)
    return p + d_world * dist


def _zero_boxes(depth64x48, boxes_norm):
    h, w = depth64x48.shape
    for x1, y1, x2, y2 in boxes_norm:
        xi0, xi1 = int(x1 * w), max(int(x1 * w) + 1, int(x2 * w))
        yi0, yi1 = int(y1 * h), max(int(y1 * h) + 1, int(y2 * h))
        depth64x48[max(0, yi0):min(h, yi1), max(0, xi0):min(w, xi1)] = 0.0


def run(args):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    track_addr = (args.host, args.track_port)
    depth_addr = (args.host, args.depth_port)

    # Both models below need a ClikaRT licence seat; _PoseTracker needs nothing but OpenCV. They
    # therefore fail independently, and a laptop whose hardware fingerprint moves between runs
    # loses the seat without warning. --no-detect keeps the pose half of the wire alive so the
    # viewer side stays testable on its own -- empty track lists, same datagram shape.
    det = depth_model = None
    if not args.no_detect:
        det = ClikaTiledDetector(DET_MODEL, args.cache_dir, device=args.device, conf=args.conf,
                                  allow_cpu_fallback=args.allow_cpu_fallback)
        depth_opts = LoadOptions(device=crt.Device.parse(args.device), cache_dir=args.cache_dir, offline=True)
        depth_model = ModelRegistry.builtin().load_depth(DEPTH_MODEL, depth_opts)
        check_device(args.device, depth_model.device, args.allow_cpu_fallback)

    cap = cv2.VideoCapture(args.clip)
    if not cap.isOpened():
        raise RuntimeError(f"could not open clip {args.clip!r}")
    fps_in = cap.get(cv2.CAP_PROP_FPS) or 30.0
    native_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    native_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    det_h = min(DET_RES, native_h)
    det_w = round(native_w * det_h / native_h)
    K_det = geom.intrinsics(det_w, det_h, args.hfov)

    depth_w = min(DEPTH_RES_W, native_w)
    depth_h = round(native_h * depth_w / native_w)
    depth_scale = (depth_w / det_w, depth_h / det_h)  # detect-space px -> depth-space px

    # The seed fixes pitch and height; _PoseTracker then turns the footage's own motion into a
    # per-frame rotation on top of it. Position stays seeded (no monocular scale).
    _, _, _, R_seed, p = next(static_adapter(args.pose_seed, det_w, det_h, 1, fps=fps_in, hfov=args.hfov))
    pose = _PoseTracker(R_seed, args.hfov)
    R = pose.R
    # Moving the camera moves everything it derives -- ground hits are rays cast from p, and the
    # depth fallback is p + direction * range -- so this one line puts the whole leg elsewhere.
    p = p + np.array([float(c) for c in args.world_offset.split(",")], dtype=np.float64)

    detect_stride = max(1, round(fps_in / args.detect_hz))
    depth_stride = max(1, round(fps_in / args.depth_hz))

    tracker = _Tracker()
    keyframer = DepthKeyframer()
    latest_depth = None  # most recent depth prediction, in depth_w x depth_h

    n_tracks_sent = n_depth_chunks_sent = n_keyframes = n_loops = 0
    frame_idx = 0
    t_start = time.time()
    print(f"[edge] clip={args.clip} native={native_w}x{native_h}@{fps_in:.2f} "
          f"det={det_w}x{det_h} stride={detect_stride} depth={depth_w}x{depth_h} stride={depth_stride}",
          file=sys.stderr)

    while time.time() - t_start < args.duration:
        ok, frame = cap.read()
        if not ok:
            if args.loop or time.time() - t_start < args.duration:
                cap.release()
                cap = cv2.VideoCapture(args.clip)
                frame_idx = 0
                n_loops += 1
                pose.reset()
                continue
            break

        t = time.time() - t_start
        boxes_norm = []
        R = pose.update(frame)
        quat = geom.matrix_to_quat(R)                   # OpenCV frame: depth chunks, recon.py
        quat_view = geom.matrix_to_quat(R @ CV_TO_GL)   # -Z frame: the wire Pose, like the phone

        if frame_idx % detect_stride == 0:
            dets = []  # Nx6: x1,y1,x2,y2,conf,cls in det_frame pixels
            if det is not None:
                det_frame = cv2.resize(frame, (det_w, det_h), interpolation=cv2.INTER_AREA) \
                    if (det_w, det_h) != (native_w, native_h) else frame
                dets = det(det_frame)
            world_dets = []
            for x1, y1, x2, y2, conf, cls in dets:
                boxes_norm.append((x1 / det_w, y1 / det_h, x2 / det_w, y2 / det_h))
                u, v = (x1 + x2) / 2.0, y2  # bbox bottom-centre
                pos = _ground_project(K_det, R, p, u, v)
                if pos is None:
                    pos = _depth_fallback_pos(K_det, R, p, u, v, latest_depth, depth_scale)
                world_dets.append((int(cls), pos))
            confirmed = tracker.step(world_dets, t)
            payload = {
                "t": int(time.time() * 1000), "tick": frame_idx,
                "pose": {"pos": [round(float(c), 3) for c in p],
                         "quat": [round(float(c), 5) for c in quat_view]},
                "tracks": [{"id": tr.id, "cls": tr.cls,
                            "pos": [round(float(c), 3) for c in tr.pos],
                            "vel": [round(float(c), 3) for c in tr.vel],
                            "conf": 1.0, "age": tr.hits} for tr in confirmed],
            }
            sock.sendto(json.dumps(payload).encode(), track_addr)
            n_tracks_sent += 1

        if depth_model is not None and frame_idx % depth_stride == 0:
            depth_frame = cv2.resize(frame, (depth_w, depth_h), interpolation=cv2.INTER_AREA)
            raw = depth_model.predict(crt.Tensor.from_data(np.ascontiguousarray(depth_frame[..., ::-1])))
            latest_depth = np.asarray(raw.to("cpu"), dtype=np.float32)
            small = cv2.resize(latest_depth, (DEPTH_W, DEPTH_H), interpolation=cv2.INTER_AREA)
            _zero_boxes(small, boxes_norm)
            if keyframer.due(small, t):
                kf_id = keyframer.emit(small, t)
                n_keyframes += 1
                dmin, dmax = float(small.min()), float(small.max())
                rng = max(dmax - dmin, 1e-6)
                q8 = np.clip(((small - dmin) / rng) * 255.0, 0, 255).astype(np.uint8)
                for row0 in range(0, DEPTH_H, ROWS_PER_CHUNK):
                    chunk = encode_depth_chunk(args.drone_id, kf_id, row0,
                                                q8[row0:row0 + ROWS_PER_CHUNK], quat, p, dmin, dmax)
                    assert len(chunk) <= 200, f"depth chunk {len(chunk)} B exceeds 200 B hard limit"
                    sock.sendto(chunk, depth_addr)
                    n_depth_chunks_sent += 1

        frame_idx += 1

    cap.release()
    print(f"[edge] done frames={frame_idx} loops={n_loops} track_datagrams={n_tracks_sent} "
          f"depth_keyframes={n_keyframes} depth_chunks={n_depth_chunks_sent}", file=sys.stderr)


def _selfcheck():
    """_PoseTracker against a synthetic pan: slide a window right across a fixed texture, which
    is what a camera panning right sees, and check the integrated rotation is a yaw of the
    expected size and sign with no pitch or roll alongside it. Catches the failures that matter
    -- a flipped sign, a swapped axis, an estimate that never accumulates."""
    w, h, dx, steps, hfov = 480, 360, 3, 20, 85.0
    rng = np.random.default_rng(7)
    # Smoothed, not raw, noise: optical flow needs a signal it can differentiate; white noise
    # aliases under the pyramid and tracks nothing.
    big = cv2.GaussianBlur((rng.random((h, w + dx * steps + 20)) * 255).astype(np.uint8), (0, 0), 1.5)
    frame = lambda i: cv2.cvtColor(big[:, i * dx:i * dx + w], cv2.COLOR_GRAY2BGR)

    f = float(geom.intrinsics(w, h, hfov)[0, 0])
    pose = _PoseTracker(np.eye(3), hfov, flow_w=w)
    for i in range(steps + 1):
        pose.update(frame(i))
    rv = cv2.Rodrigues(pose.R)[0].ravel()  # seed is identity, so this IS the integrated delta
    want = steps * dx / f
    assert abs(rv[1] - want) < 0.05 * want, f'yaw {rv[1]:.4f} rad, expected ~{want:.4f}'
    assert abs(rv[0]) < 0.01 and abs(rv[2]) < 0.01, f'pan leaked into pitch/roll: {rv}'

    # The wire's Pose must come out in the -Z frame: a UAV seed pitched 80 deg down has to render
    # as looking *down*, not at the sky. This is the mismatch that made the overlay disagree with
    # the clip, so it gets an assertion rather than a comment.
    _, _, _, R_seed, _ = next(static_adapter("meva-uav-0307-1720", 1920, 1080, 1, fps=30.0, hfov=hfov))
    view_dir = np.asarray(R_seed) @ CV_TO_GL @ np.array([0.0, 0.0, -1.0])
    assert view_dir[1] < -0.9, f'wire pose looks up, not down: {view_dir}'

    # A still camera must not drift, and reset() must forget the pan (the clip-loop case).
    pose.reset()
    assert np.allclose(pose.R, np.eye(3)), 'reset did not restore the seed'
    for _ in range(5):
        pose.update(frame(0))
    assert float(np.linalg.norm(cv2.Rodrigues(pose.R)[0])) < 0.01, 'static scene produced rotation'

    print('edge/edge.py _PoseTracker self-check: OK '
          f'(yaw {math.degrees(rv[1]):.2f} deg over {steps} frames, expected {math.degrees(want):.2f})')
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--clip", default=DEFAULT_CLIP)
    ap.add_argument("--cache-dir", default=DEFAULT_CACHE,
                     help="absolute path strongly recommended -- see detector.py's relative-path trap")
    ap.add_argument("--device", default="vulkan", choices=["vulkan", "cpu"])
    ap.add_argument("--allow-cpu-fallback", action="store_true")
    ap.add_argument("--no-detect", action="store_true",
                     help="skip the detector and depth model (no ClikaRT licence needed): still "
                          "tracks camera pose and sends it, but every datagram has empty tracks")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--track-port", type=int, default=7701)
    ap.add_argument("--depth-port", type=int, default=7790)
    ap.add_argument("--loop", action="store_true",
                     help="restart clip at EOF even if --duration has already elapsed; "
                          "note the clip is ALSO looped automatically whenever --duration "
                          "outlasts it, regardless of this flag")
    ap.add_argument("--duration", type=float, default=20.0, help="seconds of wall-clock run time")
    ap.add_argument("--pose-seed", default="meva-uav-0307-1720",
                     help="substring key into shared/pose_source.py's seed table -- independent "
                          "of --clip's actual filename, since the only clip currently fetched "
                          "into tools/footage/clips/ isn't one of the seeded names")
    ap.add_argument("--hfov", type=float, default=85.0)
    ap.add_argument("--detect-hz", type=float, default=2.0,
                     help="measured operating point, reports/CLIKA_SC1.md (~1.9 Hz @ 1080p/8 tiles)")
    ap.add_argument("--depth-hz", type=float, default=4.0,
                     help="measured operating point, reports/CLIKA_SC1.md (~5.6 Hz @ 640px wide)")
    ap.add_argument("--conf", type=float, default=0.25)
    ap.add_argument("--drone-id", type=int, default=0)
    ap.add_argument("--world-offset", default="0,0,0", metavar="X,Y,Z",
                     help="metres to shift this leg's camera (and so everything it projects) by, "
                          "in the MinBand world frame. Both this leg and a phone edge lock their "
                          "own origin near (0,0,0), so without an offset they land on top of each "
                          "other and server/src/fusion.ts can merge a phone-detected person with "
                          "a clip-detected one. Give them separate places instead")
    ap.add_argument("--selfcheck", action="store_true",
                     help="run _PoseTracker's self-check and exit (no clip, no models, no sockets)")
    args = ap.parse_args()
    if args.selfcheck:
        return _selfcheck()
    run(args)


if __name__ == "__main__":
    main()
