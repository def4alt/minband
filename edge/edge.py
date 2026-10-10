"""edge/edge.py -- P4 real-time MinBand edge pipeline for a static-camera drone leg.

detect (ClikaTiledDetector) -> track (simple nearest-neighbor, scipy unavailable in
.venv -- see note below) -> ground-project (shared/geom.py) -> emit two UDP streams:

1. Track datagrams (internal seam, NOT a frozen spec -- P5a should treat this as a draft
   to port into the real BLE wire format, core/src/wire.rs's Message::Delta/Keyframe, not
   consume it as-is long term). One JSON line per processed frame, sent to
   127.0.0.1:<--track-port>, shape:
   {"t": <wall ms>, "tick": <frame idx>, "tracks": [{"id": int, "cls": int,
   "pos": [x, y, z], "vel": [vx, vy, vz], "conf": float, "age": int}, ...]}
   pos/vel are in the single MinBand world frame (shared/geom.py: right-handed, metres,
   +Y up, ground_y=0) -- y is always 0.0 here (ground-plane tracking), kept as a 3-vector
   so a future metric-depth upgrade can fill it in without changing the schema.

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
DET_MODEL = "ustc-community/dfine-nano-coco"
DEPTH_MODEL = "depth-anything/Depth-Anything-V2-Small-hf"

DET_RES = 1080          # chosen operating point (reports/CLIKA_SC1.md): detector on a
                         # 1080p-downscaled copy of the frame, tile=0 (auto, 8 tiles), ~1.9 Hz.
DEPTH_RES_W = 640        # depth model fed a 640px-wide downscale, ~5.6 Hz measured.
MAX_GROUND_RANGE_M = 200.0   # ray/ground hit farther than this = treat as horizon-noise,
                             # fall back to depth.
DEPTH_NEAR_M, DEPTH_FAR_M = 3.0, 150.0  # heuristic relative-depth -> metres clamp (no metric
                                        # calibration available -- see _depth_fallback_pos).

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


def _ground_project(K, R, p, u, v):
    hit = geom.to_ground(K, R, p, u, v)
    if hit is None:
        return None
    x, z = hit
    if (x * x + z * z) ** 0.5 > MAX_GROUND_RANGE_M:
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

    _, _, _, R, p = next(static_adapter(args.pose_seed, det_w, det_h, 1, fps=fps_in, hfov=args.hfov))

    detect_stride = max(1, round(fps_in / args.detect_hz))
    depth_stride = max(1, round(fps_in / args.depth_hz))

    tracker = _Tracker()
    keyframer = DepthKeyframer()
    latest_depth = None  # most recent depth prediction, in depth_w x depth_h
    quat = geom.matrix_to_quat(R)

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
                continue
            break

        t = time.time() - t_start
        boxes_norm = []

        if frame_idx % detect_stride == 0:
            det_frame = cv2.resize(frame, (det_w, det_h), interpolation=cv2.INTER_AREA) \
                if (det_w, det_h) != (native_w, native_h) else frame
            dets = det(det_frame)  # Nx6: x1,y1,x2,y2,conf,cls in det_frame pixels
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
                "tracks": [{"id": tr.id, "cls": tr.cls,
                            "pos": [round(float(c), 3) for c in tr.pos],
                            "vel": [round(float(c), 3) for c in tr.vel],
                            "conf": 1.0, "age": tr.hits} for tr in confirmed],
            }
            sock.sendto(json.dumps(payload).encode(), track_addr)
            n_tracks_sent += 1

        if frame_idx % depth_stride == 0:
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


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--clip", default=DEFAULT_CLIP)
    ap.add_argument("--cache-dir", default=DEFAULT_CACHE,
                     help="absolute path strongly recommended -- see detector.py's relative-path trap")
    ap.add_argument("--device", default="vulkan", choices=["vulkan", "cpu"])
    ap.add_argument("--allow-cpu-fallback", action="store_true")
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
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
