#!/usr/bin/env python
"""Operator-side GPU viewer for the LIVE map stream: map_receiver.py's socket protocol + logging, but the per-record
numpy render is replaced by an Open3D non-blocking window (o3d.visualization.VisualizerWithKeyCallback, poll_events /
update_renderer in the main thread at --fps, default 30 Hz cap; sleeps the rest -> no busy loop).

Wire (unchanged): TCP, one sender, frames = <I len (little-endian)><payload>.
  payload b'POSE' + float64[8] traj row (t x y z qx qy qz qw) + float64 dt_slot   -> newest keyframe pose
  anything else = one record / chunk of the streamer (first one carries the stream header) -> Decoder.apply
Decoding runs in a child PROCESS (no GIL sharing with the render loop); it never calls dec.map(): IncrementalMap mirrors Decoder.map() record by record
from the decoder state (st.known / st.cidx per LOD band) and hands only the NEW voxels (+ the coarse voxels that just
got covered by finer ones) to the render loop through a queue.  One Open3D geometry per band grows with
Vector3dVector.extend (append) and is rebuilt only when voxels of that band disappear.
  --draw hybrid (default): coarse bands as flat quads (TriangleMesh, 4 vertices / voxel) + the finest band as points
                           sized from the splat (pixel size = f_px * splat / distance-to-drone, like the numpy renderer)
  --draw quads           : every voxel as a quad (closest to the numpy splat renderer, hole-free, 4 vertices / voxel)
  --draw points          : exactly the point set of Decoder.map() (coarse voxels densified kk x kk) -> most vertices
Camera: follows the latest POSE from behind/above (smoothed).  Keys: F = toggle free camera (mouse) / follow,
        T = top-down follow, P = screenshot, Q/Esc = quit.  Window title shows fps / records / latency (via xprop).
Logs: recv_log.txt (same columns as map_receiver.py), recv_gui_log.txt (per record: decode ms, queue ms, upload ms,
      arrival->visible ms, fps; per second: fps, cpu %).  On end: recv_final.ply/.txt/.splat as map_receiver.py.

usage: env -u WAYLAND_DISPLAY XDG_SESSION_TYPE=x11 python map_receiver_gui.py [--listen 0.0.0.0:5555] [--out DIR]
         [--streamer stream_x.py] [--draw hybrid|quads|points] [--fps 30] [--size 960x540] [--hold S]
         [--screenshot PNG --screenshot-after S] [--snap-every S]
(GNOME/Wayland: the env above makes Open3D's GLFW/X11 window work; natively on Wayland Open3D 0.19 aborts.)
"""
import os, sys, time, socket, struct, argparse, importlib.util, traceback, queue, subprocess, multiprocessing as mp, numpy as np
from scipy.spatial.transform import Rotation
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
POSE_LEN = 4 + 8 * 8 + 8
STAT_LEN = 27  # b"STAT" + <IHIIIfB>: sender status, ~1/s
M_GRID = 40.0  # lod_common.M: band levels are given in units of 1/40 m ... i.e. voxel size = level / M (as Decoder.map)
keyf = lambda u: (u[:, 0].astype(np.int64) << 42) | (u[:, 1].astype(np.int64) << 21) | u[:, 2].astype(np.int64)
QUAD = np.array([[0, .5, 0], [1, .5, 0], [1, .5, 1], [0, .5, 1]], float)  # flat tile at mid height of the voxel (ground frame y = normal)
QUAD_TRI = np.array([[0, 1, 2], [0, 2, 3]], np.int32)
# solid voxel: 5 faces (top + 4 sides; the bottom is never seen from the air), 4 own corners each so a face can be shaded.
# Ground frame: axis 1 points DOWN, so the top face is at d1 = 0.  Shade: top 1.0, sides 0.82 / 0.68 (fake light, unlit render).
_F = [([0, 0, 0], [1, 0, 0], [1, 0, 1], [0, 0, 1], 1.00),   # top
      ([0, 0, 0], [0, 1, 0], [0, 1, 1], [0, 0, 1], 0.82),   # -x side
      ([1, 0, 0], [1, 1, 0], [1, 1, 1], [1, 0, 1], 0.82),   # +x side
      ([0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], 0.68),   # -z side
      ([0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1], 0.68)]   # +z side
CUBE = np.array([c for f in _F for c in f[:4]], float); CUBE_SHADE = np.repeat([f[4] for f in _F], 4)
CUBE_TRI = np.concatenate([np.array([[0, 1, 2], [0, 2, 3]], np.int32) + 4 * i for i in range(len(_F))])


def default_streamer():
    for n in ("stream_best.py", "stream_lod_conf.py"):
        p = os.path.join(HERE, n)
        if os.path.exists(p): return p
    raise SystemExit("no streamer found in " + HERE)


def load_streamer(path):
    s = importlib.util.spec_from_file_location("streamer", path); m = importlib.util.module_from_spec(s); s.loader.exec_module(m); return m


def pose_RC(row):  # traj row (t x y z qx qy qz qw) -> (R cam->world, centre), same convention as live_eval.pose_at
    return Rotation.from_quat(row[4:8]).as_matrix(), np.asarray(row[1:4], float)


