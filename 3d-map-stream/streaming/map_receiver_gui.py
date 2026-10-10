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
M_GRID = 40.0  # lod_common.M: band levels are given in units of 1/40 m ... i.e. voxel size = level / M (as Decoder.map)
keyf = lambda u: (u[:, 0].astype(np.int64) << 42) | (u[:, 1].astype(np.int64) << 21) | u[:, 2].astype(np.int64)
QUAD = np.array([[0, .5, 0], [1, .5, 0], [1, .5, 1], [0, .5, 1]], float)  # flat tile at mid height of the voxel (ground frame y = normal)
QUAD_TRI = np.array([[0, 1, 2], [0, 2, 3]], np.int32)


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
    """Reads <I len><payload> frames from a socket; returns None on EOF, raises TimeoutError after idle_timeout s."""
    def __init__(self, sock, idle_timeout, tick=None):
        self.s, self.buf, self.idle, self.tick = sock, b"", idle_timeout, tick; sock.settimeout(0.2)
    def _fill(self, n):
        last = time.time()
        while len(self.buf) < n:
            try: d = self.s.recv(65536)
            except socket.timeout:
                if self.tick: self.tick()
                if time.time() - last > self.idle: raise TimeoutError(f"no data for {self.idle:.0f} s")
                continue
            if not d: return False
            self.buf += d; last = time.time()
        return True
    def read(self):
        if not self._fill(4): return None
        n = struct.unpack("<I", self.buf[:4])[0]
        if n > 64 << 20: raise ValueError(f"absurd frame length {n}")
        if not self._fill(4 + n): return None
        p = self.buf[4:4 + n]; self.buf = self.buf[4 + n:]; return p


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
    def __init__(self, draw="hybrid"): self.ready = False; self.draw = draw

    def _init(self, dec):
        self.R, self.o, self.spl = np.asarray(dec.R, float), np.asarray(dec.o, float), float(dec.spl)
        self.v = np.asarray(dec.levels, float) / M_GRID; nb = len(self.v); self.nb = nb
        self.u = [np.zeros((0, 3), np.int64) for _ in range(nb)]; self.col = [np.zeros((0, 3), np.uint8) for _ in range(nb)]
        self.alive = [np.zeros(0, bool) for _ in range(nb)]; self.k2i = [dict() for _ in range(nb)]; self.covered = [set() for _ in range(nb)]
        self.vmin = None; self.ready = True

    def _kind(self, bi): return "points" if (self.draw == "points" or (self.draw == "hybrid" and self.v[bi] <= self.vmin + 1e-9)) else "mesh"

    def _blocks(self, bi, u):
        v = self.v[bi]
        if self._kind(bi) == "mesh": off = QUAD
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
    """One Open3D geometry per band (PointCloud, or flat-quad TriangleMesh), grown by Vector3dVector.extend().
    Voxels that get covered later are killed IN PLACE (their vertices are moved to one far point through the numpy
    view of the vertex buffer -> degenerate / clipped) so the geometry is never rebuilt except on a reset event."""
    FAR = np.array([1e5, 1e5, 1e5])
    def __init__(self, vis, o3d):
        self.vis, self.o3d, self.kind, self.geo, self.added, self.nvert, self.m = vis, o3d, None, None, False, 0, 1
        self.vpos = np.zeros(0, np.int64)  # per voxel: first vertex index in the geometry, -1 = never drawn

    def _new_geo(self, kind):
        if self.added: self.vis.remove_geometry(self.geo, reset_bounding_box=False); self.added = False
        self.geo = self.o3d.geometry.TriangleMesh() if kind == "mesh" else self.o3d.geometry.PointCloud(); self.kind = kind; self.nvert = 0

    def _push(self, V, C):  # V (n, m, 3) world, C (n, 3) uint8 -> appends; returns vertex base of each block
        n, m = V.shape[:2]; V3 = np.ascontiguousarray(V.reshape(-1, 3)); C3 = np.repeat(C.astype(float) / 255.0, m, 0); base = self.nvert + m * np.arange(n)
        if self.kind == "mesh":
            tri = (base[:, None, None] + QUAD_TRI[None]).reshape(-1, 3)
            self.geo.vertices.extend(self.o3d.utility.Vector3dVector(V3)); self.geo.vertex_colors.extend(self.o3d.utility.Vector3dVector(C3)); self.geo.triangles.extend(self.o3d.utility.Vector3iVector(tri))
        else:
            self.geo.points.extend(self.o3d.utility.Vector3dVector(V3)); self.geo.colors.extend(self.o3d.utility.Vector3dVector(C3))
        self.nvert += len(V3); self.m = m; return base

    def apply(self, ev, first_bbox=False):
        reset = ev["reset"] or self.geo is None or self.kind != ev["kind"]
        if reset: self._new_geo(ev["kind"]); self.vpos = np.full(len(ev["verts"]), -1, np.int64); n0 = 0
        else: n0 = len(self.vpos); self.vpos = np.concatenate([self.vpos, np.full(len(ev["verts"]), -1, np.int64)])
        a = ev["alive"]
        if a.any(): self.vpos[n0 + np.nonzero(a)[0]] = self._push(ev["verts"][a], ev["cols"][a])
        if not reset and len(ev["dead"]):
            base = self.vpos[ev["dead"]]; base = base[base >= 0]
            if len(base):
                idx = (base[:, None] + np.arange(self.m)[None]).ravel(); V = np.asarray(self.geo.vertices if self.kind == "mesh" else self.geo.points); V[idx] = self.FAR
        if not self.added: self.vis.add_geometry(self.geo, reset_bounding_box=first_bbox); self.added = True
        else: self.vis.update_geometry(self.geo)


