"""hub/recon.py -- P5b: depth-keyframe reconstruction into a voxel occupancy map, served to the
browser over SSE (C5: stdlib-only, one-directional, auto-reconnecting -- no WebSocket dependency).

receive depth chunks (UDP, proto/DEPTH.md)
  -> reassemble per (drone_id, kf_id), accepting partial keyframes
  -> robust relative-depth -> metres fit (1/z = a*disp + b) from ground-hitting pixels
  -> non-ground pixels reprojected to world points
  -> voxel hash (0.15 m cells), occupied at >=2 hits
  -> SSE deltas + stats on :8090, stdlib http.server + text/event-stream

Camera intrinsics: depth chunks carry pose (quat, pos) but not K/HFOV (keeping chunks small is the
whole point of proto/DEPTH.md) -- `--hfov` here must match edge.py's static_adapter default (85.0,
Zenmuse X3 per D6), same convention as track.py's own `--hfov` flag.
"""
import argparse
import http.server
import json
import socket
import sys
import threading
import time
from pathlib import Path

import numpy as np

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
from shared import geom
from depth_chunks import decode_depth_chunk, dequantize

DEPTH_W, DEPTH_H = 64, 48  # fixed, proto/DEPTH.md's chosen operating point
VOXEL_M = 0.15
MIN_HITS = 2
# A pixel counts as "ground-hitting" only if the ray actually lands on the ground plane within a
# sane range -- far beyond this is horizon noise (same idea as edge.py's MAX_GROUND_RANGE_M, but a
# shorter range here since this fits the *scale*, not just a fallback decision).
MAX_FIT_RANGE_M = 120.0
MIN_GROUND_SAMPLES = 24  # refit only once there's enough signal; else keep the last (a, b)
# ponytail: fixed fraction of the keyframe's own disparity span, not a calibrated depth-error
# model; tighten if flat ground gets misclassified as obstacles, loosen if low obstacles vanish.
OBSTACLE_MARGIN_FRAC = 0.08


def ground_hit_range(K, R, p, u, v):
    """Like shared/geom.py's `ray_ground_intersection`, but returns the ray parameter `t` itself
    (== the camera-frame z-depth, since K's bottom row is [0,0,1] so the backprojected
    direction's camera-frame z is always 1) rather than the (X, Z) hit point -- what we need to
    correlate against the model's z-depth-like disparity output. Not added to shared/geom.py:
    recon.py doesn't own shared/, and this is a two-line specialisation of
    `ray_ground_intersection`'s own formula, not new geometry."""
    d = geom.backproject_ray(K, R, u, v)
    dy = d[1]
    if (p[1] > 0 and dy >= -1e-9) or (p[1] < 0 and dy <= 1e-9):
        return None
    t = (0.0 - p[1]) / dy
    if t <= 0 or t > MAX_FIT_RANGE_M:
        return None
    return t, d


def robust_fit(xs, ys):
    """1/z = a*disp + b, fit by iterative trimmed least squares (drop the worst 20% of residuals,
    refit once) -- a poor man's RANSAC. ponytail: not real RANSAC; upgrade if ground noise ever
    dominates the sample set (e.g. moving foliage misread as ground)."""
    xs, ys = np.asarray(xs, dtype=np.float64), np.asarray(ys, dtype=np.float64)
    if len(xs) < 2 or np.ptp(xs) < 1e-6:
        return None
    a, b = np.polyfit(xs, ys, 1)
    resid = np.abs(ys - (a * xs + b))
    keep = resid <= np.quantile(resid, 0.8)
    if keep.sum() < 2 or np.ptp(xs[keep]) < 1e-6:
        return float(a), float(b)
    a, b = np.polyfit(xs[keep], ys[keep], 1)
    return float(a), float(b)