def write_ply(path, pts, cols):
    pts = np.asarray(pts, np.float32).reshape(-1, 3); cols = np.asarray(cols, np.uint8).reshape(-1, 3)
    rec = np.empty(len(pts), dtype=[("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("r", "u1"), ("g", "u1"), ("b", "u1")])
    rec["x"], rec["y"], rec["z"] = pts.T; rec["r"], rec["g"], rec["b"] = cols.T
    hdr = (f"ply\nformat binary_little_endian 1.0\nelement vertex {len(pts)}\nproperty float x\nproperty float y\nproperty float z\n"
           "property uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n")
    with open(path + ".tmp", "wb") as f: f.write(hdr.encode()); f.write(rec.tobytes())
    os.replace(path + ".tmp", path)


class FrameReader:
    """Reads <I len><payload> frames from a socket; returns None on EOF, raises TimeoutError after idle_timeout s without
    data (counted from the last byte received; before the first byte `first_timeout` applies: the sender connects long
    before its first record)."""
    def __init__(self, sock, idle_timeout, tick=None, first_timeout=None):
        self.s, self.buf, self.idle, self.tick = sock, bytearray(), idle_timeout, tick; sock.settimeout(0.2)
        self.first = first_timeout if first_timeout is not None else idle_timeout; self.last = None; self.t_open = time.time()
    def _fill(self, n):
        while len(self.buf) < n:
            try: d = self.s.recv(65536)
            except socket.timeout:
                if self.tick: self.tick()
                if self.last is None:
                    if time.time() - self.t_open > self.first: raise TimeoutError(f"no data for {self.first:.0f} s after connect")
                elif time.time() - self.last > self.idle: raise TimeoutError(f"no data for {self.idle:.0f} s")
                continue
            if not d: return False
            self.buf += d; self.last = time.time()
        return True
    def read(self):
        if not self._fill(4): return None
        n = struct.unpack("<I", bytes(self.buf[:4]))[0]
        if n > 64 << 20: raise ValueError(f"absurd frame length {n}")
        if not self._fill(4 + n): return None
        p = bytes(self.buf[4:4 + n]); del self.buf[:4 + n]; return p


# ---------------------------------------------------------------------------------------------------------------------
class IncrementalMap:
    """Incremental mirror of stream_best.Decoder.map() (also stream_best_rec / stream_best_pkt: same State).
    After every dec.apply(): ingest(dec) reads the voxels appended to dec.st.known[b] / dec.st.cidx[b] and returns
    per-band update events for the renderer:
       dict(bi, kind='points'|'mesh', reset, verts float64[n,m,3] world, cols uint8[n,3], alive bool[n], dead int64[k])
    'alive' / 'dead' implement the occlusion rule of Decoder.map(): a voxel of band v is dropped as soon as any finer
    band has a voxel inside it (uf // (v/vf) == u).  draw='points' reproduces the densified point set exactly
    (coarse voxel -> kk x kk points, kk = v / vmin); 'quads' draws one flat tile per voxel; 'hybrid' = quads for the
    coarse bands and single points for the finest band present (block shape changes -> reset event, rare)."""
    def __init__(self, draw="hybrid"): self.ready = False; self.draw = draw; self.vmin = None; self.nb = 0; self.spl = 0.0

    def _init(self, dec):
        self.R, self.o, self.spl = np.asarray(dec.R, float), np.asarray(dec.o, float), float(dec.spl)
        self.v = np.asarray(dec.levels, float) / M_GRID; nb = len(self.v); self.nb = nb
        self.u = [np.zeros((0, 3), np.int64) for _ in range(nb)]; self.col = [np.zeros((0, 3), np.uint8) for _ in range(nb)]
        self.alive = [np.zeros(0, bool) for _ in range(nb)]; self.k2i = [dict() for _ in range(nb)]; self.covered = [set() for _ in range(nb)]
        self.vmin = None; self.ready = True

    def _kind(self, bi): return "points" if (self.draw == "points" or (self.draw == "hybrid" and self.v[bi] <= self.vmin + 1e-9)) else ("cubes" if self.draw == "cubes" else "mesh")

    def _blocks(self, bi, u):
        v = self.v[bi]
        if self._kind(bi) == "cubes": off = CUBE
        elif self._kind(bi) == "mesh": off = QUAD
        else:
            kk = int(np.ceil(v / self.vmin - 1e-3)); g = (np.arange(kk) + 0.5) / kk
            off = np.stack(np.meshgrid(g, [0.5], g, indexing="ij"), -1).reshape(-1, 3)
        return ((u[:, None, :].astype(float) + off[None]) * v + self.o) @ self.R  # (n, m, 3) world

    def splat(self): return (self.vmin if self.vmin is not None else 0.0) * self.spl  # as Decoder.map()
    def npts(self):  # the point count Decoder.map() would return
        if not self.ready or self.vmin is None: return 0
        return int(sum(int(self.alive[b].sum()) * int(np.ceil(self.v[b] / self.vmin - 1e-3)) ** 2 for b in range(self.nb)))
    def nvox(self): return [int(self.alive[b].sum()) for b in range(self.nb)] if self.ready else []

    def ingest(self, dec):
        st = getattr(dec, "st", None)
        if st is None or not hasattr(st, "known"): raise AttributeError("decoder has no st.known: incremental map unsupported")
        if not self.ready: self._init(dec)
        new = {}
        for b in range(self.nb):  # 1) append new voxels, index their keys
            U = st.known[b]; n0 = len(self.u[b])
            if len(U) <= n0: continue
            un = np.asarray(U[n0:], np.int64); cn = np.asarray(dec.pal, np.uint8)[np.asarray(st.cidx[b][n0:], np.int64)]
            self.u[b] = np.concatenate([self.u[b], un]); self.col[b] = np.concatenate([self.col[b], cn])
            self.alive[b] = np.concatenate([self.alive[b], np.ones(len(un), bool)])
            self.k2i[b].update(zip(keyf(un).tolist(), range(n0, n0 + len(un)))); new[b] = n0
        dead = {b: [] for b in range(self.nb)}
        for b, n0 in new.items():  # 2) new fine voxels cover their parents in every coarser band
            un = self.u[b][n0:]
            for bj in range(self.nb):
                if self.v[bj] <= self.v[b] + 1e-9: continue
                r = int(round(self.v[bj] / self.v[b])); pk = np.unique(keyf(un // r)).tolist(); cov = self.covered[bj]; cov.update(pk); d = self.k2i[bj]
                for k in pk:
                    i = d.get(k)
                    if i is not None and self.alive[bj][i]: self.alive[bj][i] = False; dead[bj].append(i)
        for b, n0 in new.items():  # 3) new coarse voxels already covered by finer ones received earlier
            cov = self.covered[b]
            if cov:
                for j, k in enumerate(keyf(self.u[b][n0:]).tolist()):
                    if k in cov: self.alive[b][n0 + j] = False
        have = [self.v[b] for b in range(self.nb) if len(self.u[b])]
        if not have: return []
        vmin = min(have); reset = set()
        if self.vmin is None or vmin < self.vmin - 1e-9:  # block shape of coarser bands changes (points: kk; hybrid: finest band)
            old = self.vmin; self.vmin = vmin
            for b in range(self.nb):
                if len(self.u[b]) and b not in new and (self.draw == "points" or (self.draw == "hybrid" and old is not None and self.v[b] <= old + 1e-9)): reset.add(b)
            reset |= set(b for b in new if new[b] > 0)  # new band data + shape change: resend whole band
        evs = []
        for b in sorted(set(new) | reset | set(bj for bj in dead if dead[bj])):
            if b in reset:
                evs.append(dict(bi=b, kind=self._kind(b), reset=True, verts=self._blocks(b, self.u[b]), cols=self.col[b].copy(), alive=self.alive[b].copy(), dead=np.zeros(0, np.int64)))
            else:
                n0 = new.get(b, len(self.u[b]))
                evs.append(dict(bi=b, kind=self._kind(b), reset=False, verts=self._blocks(b, self.u[b][n0:]), cols=self.col[b][n0:].copy(),
                                alive=self.alive[b][n0:].copy(), dead=np.array([i for i in dead[b] if i < n0], np.int64)))
        return evs

    def full_points(self):
        """(pts, cols, splat) exactly like Decoder.map() (for the parity test), independent of --draw."""
        pts, cols = [], []
        for b in range(self.nb):
            a = self.alive[b]
            if not a.any(): continue
            v = self.v[b]; kk = int(np.ceil(v / self.vmin - 1e-3)); g = (np.arange(kk) + 0.5) / kk
            off = np.stack(np.meshgrid(g, [0.5], g, indexing="ij"), -1).reshape(-1, 3)
            pts.append((((self.u[b][a][:, None, :] + off[None]) * v + self.o) @ self.R).reshape(-1, 3)); cols.append(np.repeat(self.col[b][a], len(off), 0))
        return np.concatenate(pts), np.concatenate(cols), self.splat()


# ---------------------------------------------------------------------------------------------------------------------
class BandGeom:
    """Open3D geometries for one band (PointCloud, flat-quad or solid-cube TriangleMesh), split into chunks of at most
    CHUNK voxels: a record only re-uploads the chunk it appends to (plus chunks where voxels died), not the whole band.
    Voxels that get covered later are killed IN PLACE (their vertices are moved to one far point through the numpy view
    of the vertex buffer -> degenerate / clipped), so geometry is never rebuilt except on a reset event."""
    FAR = np.array([1e5, 1e5, 1e5]); CHUNK = 8000
    def __init__(self, vis, o3d):
        self.vis, self.o3d, self.kind, self.m = vis, o3d, None, 1; self.chunks = []
        self.vch = np.zeros(0, np.int64); self.vpos = np.zeros(0, np.int64)  # per voxel: chunk, first vertex in it (-1 = never drawn)

    @property
    def nvert(self): return sum(c["nvert"] for c in self.chunks)

    def _reset(self, kind):
        for c in self.chunks:
            if c["added"]: self.vis.remove_geometry(c["geo"], reset_bounding_box=False)
        self.chunks = []; self.kind = kind

    def _chunk(self):
        if not self.chunks or self.chunks[-1]["nvox"] >= self.CHUNK:
            geo = self.o3d.geometry.TriangleMesh() if self.kind in ("mesh", "cubes") else self.o3d.geometry.PointCloud()
            self.chunks.append(dict(geo=geo, nvert=0, nvox=0, added=False))
        return len(self.chunks) - 1

    def _push(self, V, C):  # V (n, m, 3) world, C (n, 3) uint8 -> appends (possibly over several chunks); -> (chunk, vertex base) per voxel
        n, m = V.shape[:2]; self.m = m; chs = np.zeros(n, np.int64); bases = np.zeros(n, np.int64); i = 0; touched = set()
        while i < n:
            ci = self._chunk(); c = self.chunks[ci]; k = min(n - i, self.CHUNK - c["nvox"]); Vk, Ck = V[i:i + k], C[i:i + k]
            V3 = np.ascontiguousarray(Vk.reshape(-1, 3)); C3 = np.repeat(Ck.astype(float) / 255.0, m, 0); base = c["nvert"] + m * np.arange(k)
            if self.kind == "cubes": C3 *= np.tile(CUBE_SHADE, k)[:, None]
            g = c["geo"]
            if self.kind in ("mesh", "cubes"):
                tri = (base[:, None, None] + (CUBE_TRI if self.kind == "cubes" else QUAD_TRI)[None]).reshape(-1, 3)
                g.vertices.extend(self.o3d.utility.Vector3dVector(V3)); g.vertex_colors.extend(self.o3d.utility.Vector3dVector(C3)); g.triangles.extend(self.o3d.utility.Vector3iVector(tri))
            else:
                g.points.extend(self.o3d.utility.Vector3dVector(V3)); g.colors.extend(self.o3d.utility.Vector3dVector(C3))
            c["nvert"] += len(V3); c["nvox"] += k; chs[i:i + k] = ci; bases[i:i + k] = base; touched.add(ci); i += k
        return chs, bases, touched

    def apply(self, ev, first_bbox=False):
        reset = ev["reset"] or not self.chunks or self.kind != ev["kind"]; touched = set()
        if reset: self._reset(ev["kind"]); self.vch = np.full(len(ev["verts"]), -1, np.int64); self.vpos = np.full(len(ev["verts"]), -1, np.int64); n0 = 0
        else:
            n0 = len(self.vpos); self.vch = np.concatenate([self.vch, np.full(len(ev["verts"]), -1, np.int64)]); self.vpos = np.concatenate([self.vpos, np.full(len(ev["verts"]), -1, np.int64)])
        a = ev["alive"]
        if a.any():
            ii = n0 + np.nonzero(a)[0]; chs, bases, t = self._push(ev["verts"][a], ev["cols"][a]); self.vch[ii] = chs; self.vpos[ii] = bases; touched |= t
        if not reset and len(ev["dead"]):
            d = ev["dead"]; ok = self.vpos[d] >= 0; d = d[ok]
            for ci in np.unique(self.vch[d]).tolist():
                base = self.vpos[d[self.vch[d] == ci]]; c = self.chunks[ci]
                idx = (base[:, None] + np.arange(self.m)[None]).ravel(); V = np.asarray(c["geo"].vertices if self.kind in ("mesh", "cubes") else c["geo"].points); V[idx] = self.FAR; touched.add(ci)
        for ci in sorted(touched):
            c = self.chunks[ci]
            if not c["added"]: self.vis.add_geometry(c["geo"], reset_bounding_box=first_bbox); c["added"] = True; first_bbox = False
            else: self.vis.update_geometry(c["geo"])


class Camera:
    """View modes: 'third' = chase camera behind/above the latest POSE looking ahead (smoothed); 'first' = at the drone
    camera pose looking along its optical axis (incl. roll, ~70 deg hfov like the source camera); 'top' = above the drone;
    follow=False = free Open3D mouse camera."""
    def __init__(self, vis, back=12.0, height=6.0, ahead=8.0, tau=0.5, scale=1.0 / M_GRID, mode="third", hfov_first=70.0):  # back/height/ahead in metres; scale = scene units per metre
        self.vis, self.ctr = vis, vis.get_view_control(); self.back, self.height, self.ahead, self.tau = back * scale, height * scale, ahead * scale, tau
        self.follow, self.up = True, None; self.mode = mode; self.eye = self.tgt = None; self.goal = None; self.t_last = time.time(); self.dirty = True; self.last = None
        prm = self.ctr.convert_to_pinhole_camera_parameters(); self.fx = float(prm.intrinsic.intrinsic_matrix[0, 0]); self.fx3 = self.fx
        self.W, self.H = prm.intrinsic.width, prm.intrinsic.height; self.fx1 = (self.W / 2) / np.tan(np.radians(hfov_first) / 2)
        self.look = 40.0 * scale  # first person: look-at distance (and point-size reference) = 40 m
        for fn, val in (("set_constant_z_near", 0.01), ("set_constant_z_far", 3000.0)):
            if hasattr(self.ctr, fn): getattr(self.ctr, fn)(val)

    @property
    def top(self): return self.mode == "top"

    def set_mode(self, mode):
        self.mode = mode; self.follow = True; self.dirty = True; self.eye = None  # jump (no smoothing across a mode switch)
        self.fx = self.fx1 if mode == "first" else self.fx3
        if self.last is not None: self.set_pose(self.last)
        print(f"[gui] view: {self.label()}", flush=True)

    def label(self): return "FREE (mouse)" if not self.follow else {"third": "3RD PERSON", "first": "1ST PERSON", "top": "TOP"}[self.mode]

    def set_pose(self, row):
        self.last = row; R, c = pose_RC(row)
        if self.mode == "first":
            up = -R[:, 1]; eye, tgt = c.copy(), c + R[:, 2] * self.look
        else:
            up = self.up if self.up is not None else -R[:, 1]
            fwd = R[:, 2] - up * (R[:, 2] @ up); fwd = fwd / (np.linalg.norm(fwd) + 1e-9)
            if self.mode == "top": eye, tgt = c + up * (self.back + self.height) * 1.6, c + fwd * 1e-3
            else: eye, tgt = c - fwd * self.back + up * self.height, c + fwd * self.ahead
        self.goal = (eye, tgt, up); self.dirty = True
        if self.eye is None: self.eye, self.tgt = eye.copy(), tgt.copy()

    def step(self):
        """-> distance eye->drone target (for the point size); applies the extrinsic only while moving."""
        now = time.time(); dt = now - self.t_last; self.t_last = now
        if self.goal is None: return None
        eye, tgt, up = self.goal
        if not self.follow: return float(np.linalg.norm(self._eye_free() - tgt))
        tau = 0.2 if self.mode == "first" else self.tau  # first person: tighter (poses arrive at 5 Hz)
        a = 1.0 - np.exp(-dt / tau); self.eye += (eye - self.eye) * a; self.tgt += (tgt - self.tgt) * a
        if not self.dirty: return float(np.linalg.norm(self.eye - tgt))
        if np.linalg.norm(eye - self.eye) < 0.002 and np.linalg.norm(tgt - self.tgt) < 0.002: self.eye, self.tgt, self.dirty = eye.copy(), tgt.copy(), False
        z = self.tgt - self.eye; z /= np.linalg.norm(z) + 1e-9; x = np.cross(z, up); x /= np.linalg.norm(x) + 1e-9; y = np.cross(z, x)
        Rwc = np.stack([x, y, z]); E = np.eye(4); E[:3, :3] = Rwc; E[:3, 3] = -Rwc @ self.eye
        prm = self.ctr.convert_to_pinhole_camera_parameters(); prm.extrinsic = E
        prm.intrinsic.set_intrinsics(self.W, self.H, self.fx, self.fx, self.W / 2 - 0.5, self.H / 2 - 0.5)
        self.ctr.convert_from_pinhole_camera_parameters(prm, allow_arbitrary=True)
        return float(np.linalg.norm(self.eye - tgt))

    def _eye_free(self):
        E = np.asarray(self.ctr.convert_to_pinhole_camera_parameters().extrinsic); return -E[:3, :3].T @ E[:3, 3]


def cpu_seconds(pid="self"):
    try:
        with open(f"/proc/{pid}/stat") as f: p = f.read().split(")")[-1].split()
        return (int(p[11]) + int(p[12])) / os.sysconf("SC_CLK_TCK")
    except Exception: return 0.0


def net_proc(a, streamer, q, stop):
    """Child process: listens, reads frames, decodes, feeds IncrementalMap and sends render events through q.
    Writes recv_log.txt (same columns as map_receiver.py) and, at the end, recv_final.ply/.txt/.splat."""
    m = load_streamer(streamer); dec = m.make_decoder(); imap = IncrementalMap(a.draw)
    log = open(os.path.join(a.out, "recv_log.txt"), "a"); log.write(f"# map_receiver_gui {time.strftime('%Y-%m-%d %H:%M:%S')} streamer={os.path.basename(streamer)}\n# t_rel_s t_abs_s kind bytes band cum_kB pts\n"); log.flush()
    poses, nrec, cum, t0, reason = [], 0, 0, None, "socket closed"; inc_ok = True; last_npts = 0
    try:
        host, port = a.listen.rsplit(":", 1); srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((host, int(port))); srv.listen(1); srv.settimeout(0.5); print(f"[recv] listening on {a.listen}, out={a.out}, streamer={os.path.basename(streamer)}", flush=True)
        conn, t_start = None, time.time()
        while conn is None:
            if stop.is_set(): reason = "window closed"; break
            try: conn, addr = srv.accept(); print(f"[recv] sender connected from {addr}", flush=True); conn.sendall(b"MAPR")
            except socket.timeout:
                if time.time() - t_start > a.accept_timeout: print("[recv] no sender, giving up", flush=True); reason = "no sender"; break
        srv.close()
        if conn is not None:
            def tick():
                if stop.is_set(): raise TimeoutError("window closed")
            rd = FrameReader(conn, a.idle_timeout, tick=tick, first_timeout=a.accept_timeout)
            try:
                while True:
                    try: pay = rd.read()
                    except (TimeoutError, ValueError) as e: reason = str(e); break
                    if pay is None: break
                    now = time.time(); t0 = t0 or now; cum += len(pay) + 4; trel = now - t0
                    if len(pay) == STAT_LEN and pay[:4] == b"STAT":  # sender status (not a map record)
                        log.write(f"{trel:.3f} {now:.3f} STAT {len(pay)+4} - {cum/1e3:.3f} {last_npts}\n"); log.flush()
                        q.put(("stat", struct.unpack("<IHIIIfB", pay[4:27]), now, len(pay) + 4)); continue
                    if len(pay) == POSE_LEN and pay[:4] == b"POSE":
                        row = np.frombuffer(pay[4:68], np.float64).copy(); dt_slot = struct.unpack("<d", pay[68:76])[0]; poses.append(row)
                        log.write(f"{trel:.3f} {now:.3f} POSE {len(pay)+4} - {cum/1e3:.3f} {last_npts}\n"); log.flush()
                        print(f"[recv] {trel:6.1f}s POSE kf t={row[0]:.2f} dt={dt_slot:.2f}", flush=True); q.put(("pose", row, dt_slot, trel, now)); continue
                    first = nrec == 0
                    band = "hdr" if first else (f"b{pay[0]}" if len(pay) >= 5 and struct.unpack("<I", pay[1:5])[0] == len(pay) - 5 else "chunk")
                    t1 = time.time(); evs = []; npts = 0; splat = 0.0
                    try:
                        dec.apply(pay); applied = True; t_inc = time.time()
                        if inc_ok:  # incremental mirror (stream_best family); decided once: decoders without st.known use dec.map()
                            try: evs = imap.ingest(dec); npts = imap.npts(); splat = imap.splat()
                            except AttributeError as e: inc_ok = False; print(f"[recv] incremental map unsupported by this decoder ({e}); using dec.map()", flush=True)
                            except Exception: inc_ok = False; print("[recv] incremental map failed, falling back to dec.map():", flush=True); traceback.print_exc()
                        if not inc_ok:
                            p2, c2, sp = dec.map(); npts = len(p2); splat = float(sp)
                            evs = [dict(bi=0, kind="points", reset=True, verts=np.asarray(p2, float)[:, None, :], cols=np.asarray(c2, np.uint8), alive=np.ones(len(p2), bool), dead=np.zeros(0, np.int64))]
                    except Exception:
                        applied = False; print(f"[recv] apply FAILED on record {nrec} ({len(pay)} B):", flush=True); traceback.print_exc()
                    t2 = time.time(); tdec = t2 - t1
                    if applied:
                        nrec += 1; last_npts = npts
                        log.write(f"{trel:.3f} {now:.3f} REC {len(pay)+4} {band} {cum/1e3:.3f} {npts}\n"); log.flush()
                        print(f"[recv] {trel:6.1f}s record {nrec} band {band} {len(pay)} B  cum {cum/1e3:.1f} KB  pts {npts:,}  vox {imap.nvox()}  apply {(t_inc-t1)*1e3:.0f} ms + map {(t2-t_inc)*1e3:.0f} ms", flush=True)
                        q.put(("rec", evs, dict(t_arr=now, t_dec=t2, trel=trel, nrec=nrec, band=band, nbytes=len(pay), apply_ms=(t_inc - t1) * 1e3, inc_ms=(t2 - t_inc) * 1e3, cum=cum, npts=npts, splat=splat)))
            finally:
                try: conn.close()
                except Exception: pass
    except Exception: reason = "net error"; traceback.print_exc()
    finally:
        try:
            if nrec > 0: pts, cols, splat = dec.map(); pts, cols, splat = np.asarray(pts, float), np.asarray(cols, np.uint8), float(splat)
            else: pts, cols, splat = np.zeros((0, 3)), np.zeros((0, 3), np.uint8), 0.0
        except Exception: traceback.print_exc(); pts, cols, splat = np.zeros((0, 3)), np.zeros((0, 3), np.uint8), 0.0
        write_ply(os.path.join(a.out, "recv_final.ply"), pts, cols)
        np.savetxt(os.path.join(a.out, "recv_final.txt"), np.array(poses).reshape(-1, 8), fmt="%.6f")
        with open(os.path.join(a.out, "recv_final.splat"), "w") as f: f.write(f"{splat:.6g}\n")
        log.write(f"# end: {reason}, {nrec} records, {cum/1e3:.1f} KB, {len(pts)} pts, 0 frames\n"); log.close()
        print(f"[recv] done ({reason}): {nrec} records, {cum/1e3:.1f} KB, {len(pts):,} pts, {len(poses)} poses -> {a.out}", flush=True)
        q.put(("end", reason, nrec, cum, len(pts)))


# ---------------------------------------------------------------------------------------------------------------------
class NetPanel:
    """Network statistics window (OpenCV, next to the 3D view; also composited into the screenshots / recording).
    Fed from the GUI loop: add(kind, bytes, t) per frame, stat(fields, t) per sender STAT; render() ~4x/s."""
    def __init__(self, budget_kbit, h, x, y, show=True):
        import cv2; self.cv2 = cv2; self.budget = float(budget_kbit); self.W, self.H = 440, max(h, 420)
        self.ev = []; self.st = None; self.t_st = None; self.t_rec = None; self.t_pose = None; self.t0 = None; self.img = None
        self.lat = []; self.fps = 0.0; self.npts = 0; self.nrec = 0; self.total = 0; self.ended = None; self.name = "network statistics"; self.show = show
        if show:
            try: cv2.namedWindow(self.name, cv2.WINDOW_AUTOSIZE); cv2.moveWindow(self.name, x, y)
            except Exception as e: print(f"[gui] stats window unavailable ({e}); stats only in screenshots", flush=True); self.show = False
    def add(self, kind, nbytes, t):
        self.t0 = self.t0 or t; self.ev.append((t, nbytes, kind)); self.total += nbytes
        if kind == "rec": self.t_rec = t; self.nrec += 1
        elif kind == "pose": self.t_pose = t
    def stat(self, f, t): self.st, self.t_st = f, t
    def _rate(self, now, win, kinds=None):
        b = sum(n for t, n, k in self.ev if now - t <= win and (kinds is None or k in kinds)); return b * 8 / 1e3 / win
    def _count(self, now, win, kind): return sum(1 for t, n, k in self.ev if now - t <= win and k == kind) / win
    def status(self, now):
        if self.ended: return "STREAM ENDED (" + self.ended + ")", (160, 160, 160)
        if self.t0 is None: return "WAITING FOR SENDER", (0, 200, 255)
        last = max(t for t, _, _ in self.ev[-1:]) if self.ev else self.t0
        if now - last > 3: return f"NO DATA FROM SENDER for {now - last:.0f} s (link stalled?)", (60, 60, 255)
        st = self.st; rec_age = now - self.t_rec if self.t_rec else None
        if st is not None and st[6] & 2: return "WAITING FOR SLAM (needs 2nd keyframe)", (0, 200, 255)
        if rec_age is not None and rec_age > 2 and st is not None and st[6] & 1: return "MAP UP TO DATE - nothing new to send", (80, 200, 80)
        if rec_age is not None and rec_age > 2 and st is not None and st[4] > 0: return f"LINK BACKLOG {st[4]/1e3:.1f} KB", (0, 140, 255)
        if self.t_rec is None: return "WAITING FOR FIRST MAP PACKET", (0, 200, 255)
        return "STREAMING", (80, 200, 80)
    def render(self, now):
        cv2 = self.cv2; W, H = self.W, self.H; img = np.full((H, W, 3), 32, np.uint8); y = [0]
        def line(txt, col=(230, 230, 230), sc=0.5, th=1, dy=22):
            y[0] += dy; cv2.putText(img, txt, (12, y[0]), cv2.FONT_HERSHEY_SIMPLEX, sc, col, th, cv2.LINE_AA)
        def age(t): return "-" if t is None else f"{now - t:.1f} s ago"
        line("NETWORK", (255, 255, 255), 0.7, 2, 28)
        txt, col = self.status(now); line(txt, col, 0.5, 2, 26)
        r1, r5 = self._rate(now, 1.0), self._rate(now, 5.0); el = (now - self.t0) if self.t0 else 0.0
        line(f"link   {r1:5.1f} kbit/s now   {r5:5.1f} avg 5 s", (255, 255, 255), 0.55, 1, 30)
        line(f"budget {self.budget:5.1f} kbit/s   used {100 * r5 / max(self.budget, 1e-9):3.0f} %")
        line(f"total  {self.total / 1e3:6.1f} KB in {el:4.0f} s  = {self.total * 8 / 1e3 / max(el, 1e-9):4.1f} kbit/s mean")
        line("MAP", (255, 200, 120), 0.55, 1, 30)
        lat = f"{np.mean(self.lat[-10:]):.0f} ms" if self.lat else "-"
        line(f"packets {self.nrec}   {self._count(now, 5.0, 'rec'):4.1f}/s   last {age(self.t_rec)}")
        line(f"map rate {self._rate(now, 5.0, ('rec',)):5.1f} kbit/s   arrival->visible {lat}")
        line(f"points on screen {self.npts:,}   render {self.fps:4.1f} fps")
        line("CAMERA POSE", (120, 200, 255), 0.55, 1, 30)
        pa = (now - self.t_pose) if self.t_pose else None
        line(f"{self._count(now, 5.0, 'pose'):4.1f} Hz   last {age(self.t_pose)}   {self._rate(now, 5.0, ('pose',)):4.1f} kbit/s",
             (60, 60, 255) if (pa is not None and pa > 2) else (230, 230, 230))
        if pa is not None and pa > 2: line("SLAM not tracking (clip ended / lost)", (60, 60, 255))
        line("SENDER", (200, 200, 200), 0.55, 1, 30)
        if self.st is not None:
            k, nkf, npts, chunk, backlog, enc, fl = self.st
            line(f"keyframes {nkf}   cloud {npts:,} pts   slot {k}")
            line(f"last slot {chunk} B   encode {enc * 1e3:.0f} ms   backlog {backlog} B")
            line(f"status {age(self.t_st)}", (160, 160, 160), 0.45, 1, 20)
        else: line("no status yet")
        # bandwidth graph: last 60 s, 1 s bins, stacked map / pose / status, budget line
        gx0, gy0, gw, gh = 12, y[0] + 16, W - 24, H - y[0] - 40
        if gh > 60:
            nb = 60; bins = np.zeros((3, nb)); kinds = {"rec": 0, "pose": 1, "stat": 2}
            for t, n, k in self.ev:
                i = int(now - t)
                if 0 <= i < nb: bins[kinds.get(k, 2), nb - 1 - i] += n * 8 / 1e3
            top = max(self.budget * 1.4, bins.sum(0).max() * 1.1, 1.0)
            cv2.rectangle(img, (gx0, gy0), (gx0 + gw, gy0 + gh), (70, 70, 70), 1)
            bw = gw / nb
            for i in range(nb):
                yb = gy0 + gh
                for c, colr in ((0, (255, 170, 60)), (1, (60, 170, 255)), (2, (150, 150, 150))):
                    hh = int(gh * bins[c, i] / top)
                    if hh > 0: cv2.rectangle(img, (int(gx0 + i * bw + 1), yb - hh), (int(gx0 + (i + 1) * bw - 1), yb), colr, -1); yb -= hh
            yb = int(gy0 + gh - gh * self.budget / top)
            for xx in range(gx0, gx0 + gw, 10): cv2.line(img, (xx, yb), (min(xx + 5, gx0 + gw), yb), (60, 60, 255), 1)
            cv2.putText(img, f"budget {self.budget:.0f}", (gx0 + gw - 95, yb - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (60, 60, 255), 1, cv2.LINE_AA)
            xx = gx0
            for txt, colr in (("kbit/s per second, last 60 s:", (200, 200, 200)), ("map", (255, 170, 60)), ("pose", (60, 170, 255)), ("status", (150, 150, 150))):
                cv2.putText(img, txt, (xx, gy0 + gh + 16), cv2.FONT_HERSHEY_SIMPLEX, 0.4, colr, 1, cv2.LINE_AA)
                xx += cv2.getTextSize(txt, cv2.FONT_HERSHEY_SIMPLEX, 0.4, 1)[0][0] + 10
        self.ev = [e for e in self.ev if now - e[0] <= 61]
        self.img = img
        if self.show:
            try: cv2.imshow(self.name, img)
            except Exception as e: print(f"[gui] stats window failed ({e})", flush=True); self.show = False
        return img
    def pump(self):
        if self.show:
            try: self.cv2.waitKey(1)
            except Exception: self.show = False

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--listen", default="0.0.0.0:5555"); ap.add_argument("--out", default="/home/matteo/Documents/3d/slam_results/00_LIVE_E2E/run_gui")
    ap.add_argument("--streamer", default=None, help="streamer module (only make_decoder is used); default stream_best.py else stream_lod_conf.py")
    ap.add_argument("--accept-timeout", type=float, default=600.0, help="s to wait for the sender"); ap.add_argument("--idle-timeout", type=float, default=120.0, help="s without data before finishing")
    ap.add_argument("--rate-kbit", type=float, default=20.0, help="label only"); ap.add_argument("--draw", default="cubes", choices=["cubes", "hybrid", "quads", "points"])
    ap.add_argument("--fps", type=float, default=30.0, help="render loop cap"); ap.add_argument("--size", default="960x540"); ap.add_argument("--title", default="map_receiver_gui (live)")
    ap.add_argument("--hold", type=float, default=3.0, help="s to keep the window after the stream ends (-1 = until closed)")
    ap.add_argument("--screenshot", default=None); ap.add_argument("--screenshot-after", type=float, default=25.0, help="s after the first record")
    ap.add_argument("--snap-every", type=float, default=0.0, help="s between screen captures into recv_frames/ (0 = off)")
    ap.add_argument("--no-stats", action="store_true", help="no network statistics window");
    ap.add_argument("--view", default="third", choices=["third", "first", "top"], help="start view (keys: 1 first person, 3 third person, T top, V cycle, F free mouse)")
    ap.add_argument("--no-follow", action="store_true"); ap.add_argument("--cam", default="12,6,8", help="follow camera back,height,ahead in metres (scene unit = 40 m)")
    a = ap.parse_args()
    streamer = a.streamer or default_streamer(); os.makedirs(os.path.join(a.out, "recv_frames"), exist_ok=True)
    glog = open(os.path.join(a.out, "recv_gui_log.txt"), "a"); glog.write(f"# map_receiver_gui {time.strftime('%Y-%m-%d %H:%M:%S')} draw={a.draw} fps_cap={a.fps:g}\n# REC t_rel_s rec band bytes apply_ms incmap_ms queue_ms upload_ms arrival_to_visible_ms fps | SEC t_rel_s fps cpu_gui_pct cpu_dec_pct nvert pts\n"); glog.flush()
    ctx = mp.get_context("fork"); q = ctx.Queue(); stop = ctx.Event(); S = dict(poses=[], nrec=0, cum=0, t0=None, reason="socket closed", npts=0, splat=0.0)
    proc = ctx.Process(target=net_proc, args=(a, streamer, q, stop), daemon=True); proc.start()  # before any GL
    import open3d as o3d
    W, H = (int(x) for x in a.size.lower().split("x"))
    vis = o3d.visualization.VisualizerWithKeyCallback()
    if not vis.create_window(a.title, W, H, 10, 40): raise SystemExit("[gui] create_window failed (run with: env -u WAYLAND_DISPLAY XDG_SESSION_TYPE=x11)")
    ro = vis.get_render_option(); ro.background_color = np.array([184, 209, 237]) / 255.0; ro.light_on = False; ro.mesh_show_back_face = True; ro.point_size = 3.0
    cam = Camera(vis, *[float(x) for x in a.cam.split(",")], mode=a.view); cam.follow = not a.no_follow; cam.fx = cam.fx1 if a.view == "first" else cam.fx3
    panel = None
    try: panel = NetPanel(a.rate_kbit, H, 10 + W + 12, 40, show=not a.no_stats)
    except Exception as e: print(f"[gui] no network panel ({e!r})", flush=True)
    shots = dict(n=0)
    def shot(path=None):
        p = path or os.path.join(a.out, "recv_frames", f"shot_{shots['n']:04d}.png"); shots["n"] += 1
        try:
            vis.capture_screen_image(p, do_render=True)
            if panel is not None and panel.img is not None:
                import cv2; im = cv2.imread(p)
                if im is not None:
                    pi = panel.img if panel.img.shape[0] == im.shape[0] else cv2.resize(panel.img, (int(panel.W * im.shape[0] / panel.img.shape[0]) // 2 * 2, im.shape[0]))
                    cv2.imwrite(p, np.hstack([im, pi]))
            print(f"[gui] screenshot -> {p}", flush=True)
        except Exception: traceback.print_exc()
    S_last_pose = []
    def key_follow(v): cam.follow = not cam.follow; cam.dirty = True; print(f"[gui] camera {cam.label()}", flush=True); return False
    def key_view(mode):
        def f(v): cam.set_mode(mode); sync_drone(); return False
        return f
    def key_cycle(v):
        order = ["third", "first", "top"]; cam.set_mode(order[(order.index(cam.mode) + 1) % 3]); sync_drone(); return False
    def key_shot(v): shot(); return False
    vis.register_key_callback(ord("F"), key_follow); vis.register_key_callback(ord("T"), key_view("top")); vis.register_key_callback(ord("P"), key_shot)
    vis.register_key_callback(ord("1"), key_view("first")); vis.register_key_callback(ord("3"), key_view("third")); vis.register_key_callback(ord("V"), key_cycle)
    drone = o3d.geometry.TriangleMesh.create_coordinate_frame(size=1.5 / M_GRID); drone_V = np.asarray(drone.vertices).copy(); drone_added = False
    drone_shown = [False]
    def sync_drone():  # the marker is in the scene only when the view is not first person
        want = drone_added and cam.mode != "first"
        if want and not drone_shown[0]: vis.add_geometry(drone, reset_bounding_box=False); drone_shown[0] = True
        elif not want and drone_shown[0]: vis.remove_geometry(drone, reset_bounding_box=False); drone_shown[0] = False
    path_ls = o3d.geometry.LineSet(); path_added = False
    bands = {}; title_proc = [None]
    def set_title(txt):
        if title_proc[0] is not None and title_proc[0].poll() is None: return
        try: title_proc[0] = subprocess.Popen(["xprop", "-name", a.title, "-f", "_NET_WM_NAME", "8u", "-set", "_NET_WM_NAME", txt], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception: title_proc[0] = None
    period = 1.0 / max(a.fps, 1.0); n_frames, t_sec, cpu0, cpud0 = 0, time.time(), cpu_seconds(), cpu_seconds(proc.pid); fps = 0.0; lat_all = []; up_all = []; fps_all = []
    t_panel = 0.0
    t_first_rec, t_end, t_snap, ended = None, None, 0.0, False; pending = []; first_bbox = True; last_ps = None
    print(f"[gui] window {W}x{H}, draw={a.draw}, fps cap {a.fps:g}; keys: 1 first person, 3 third person, T top, V cycle views, F free mouse, P screenshot, Q quit", flush=True)
    try:
        while True:
            t_frame = time.time()
            # ---- drain the decode queue (geometry uploads happen here, on the GL thread)
            while True:
                try: item = q.get_nowait()
                except queue.Empty: break
                if item[0] == "pose":
                    if panel is not None: panel.add("pose", 80, item[4])
                    row = item[1]; S["poses"].append(row); S["t0"] = S["t0"] or (time.time() - item[3]); S_last_pose[:] = [row]; cam.set_pose(row); R, c = pose_RC(row)
                    drone.vertices = o3d.utility.Vector3dVector(drone_V @ R.T + c)
                    if not drone_added:
                        vis.add_geometry(drone, reset_bounding_box=first_bbox); drone_added = True; drone_shown[0] = True; first_bbox = False; sync_drone()
                    elif drone_shown[0]: vis.update_geometry(drone)
                    P = np.array([p[1:4] for p in S["poses"]], float).reshape(-1, 3)
                    if len(P) >= 2:
                        path_ls.points = o3d.utility.Vector3dVector(P); path_ls.lines = o3d.utility.Vector2iVector(np.stack([np.arange(len(P) - 1), np.arange(1, len(P))], 1)); path_ls.colors = o3d.utility.Vector3dVector(np.tile([[1.0, 0.2, 0.1]], (len(P) - 1, 1)))
                        if not path_added: vis.add_geometry(path_ls, reset_bounding_box=False); path_added = True
                        else: vis.update_geometry(path_ls)
                elif item[0] == "stat":
                    if panel is not None: panel.add("stat", item[3], item[2]); panel.stat(item[1], item[2])
                elif item[0] == "rec":
                    evs, meta = item[1], item[2]; t_u = time.time(); S["nrec"], S["cum"], S["npts"], S["splat"] = meta["nrec"], meta["cum"], meta["npts"], meta["splat"]; S["t0"] = S["t0"] or (t_u - meta["trel"])
                    for ev in evs:
                        bg = bands.get(ev["bi"])
                        if bg is None: bg = bands[ev["bi"]] = BandGeom(vis, o3d)
                        bg.apply(ev, first_bbox=first_bbox and not drone_added); first_bbox = False
                    meta["queue_ms"] = (t_u - meta["t_dec"]) * 1e3; meta["upload_ms"] = (time.time() - t_u) * 1e3; up_all.append(meta["upload_ms"]); pending.append(meta)
                    if t_first_rec is None: t_first_rec = time.time()
                    if panel is not None: panel.add("rec", meta["nbytes"] + 4, meta["t_arr"]); panel.npts = S["npts"]
                elif item[0] == "end":
                    if panel is not None: panel.ended = str(item[1])
                    ended, t_end = True, time.time(); S["reason"], S["nrec"], S["cum"] = item[1], item[2], item[3]; print(f"[gui] stream ended ({item[1]}); holding {a.hold:g} s", flush=True)
            # ---- camera + point size from the splat (pixel size of a splat at the drone's distance, as the numpy renderer)
            dist = cam.step(); sp = S["splat"]
            if dist and sp > 0:
                ps = float(np.clip(cam.fx * sp / max(dist, 1e-3), 1.0, 20.0))
                if last_ps is None or abs(ps - last_ps) > 0.5: ro.point_size = ps; last_ps = ps
            if panel is not None:
                if time.time() - t_panel >= 0.25: t_panel = time.time(); panel.fps = fps; panel.render(t_panel)
                panel.pump()
            if not vis.poll_events(): S["reason"] = "window closed"; break
            vis.update_renderer(); t_vis = time.time(); n_frames += 1
            for meta in pending:
                lat = (t_vis - meta["t_arr"]) * 1e3; lat_all.append(lat)
                if panel is not None: panel.lat.append(lat)
                glog.write(f"REC {meta['trel']:.3f} {meta['nrec']} {meta['band']} {meta['nbytes']} {meta['apply_ms']:.1f} {meta['inc_ms']:.1f} {meta['queue_ms']:.1f} {meta['upload_ms']:.1f} {lat:.1f} {fps:.1f}\n")
                print(f"[gui] record {meta['nrec']} visible: apply {meta['apply_ms']:.0f} ms + incmap {meta['inc_ms']:.0f} + queue {meta['queue_ms']:.0f} + upload {meta['upload_ms']:.1f} -> arrival->visible {lat:.0f} ms", flush=True)
            if pending: glog.flush(); pending = []
            if t_vis - t_sec >= 1.0:
                fps = n_frames / (t_vis - t_sec); c1, d1 = cpu_seconds(), cpu_seconds(proc.pid); cpu = 100.0 * (c1 - cpu0) / (t_vis - t_sec); cpud = 100.0 * (d1 - cpud0) / (t_vis - t_sec); cpu0, cpud0 = c1, d1; n_frames, t_sec = 0, t_vis
                nvert = sum(b.nvert for b in bands.values()); trel = (t_vis - S["t0"]) if S["t0"] else 0.0
                if t_first_rec and not ended: fps_all.append(fps)
                glog.write(f"SEC {trel:.3f} {fps:.1f} {cpu:.0f} {cpud:.0f} {nvert} {S['npts']}\n"); glog.flush()
                lat_txt = f"  lat {np.mean(lat_all[-10:]):.0f} ms" if lat_all else ""
                set_title(f"{a.title}  |  {fps:.0f} fps  |  rec {S['nrec']}  {S['cum']/1e3:.1f} KB  t={trel:.0f}s  |  {S['npts']:,} pts ({nvert:,} vert){lat_txt}  |  {cam.label()}")
                print(f"[gui] {trel:6.1f}s {fps:5.1f} fps  cpu gui {cpu:3.0f}% dec {cpud:3.0f}%  {nvert:,} vertices  {S['npts']:,} pts  rec {S['nrec']}{lat_txt}", flush=True)
            if a.screenshot and t_first_rec and shots["n"] == 0 and (t_vis - t_first_rec >= a.screenshot_after or (ended and t_vis - t_end >= min(1.0, max(a.hold, 0)))): shot(a.screenshot)
            if a.snap_every > 0 and t_first_rec and t_vis - t_snap >= a.snap_every: t_snap = t_vis; shot()
            if ended and a.hold >= 0 and t_vis - t_end >= a.hold: break
            rest = period - (time.time() - t_frame)
            if rest > 0: time.sleep(rest)
    except KeyboardInterrupt: S["reason"] = "interrupted"
    finally:
        stop.set()
        if a.screenshot and shots["n"] == 0 and t_first_rec:
            try: shot(a.screenshot)
            except Exception: pass
        try: vis.destroy_window()
        except Exception: pass
        if panel is not None and panel.show:
            try: panel.cv2.destroyAllWindows()
            except Exception: pass
        proc.join(timeout=15.0)
        if proc.is_alive(): proc.terminate()
        lat = np.array(lat_all) if lat_all else np.zeros(1); up = np.array(up_all) if up_all else np.zeros(1); fa = np.array(fps_all) if fps_all else np.zeros(1)
        summ = (f"# end: {S['reason']}, {S['nrec']} records, {S['cum']/1e3:.1f} KB, {S['npts']} pts | render fps while streaming mean {fa.mean():.1f} min {fa.min():.1f}"
                f" | arrival->visible mean {lat.mean():.0f} ms median {np.median(lat):.0f} max {lat.max():.0f} | upload mean {up.mean():.1f} ms max {up.max():.1f} | {shots['n']} screenshots\n")
        glog.write(summ); glog.close(); print(f"[gui] {summ.strip()}", flush=True)
    return 0


if __name__ == "__main__": sys.exit(main())