class Camera:
    """Follow camera: behind/above the latest POSE looking ahead (smoothed), or free (Open3D mouse)."""
    def __init__(self, vis, back=12.0, height=6.0, ahead=8.0, tau=0.5, scale=1.0 / M_GRID):  # back/height/ahead in metres; scale = scene units per metre
        self.vis, self.ctr = vis, vis.get_view_control(); self.back, self.height, self.ahead, self.tau = back * scale, height * scale, ahead * scale, tau
        self.follow, self.top, self.up = True, False, None; self.eye = self.tgt = None; self.goal = None; self.t_last = time.time(); self.dirty = True
        prm = self.ctr.convert_to_pinhole_camera_parameters(); self.fx = float(prm.intrinsic.intrinsic_matrix[0, 0])
        for fn, val in (("set_constant_z_near", 0.1), ("set_constant_z_far", 3000.0)):
            if hasattr(self.ctr, fn): getattr(self.ctr, fn)(val)

    def set_pose(self, row):
        R, c = pose_RC(row); up = self.up if self.up is not None else -R[:, 1]
        fwd = R[:, 2] - up * (R[:, 2] @ up); fwd = fwd / (np.linalg.norm(fwd) + 1e-9)
        if self.top: eye, tgt = c + up * (self.back + self.height) * 1.6, c + fwd * 1e-3
        else: eye, tgt = c - fwd * self.back + up * self.height, c + fwd * self.ahead
        self.goal = (eye, tgt, up); self.dirty = True
        if self.eye is None: self.eye, self.tgt = eye.copy(), tgt.copy()

    def step(self):
        """-> distance eye->drone target (for the point size); applies the extrinsic only while moving."""
        now = time.time(); dt = now - self.t_last; self.t_last = now
        if self.goal is None: return None
        eye, tgt, up = self.goal
        if not self.follow: return float(np.linalg.norm(self._eye_free() - tgt))
        a = 1.0 - np.exp(-dt / self.tau); self.eye += (eye - self.eye) * a; self.tgt += (tgt - self.tgt) * a
        if not self.dirty: return float(np.linalg.norm(self.eye - tgt))
        if np.linalg.norm(eye - self.eye) < 0.01 and np.linalg.norm(tgt - self.tgt) < 0.01: self.eye, self.tgt, self.dirty = eye.copy(), tgt.copy(), False
        z = self.tgt - self.eye; z /= np.linalg.norm(z) + 1e-9; x = np.cross(z, up); x /= np.linalg.norm(x) + 1e-9; y = np.cross(z, x)
        Rwc = np.stack([x, y, z]); E = np.eye(4); E[:3, :3] = Rwc; E[:3, 3] = -Rwc @ self.eye
        prm = self.ctr.convert_to_pinhole_camera_parameters(); prm.extrinsic = E; self.ctr.convert_from_pinhole_camera_parameters(prm, allow_arbitrary=True)
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
    poses, nrec, cum, t0, reason = [], 0, 0, None, "socket closed"
    try:
        host, port = a.listen.rsplit(":", 1); srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((host, int(port))); srv.listen(1); srv.settimeout(0.5); print(f"[recv] listening on {a.listen}, out={a.out}, streamer={os.path.basename(streamer)}", flush=True)
        conn, t_start = None, time.time()
        while conn is None:
            if stop.is_set(): reason = "window closed"; break
            try: conn, addr = srv.accept(); print(f"[recv] sender connected from {addr}", flush=True)
            except socket.timeout:
                if time.time() - t_start > a.accept_timeout: print("[recv] no sender, giving up", flush=True); reason = "no sender"; break
        srv.close()
        if conn is not None:
            def tick():
                if stop.is_set(): raise TimeoutError("window closed")
            rd = FrameReader(conn, a.idle_timeout, tick=tick)
            try:
                while True:
                    try: pay = rd.read()
                    except TimeoutError as e: reason = str(e); break
                    if pay is None: break
                    now = time.time(); t0 = t0 or now; cum += len(pay) + 4; trel = now - t0
                    if len(pay) == POSE_LEN and pay[:4] == b"POSE":
                        row = np.frombuffer(pay[4:68], np.float64).copy(); dt_slot = struct.unpack("<d", pay[68:76])[0]; poses.append(row)
                        log.write(f"{trel:.3f} {now:.3f} POSE {len(pay)+4} - {cum/1e3:.3f} {imap.npts()}\n"); log.flush()
                        print(f"[recv] {trel:6.1f}s POSE kf t={row[0]:.2f} dt={dt_slot:.2f}", flush=True); q.put(("pose", row, dt_slot, trel)); continue
                    first = getattr(dec, "R", None) is None
                    band = "hdr" if first else (f"b{pay[0]}" if len(pay) >= 5 and struct.unpack("<I", pay[1:5])[0] == len(pay) - 5 else "chunk")
                    t1 = time.time(); evs = []
                    try:
                        dec.apply(pay); applied = True; t_inc = time.time()
                        try: evs = imap.ingest(dec)
                        except Exception:
                            print("[recv] incremental map failed, falling back to dec.map():", flush=True); traceback.print_exc()
                            p2, c2, sp = dec.map(); evs = [dict(bi=0, kind="points", reset=True, verts=np.asarray(p2, float)[:, None, :], cols=np.asarray(c2, np.uint8), alive=np.ones(len(p2), bool), dead=np.zeros(0, np.int64))]
                    except Exception:
                        applied = False; print(f"[recv] apply FAILED on record {nrec} ({len(pay)} B):", flush=True); traceback.print_exc()
                    t2 = time.time(); tdec = t2 - t1
                    if applied:
                        nrec += 1; npts = imap.npts()
                        log.write(f"{trel:.3f} {now:.3f} REC {len(pay)+4} {band} {cum/1e3:.3f} {npts}\n"); log.flush()
                        print(f"[recv] {trel:6.1f}s record {nrec} band {band} {len(pay)} B  cum {cum/1e3:.1f} KB  pts {npts:,}  vox {imap.nvox()}  apply {(t_inc-t1)*1e3:.0f} ms + map {(t2-t_inc)*1e3:.0f} ms", flush=True)
                        q.put(("rec", evs, dict(t_arr=now, t_dec=t2, trel=trel, nrec=nrec, band=band, nbytes=len(pay), apply_ms=(t_inc - t1) * 1e3, inc_ms=(t2 - t_inc) * 1e3, cum=cum, npts=npts, splat=imap.splat())))
            finally:
                try: conn.close()
                except Exception: pass
    except Exception: reason = "net error"; traceback.print_exc()
    finally:
        try:
            if getattr(dec, "R", None) is not None: pts, cols, splat = dec.map(); pts, cols, splat = np.asarray(pts, float), np.asarray(cols, np.uint8), float(splat)
            else: pts, cols, splat = np.zeros((0, 3)), np.zeros((0, 3), np.uint8), 0.0
        except Exception: traceback.print_exc(); pts, cols, splat = np.zeros((0, 3)), np.zeros((0, 3), np.uint8), 0.0
        write_ply(os.path.join(a.out, "recv_final.ply"), pts, cols)
        np.savetxt(os.path.join(a.out, "recv_final.txt"), np.array(poses).reshape(-1, 8), fmt="%.9g")
        with open(os.path.join(a.out, "recv_final.splat"), "w") as f: f.write(f"{splat:.6g}\n")
        log.write(f"# end: {reason}, {nrec} records, {cum/1e3:.1f} KB, {len(pts)} pts, 0 frames\n"); log.close()
        print(f"[recv] done ({reason}): {nrec} records, {cum/1e3:.1f} KB, {len(pts):,} pts, {len(poses)} poses -> {a.out}", flush=True)
        q.put(("end", reason, nrec, cum, len(pts)))