class DroneRecon:
    """Per-drone depth-keyframe state: the in-progress/last keyframe's dequantized map, its own
    ground-depth scale fit (persisted across keyframes per the plan: "reusing last a,b when
    ground is not visible"), and the voxel hash it feeds."""

    def __init__(self, hfov_deg, accumulate):
        self.hfov_deg = hfov_deg
        self.accumulate = accumulate
        self.kf_id = None
        self.depth = np.full((DEPTH_H, DEPTH_W), np.nan, dtype=np.float32)
        self.have_row = np.zeros(DEPTH_H, dtype=bool)
        self.ab = None  # (a, b), persists across keyframes
        self.voxels = {}  # (i, j, k) -> hit count
        self.voxel_log = []  # append-only, newest-occupied order; SSE clients each keep their own
        # read cursor into this (see drain_deltas) so multiple simultaneous viewers each see every
        # voxel once, including ones occupied before they connected -- a single shared "drained"
        # queue (the previous design) meant whichever client's SSE loop ran first stole the event
        # and every other client silently never saw it.
        self.K = None
        # Obstacle pixels seen before `self.ab` had enough ground samples to fit yet (e.g. the
        # very first keyframe, if a chunk with no ground in it streams in before one that has
        # some) -- held here, per (R, p, u, v, disp), and flushed once a fit exists so row order
        # within a keyframe can never silently lose an obstacle.
        self.pending_obstacles = []

    def ingest_chunk(self, chunk):
        if chunk["kf_id"] != self.kf_id:
            self.kf_id = chunk["kf_id"]
            self.depth.fill(np.nan)
            self.have_row.fill(False)
            self.pending_obstacles = []
            if not self.accumulate:
                self.voxels.clear()  # SNAPSHOT (D7): fresh map per keyframe
        row0, nrows = chunk["row0"], chunk["nrows"]
        self.depth[row0:row0 + nrows, :chunk["w"]] = dequantize(chunk)
        self.have_row[row0:row0 + nrows] = True
        if self.K is None:
            # ponytail: intrinsics approximated at the 64x48 chunk resolution/aspect, which is not
            # necessarily the original capture's aspect ratio (the depth map was resized down for
            # the wire format, proto/DEPTH.md) -- fine for this prototype's non-metric
            # reconstruction; upgrade if reconstructed geometry needs to be dimensionally exact.
            self.K = geom.intrinsics(DEPTH_W, DEPTH_H, self.hfov_deg)
        self._reconstruct_rows(chunk, row0, nrows)

    def _reconstruct_rows(self, chunk, row0, nrows):
        R = geom.quat_to_matrix(chunk["quat"])
        p = np.asarray(chunk["pos"], dtype=np.float64)
        disp_rows = dequantize(chunk)
        depth_range = max(chunk["dmax"] - chunk["dmin"], 1e-6)
        # "High-gradient flying pixels" (plan, P5b scope): monocular depth is noisy right at
        # object-silhouette edges, and voxelizing that noise paints a false floating shell around
        # every obstacle. Drop a pixel whose steepest immediate-neighbour jump (within this
        # chunk's own rows -- no cross-chunk neighbours available mid-reassembly) exceeds 25% of
        # this keyframe's own dmin-dmax span.
        gy, gx = np.gradient(disp_rows.astype(np.float64))
        steep = (np.abs(gx) + np.abs(gy)) > 0.25 * depth_range
        ground_disp, ground_inv_t = [], []
        obstacle_px = []  # (u, v, disp)
        # Once a fit exists, a pixel whose ray geometrically crosses the ground plane is still an
        # obstacle if its *measured* disparity reads closer than the ground-hit predicts (true for
        # a nadir camera, where almost every ray crosses ground somewhere -- geometry alone can't
        # tell ground from "car roof in the way", only the measured depth can).
        a_b = self.ab
        for dv in range(nrows):
            v = row0 + dv
            for u in range(chunk["w"]):
                disp = float(disp_rows[dv, u])
                if disp <= 0 or steep[dv, u]:
                    continue  # dropped: no data (proto/DEPTH.md px=0), or a flying/edge pixel
                hit = ground_hit_range(self.K, R, p, u, v)
                if hit is None:
                    continue  # ray never reaches ground (e.g. looking above horizon) -- no usable geometry
                t, _ = hit
                if a_b is not None:
                    a, b = a_b
                    pred_ground_disp = (1.0 / t - b) / a if a > 1e-9 else None
                    if pred_ground_disp is not None and disp - pred_ground_disp > OBSTACLE_MARGIN_FRAC * depth_range:
                        obstacle_px.append((u, v, disp))
                        continue
                ground_disp.append(disp)
                ground_inv_t.append(1.0 / t)
        if len(ground_disp) >= MIN_GROUND_SAMPLES:
            fit = robust_fit(ground_disp, ground_inv_t)
            if fit is not None:
                self.ab = fit
        self.pending_obstacles.extend((R, p, u, v, disp) for u, v, disp in obstacle_px)
        if self.ab is None:
            return  # no fit yet this keyframe -- held in pending_obstacles until one lands
        a, b = self.ab
        pending, self.pending_obstacles = self.pending_obstacles, []
        for R, p, u, v, disp in pending:
            inv_z = a * disp + b
            if inv_z <= 1e-6:
                continue  # fit says "behind camera or at infinity" -- drop, don't fabricate a voxel
            z = 1.0 / inv_z
            if z <= 0 or z > MAX_FIT_RANGE_M:
                continue
            d = geom.backproject_ray(self.K, R, u, v)
            world = p + z * d
            self._add_voxel(world)

    def _add_voxel(self, world):
        key = tuple(int(np.floor(c / VOXEL_M)) for c in world)
        n = self.voxels.get(key, 0) + 1
        self.voxels[key] = n
        if n == MIN_HITS:
            self.voxel_log.append(key)

    def voxels_since(self, cursor):
        """-> (new keys since `cursor`, cursor to pass in next time)."""
        return self.voxel_log[cursor:], len(self.voxel_log)