# ---------------------------------------------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--listen", default="0.0.0.0:5555"); ap.add_argument("--out", default="/home/matteo/Documents/3d/slam_results/00_LIVE_E2E/run_gui")
    ap.add_argument("--streamer", default=None, help="streamer module (only make_decoder is used); default stream_best.py else stream_lod_conf.py")
    ap.add_argument("--accept-timeout", type=float, default=600.0, help="s to wait for the sender"); ap.add_argument("--idle-timeout", type=float, default=120.0, help="s without data before finishing")
    ap.add_argument("--rate-kbit", type=float, default=20.0, help="label only"); ap.add_argument("--draw", default="hybrid", choices=["hybrid", "quads", "points"])
    ap.add_argument("--fps", type=float, default=30.0, help="render loop cap"); ap.add_argument("--size", default="960x540"); ap.add_argument("--title", default="map_receiver_gui (live)")
    ap.add_argument("--hold", type=float, default=3.0, help="s to keep the window after the stream ends (-1 = until closed)")
    ap.add_argument("--screenshot", default=None); ap.add_argument("--screenshot-after", type=float, default=25.0, help="s after the first record")
    ap.add_argument("--snap-every", type=float, default=0.0, help="s between screen captures into recv_frames/ (0 = off)")
    ap.add_argument("--no-follow", action="store_true"); ap.add_argument("--cam", default="12,6,8", help="follow camera back,height,ahead in metres (scene unit = 40 m)")
    a = ap.parse_args()
    streamer = a.streamer or default_streamer(); os.makedirs(os.path.join(a.out, "recv_frames"), exist_ok=True)
    glog = open(os.path.join(a.out, "recv_gui_log.txt"), "a"); glog.write(f"# map_receiver_gui {time.strftime('%Y-%m-%d %H:%M:%S')} draw={a.draw} fps_cap={a.fps:g}\n# REC t_rel_s rec band bytes apply_ms incmap_ms queue_ms upload_ms arrival_to_visible_ms fps | SEC t_rel_s fps cpu_gui_pct cpu_dec_pct nvert pts\n"); glog.flush()
    ctx = mp.get_context("fork"); q = ctx.Queue(); stop = ctx.Event(); S = dict(poses=[], nrec=0, cum=0, t0=None, reason="socket closed", npts=0, splat=0.0)
    proc = ctx.Process(target=net_proc, args=(a, streamer, q, stop), daemon=True); proc.start()  # before any GL
    import open3d as o3d
    W, H = (int(x) for x in a.size.lower().split("x"))
    vis = o3d.visualization.VisualizerWithKeyCallback()
    if not vis.create_window(a.title, W, H): raise SystemExit("[gui] create_window failed (run with: env -u WAYLAND_DISPLAY XDG_SESSION_TYPE=x11)")
    ro = vis.get_render_option(); ro.background_color = np.array([184, 209, 237]) / 255.0; ro.light_on = False; ro.mesh_show_back_face = True; ro.point_size = 3.0
    cam = Camera(vis, *[float(x) for x in a.cam.split(",")]); cam.follow = not a.no_follow
    shots = dict(n=0)
    def shot(path=None):
        p = path or os.path.join(a.out, "recv_frames", f"shot_{shots['n']:04d}.png"); shots["n"] += 1
        try: vis.capture_screen_image(p, do_render=True); print(f"[gui] screenshot -> {p}", flush=True)
        except Exception: traceback.print_exc()
    S_last_pose = []
    def key_follow(v): cam.follow = not cam.follow; cam.dirty = True; print(f"[gui] camera {'FOLLOW' if cam.follow else 'FREE (mouse)'}", flush=True); return False
    def key_top(v):
        cam.top = not cam.top; cam.follow = True; cam.dirty = True
        if S_last_pose: cam.set_pose(S_last_pose[0])
        return False
    def key_shot(v): shot(); return False
    vis.register_key_callback(ord("F"), key_follow); vis.register_key_callback(ord("T"), key_top); vis.register_key_callback(ord("P"), key_shot)
    drone = o3d.geometry.TriangleMesh.create_coordinate_frame(size=1.5 / M_GRID); drone_V = np.asarray(drone.vertices).copy(); drone_added = False
    path_ls = o3d.geometry.LineSet(); path_added = False
    bands = {}; title_proc = [None]
    def set_title(txt):
        if title_proc[0] is not None and title_proc[0].poll() is None: return
        try: title_proc[0] = subprocess.Popen(["xprop", "-name", a.title, "-f", "_NET_WM_NAME", "8u", "-set", "_NET_WM_NAME", txt], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception: title_proc[0] = None
    period = 1.0 / max(a.fps, 1.0); n_frames, t_sec, cpu0, cpud0 = 0, time.time(), cpu_seconds(), cpu_seconds(proc.pid); fps = 0.0; lat_all = []; up_all = []; fps_all = []
    t_first_rec, t_end, t_snap, ended = None, None, 0.0, False; pending = []; first_bbox = True; last_ps = None
    print(f"[gui] window {W}x{H}, draw={a.draw}, fps cap {a.fps:g}; keys: F free/follow, T top view, P screenshot, Q quit", flush=True)
    try:
        while True:
            t_frame = time.time()
            # ---- drain the decode queue (geometry uploads happen here, on the GL thread)
            while True:
                try: item = q.get_nowait()
                except queue.Empty: break
                if item[0] == "pose":
                    row = item[1]; S["poses"].append(row); S["t0"] = S["t0"] or (time.time() - item[3]); S_last_pose[:] = [row]; cam.set_pose(row); R, c = pose_RC(row)
                    drone.vertices = o3d.utility.Vector3dVector(drone_V @ R.T + c)
                    if not drone_added: vis.add_geometry(drone, reset_bounding_box=first_bbox); drone_added = True; first_bbox = False
                    else: vis.update_geometry(drone)
                    P = np.array([p[1:4] for p in S["poses"]], float).reshape(-1, 3)
                    if len(P) >= 2:
                        path_ls.points = o3d.utility.Vector3dVector(P); path_ls.lines = o3d.utility.Vector2iVector(np.stack([np.arange(len(P) - 1), np.arange(1, len(P))], 1)); path_ls.colors = o3d.utility.Vector3dVector(np.tile([[1.0, 0.2, 0.1]], (len(P) - 1, 1)))
                        if not path_added: vis.add_geometry(path_ls, reset_bounding_box=False); path_added = True
                        else: vis.update_geometry(path_ls)
                elif item[0] == "rec":
                    evs, meta = item[1], item[2]; t_u = time.time(); S["nrec"], S["cum"], S["npts"], S["splat"] = meta["nrec"], meta["cum"], meta["npts"], meta["splat"]; S["t0"] = S["t0"] or (t_u - meta["trel"])
                    for ev in evs:
                        bg = bands.get(ev["bi"])
                        if bg is None: bg = bands[ev["bi"]] = BandGeom(vis, o3d)
                        bg.apply(ev, first_bbox=first_bbox and not drone_added); first_bbox = False
                    meta["queue_ms"] = (t_u - meta["t_dec"]) * 1e3; meta["upload_ms"] = (time.time() - t_u) * 1e3; up_all.append(meta["upload_ms"]); pending.append(meta)
                    if t_first_rec is None: t_first_rec = time.time()
                elif item[0] == "end":
                    ended, t_end = True, time.time(); S["reason"], S["nrec"], S["cum"] = item[1], item[2], item[3]; print(f"[gui] stream ended ({item[1]}); holding {a.hold:g} s", flush=True)
            # ---- camera + point size from the splat (pixel size of a splat at the drone's distance, as the numpy renderer)
            dist = cam.step(); sp = S["splat"]
            if dist and sp > 0:
                ps = float(np.clip(cam.fx * sp / max(dist, 1e-3), 1.0, 20.0))
                if last_ps is None or abs(ps - last_ps) > 0.5: ro.point_size = ps; last_ps = ps
            if not vis.poll_events(): S["reason"] = "window closed"; break
            vis.update_renderer(); t_vis = time.time(); n_frames += 1
            for meta in pending:
                lat = (t_vis - meta["t_arr"]) * 1e3; lat_all.append(lat)
                glog.write(f"REC {meta['trel']:.3f} {meta['nrec']} {meta['band']} {meta['nbytes']} {meta['apply_ms']:.1f} {meta['inc_ms']:.1f} {meta['queue_ms']:.1f} {meta['upload_ms']:.1f} {lat:.1f} {fps:.1f}\n")
                print(f"[gui] record {meta['nrec']} visible: apply {meta['apply_ms']:.0f} ms + incmap {meta['inc_ms']:.0f} + queue {meta['queue_ms']:.0f} + upload {meta['upload_ms']:.1f} -> arrival->visible {lat:.0f} ms", flush=True)
            if pending: glog.flush(); pending = []
            if t_vis - t_sec >= 1.0:
                fps = n_frames / (t_vis - t_sec); c1, d1 = cpu_seconds(), cpu_seconds(proc.pid); cpu = 100.0 * (c1 - cpu0) / (t_vis - t_sec); cpud = 100.0 * (d1 - cpud0) / (t_vis - t_sec); cpu0, cpud0 = c1, d1; n_frames, t_sec = 0, t_vis
                nvert = sum(b.nvert for b in bands.values()); trel = (t_vis - S["t0"]) if S["t0"] else 0.0
                if t_first_rec and not ended: fps_all.append(fps)
                glog.write(f"SEC {trel:.3f} {fps:.1f} {cpu:.0f} {cpud:.0f} {nvert} {S['npts']}\n"); glog.flush()
                lat_txt = f"  lat {np.mean(lat_all[-10:]):.0f} ms" if lat_all else ""
                set_title(f"{a.title}  |  {fps:.0f} fps  |  rec {S['nrec']}  {S['cum']/1e3:.1f} KB  t={trel:.0f}s  |  {S['npts']:,} pts ({nvert:,} vert){lat_txt}  |  {'FOLLOW' if cam.follow else 'FREE'}")
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
        proc.join(timeout=15.0)
        if proc.is_alive(): proc.terminate()
        lat = np.array(lat_all) if lat_all else np.zeros(1); up = np.array(up_all) if up_all else np.zeros(1); fa = np.array(fps_all) if fps_all else np.zeros(1)
        summ = (f"# end: {S['reason']}, {S['nrec']} records, {S['cum']/1e3:.1f} KB, {S['npts']} pts | render fps while streaming mean {fa.mean():.1f} min {fa.min():.1f}"
                f" | arrival->visible mean {lat.mean():.0f} ms median {np.median(lat):.0f} max {lat.max():.0f} | upload mean {up.mean():.1f} ms max {up.max():.1f} | {shots['n']} screenshots\n")
        glog.write(summ); glog.close(); print(f"[gui] {summ.strip()}", flush=True)
    return 0


if __name__ == "__main__": sys.exit(main())