class ReconServer:
    def __init__(self, hfov_deg, accumulate):
        self.drones = {}
        self.hfov_deg = hfov_deg
        self.accumulate = accumulate
        self.lock = threading.Lock()
        self.chunks_seen = 0
        self.keyframes_seen = set()

    def ingest(self, buf):
        chunk = decode_depth_chunk(buf)
        with self.lock:
            self.chunks_seen += 1
            self.keyframes_seen.add((chunk["drone_id"], chunk["kf_id"]))
            d = self.drones.setdefault(chunk["drone_id"], DroneRecon(self.hfov_deg, self.accumulate))
            d.ingest_chunk(chunk)

    def drain_deltas(self, cursors):
        """-> list of [[i,j,k],...] voxel-key lists, one per drone with new occupied voxels since
        `cursors` (a per-SSE-client {drone_id: index} dict, updated in place) last looked."""
        out = []
        with self.lock:
            for did, d in self.drones.items():
                add, cursors[did] = d.voxels_since(cursors.get(did, 0))
                if add:
                    out.append([list(k) for k in add])
        return out

    def stats(self):
        with self.lock:
            return {
                "chunks": self.chunks_seen,
                "keyframes": len(self.keyframes_seen),
                "drones": {
                    did: {"voxels": len(d.voxels), "ab": d.ab}
                    for did, d in self.drones.items()
                },
            }


def udp_reader(sock, recon):
    while True:
        try:
            buf, _ = sock.recvfrom(2048)
        except OSError:
            return
        try:
            recon.ingest(buf)
        except (ValueError, IndexError) as e:
            print(f"recon: malformed chunk dropped: {e}", file=sys.stderr)


def make_sse_handler(recon):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # quiet -- stats/vox events would otherwise spam stdout every tick

        def do_GET(self):
            if self.path not in ("/", "/events"):
                self.send_response(404); self.end_headers(); return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            cursors = {}  # this connection's own read position into each drone's voxel_log
            try:
                while True:
                    for add in recon.drain_deltas(cursors):
                        self.wfile.write(f"data: {json.dumps({'t': 'vox', 'add': add})}\n\n".encode())
                    self.wfile.write(f"data: {json.dumps({'t': 'stats', **recon.stats()})}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(0.5)
            except (BrokenPipeError, ConnectionResetError):
                return  # client disconnected -- EventSource will auto-reconnect (C5)

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen-port", type=int, default=7790, help="UDP, matches edge.py's --depth-port")
    ap.add_argument("--sse-port", type=int, default=8090)
    ap.add_argument("--hfov", type=float, default=85.0, help="must match edge.py's static_adapter hfov")
    ap.add_argument("--accumulate", action="store_true",
                     help="stretch mode (D7): keep voxels across keyframes instead of resetting "
                          "each keyframe (SNAPSHOT, the default). Drifts -- not an acceptance mode.")
    args = ap.parse_args()

    recon = ReconServer(args.hfov, args.accumulate)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", args.listen_port))
    threading.Thread(target=udp_reader, args=(sock, recon), daemon=True).start()

    httpd = http.server.ThreadingHTTPServer(("0.0.0.0", args.sse_port), make_sse_handler(recon))
    print(f"recon: depth chunks on :{args.listen_port}, SSE on :{args.sse_port}"
          f"{' (accumulate)' if args.accumulate else ' (snapshot)'}", file=sys.stderr)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
