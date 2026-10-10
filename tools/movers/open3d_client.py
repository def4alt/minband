#!/usr/bin/env python3
"""MinBand movers in Open3D: animate only what moves, on top of a static 3D reconstruction.

The MinBand server classifies every fused entity as moving or static (server/src/movers.ts) and
serves the movers at `GET /api/movers`, dead-reckoned to the request time, in the marker frame
(metres, +Y up, +X right, -Z forward; heading in degrees, 0 along -Z, 90 along +X). This client polls
that endpoint, dead-reckons between polls (pos + vel * dt) and moves one mesh per mover in an Open3D
window: a figure for a person, a box for a vehicle, a small box for anything else.

    python open3d_client.py --server http://192.168.0.16:8080 [--scene room.ply] [--transform T.txt]
    python open3d_client.py --server http://192.168.0.16:8080 --print        # no window, text only

`--transform T.txt` is a 4x4 matrix (4 lines of 4 numbers) mapping marker-frame coordinates into
the reconstruction's frame (align.py writes it from 3 picked points). Without it the marker frame
is the scene frame. `--scene` loads a point cloud or mesh (.ply .pcd .xyz .obj .glb .stl) as is.

Embedding in your own loop instead: `feed = Feed(url); feed.start()` and each frame
`for row, pos in feed.current(): ...` (row is the server's JSON row, pos the dead-reckoned position).
Needs numpy; open3d only for the window. Python 3.9+.
"""
import argparse
import io
import json
import math
import sys
import threading
import time
import urllib.error
import urllib.request

import numpy as np

# Marker-frame body sizes (x width, y height, z length), metres, standing on y = 0, facing -Z.
PERSON = 0
VEHICLE_SIZE = {2: (1.8, 1.5, 4.5), 5: (2.5, 3.0, 12.0), 7: (2.5, 3.0, 8.0), 101: (3.5, 2.5, 7.0)}
CYCLE_SIZE = {1: (0.6, 1.2, 1.8), 3: (0.8, 1.3, 2.2)}
MOVER_SIZE = (0.6, 1.0, 0.6)      # class 100: a ground mover of unknown size
SMALL_SIZE = (0.4, 0.4, 0.4)      # chairs, cups, laptops...: what the tracker also carries
COLOR = {0: (0.92, 0.92, 0.92), 100: (1.0, 0.75, 0.2), 101: (0.85, 0.35, 0.3)}
VEHICLE_COLOR = (0.55, 0.75, 1.0)
OTHER_COLOR = (0.6, 0.6, 0.6)
STATIC_COLOR = (0.35, 0.35, 0.35)
BACKGROUND = (0.027, 0.031, 0.035)   # docs/STYLE.md #070809
MAX_DEAD_RECKON_S = 1.0              # beyond this, hold the last position
FEED_TIMEOUT_S = 2.0                 # no successful poll for this long: clear the scene


def load_transform(path):
    """4x4 from a text file (4 lines of 4 numbers, whitespace or comma separated)."""
    with open(path) as f:
        T = np.loadtxt(io.StringIO(f.read().replace(',', ' ')), dtype=float)
    if T.shape != (4, 4):
        raise SystemExit(f'{path}: expected a 4x4 matrix, got {T.shape}')
    return T


class Feed(threading.Thread):
    """Polls /api/movers at `hz`; `current()` gives the rows dead-reckoned to now."""

    def __init__(self, server, hz=20.0, include_static=False, timeout=1.0):
        super().__init__(daemon=True)
        self.url = server.rstrip('/') + '/api/movers' + ('?all=1' if include_static else '')
        self.period = 1.0 / hz
        self.timeout = timeout
        self.lock = threading.Lock()
        self.rows = {}            # gid -> row (server JSON)
        self.received = None      # time.monotonic() of the last good poll
        self.server_t = None      # server ms the rows are valid for
        self.polls = self.failures = 0
        self.error = None
        self.stop_event = threading.Event()

    def run(self):
        while not self.stop_event.is_set():
            t0 = time.monotonic()
            try:
                with urllib.request.urlopen(self.url, timeout=self.timeout) as r:
                    feed = json.loads(r.read().decode('utf-8'))
                with self.lock:
                    self.rows = {row['gid']: row for row in feed['movers']}
                    self.received = time.monotonic()
                    self.server_t = feed['t']
                    self.polls += 1
                    self.error = None
            except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
                with self.lock:
                    self.failures += 1
                    self.error = str(e)
            self.stop_event.wait(max(0.0, self.period - (time.monotonic() - t0)))

    def stop(self):
        self.stop_event.set()

    def current(self, now=None):
        """[(row, pos)] with pos = row.pos + row.vel * dt, dt since the poll (<= MAX_DEAD_RECKON_S).
        Empty when the last good poll is older than FEED_TIMEOUT_S."""
        now = time.monotonic() if now is None else now
        with self.lock:
            if self.received is None or now - self.received > FEED_TIMEOUT_S:
                return []
            dt = min(MAX_DEAD_RECKON_S, max(0.0, now - self.received))
            out = []
            for row in self.rows.values():
                p = np.asarray(row['pos'], float) + np.asarray(row['vel'], float) * dt
                out.append((row, p))
            return out

    def status(self):
        with self.lock:
            age = None if self.received is None else time.monotonic() - self.received
            return {'polls': self.polls, 'failures': self.failures, 'ageS': age, 'error': self.error}


# ---- geometry (marker frame) ------------------------------------------------------------------------

def heading_matrix(heading_deg):
    """Rotation about +Y so that the body's -Z points along the heading (0 -> -Z, 90 -> +X)."""
    a = -math.radians(heading_deg or 0.0)
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]])


def pose_matrix(pos, heading_deg, T=None):
    """4x4 placing a body at `pos` (feet / ground contact) facing `heading_deg`, then into the scene frame."""
    M = np.eye(4)
    M[:3, :3] = heading_matrix(heading_deg)
    M[:3, 3] = pos
    return M if T is None else T @ M


def body_class(cls):
    if cls == PERSON:
        return 'person'
    if cls in VEHICLE_SIZE or cls in CYCLE_SIZE or cls == 100:
        return 'box'
    return 'small'


def body_color(cls):
    if cls in COLOR:
        return COLOR[cls]
    if cls in VEHICLE_SIZE or cls in CYCLE_SIZE:
        return VEHICLE_COLOR
    return OTHER_COLOR


def make_body(o3d, cls):
    """A TriangleMesh in the marker frame: standing on y = 0 at the origin, facing -Z."""
    if cls == PERSON:
        trunk = o3d.geometry.TriangleMesh.create_cylinder(radius=0.18, height=1.45, resolution=16)
        trunk.rotate(np.array([[1, 0, 0], [0, 0, -1], [0, 1, 0]], float), center=(0, 0, 0))  # Z axis -> Y
        trunk.translate((0, 0.725, 0))
        head = o3d.geometry.TriangleMesh.create_sphere(radius=0.13, resolution=12)
        head.translate((0, 1.62, 0))
        nose = o3d.geometry.TriangleMesh.create_box(0.08, 0.08, 0.12)   # which way it faces
        nose.translate((-0.04, 1.55, -0.27))
        mesh = trunk + head + nose
    else:
        w, h, l = VEHICLE_SIZE.get(cls) or CYCLE_SIZE.get(cls) or (MOVER_SIZE if cls == 100 else SMALL_SIZE)
        mesh = o3d.geometry.TriangleMesh.create_box(w, h, l)
        mesh.translate((-w / 2, 0, -l / 2))
        if cls in VEHICLE_SIZE or cls in CYCLE_SIZE:
            cab = o3d.geometry.TriangleMesh.create_box(w * 0.6, h * 0.25, l * 0.15)   # a block on top at the front
            cab.translate((-w * 0.3, h, -l / 2 + l * 0.05))
            mesh += cab
    mesh.compute_vertex_normals()
    mesh.paint_uniform_color(body_color(cls))
    return mesh


def floor_grid(o3d, half=10, step=1.0, T=None):
    """A LineSet grid on y = 0 of the marker frame, mapped by T."""
    pts, lines = [], []
    n = int(half / step)
    for i in range(-n, n + 1):
        v = i * step
        pts += [(-half, 0, v), (half, 0, v), (v, 0, -half), (v, 0, half)]
        k = len(pts) - 4
        lines += [(k, k + 1), (k + 2, k + 3)]
    P = np.asarray(pts, float)
    if T is not None:
        P = (T[:3, :3] @ P.T).T + T[:3, 3]
    ls = o3d.geometry.LineSet(o3d.utility.Vector3dVector(P), o3d.utility.Vector2iVector(np.asarray(lines)))
    ls.paint_uniform_color((0.18, 0.2, 0.22))
    return ls


def load_scene(o3d, path):
    ext = path.lower().rsplit('.', 1)[-1]
    if ext in ('obj', 'glb', 'gltf', 'stl', 'off'):
        return o3d.io.read_triangle_mesh(path)
    if ext == 'ply':
        m = o3d.io.read_triangle_mesh(path)
        if len(m.triangles) > 0:
            m.compute_vertex_normals()
            return m
    return o3d.io.read_point_cloud(path)


# ---- the window --------------------------------------------------------------------------------------

class Bodies:
    """One mesh per mover gid, re-posed each frame from a cached base mesh."""

    def __init__(self, o3d, vis, T):
        self.o3d, self.vis, self.T = o3d, vis, T
        self.bases = {}     # class -> (vertices, normals, color)
        self.live = {}      # gid -> (mesh, class)

    def base(self, cls):
        b = self.bases.get(cls)
        if b is None:
            m = make_body(self.o3d, cls)
            b = (np.asarray(m.vertices).copy(), np.asarray(m.vertex_normals).copy(), m)
            self.bases[cls] = b
        return b

    def sync(self, rows):
        seen = set()
        for row, pos in rows:
            gid, cls = row['gid'], row['class']
            seen.add(gid)
            M = pose_matrix(pos, row.get('headingDeg'), self.T)
            R, t = M[:3, :3], M[:3, 3]
            V, N, proto = self.base(cls)
            ent = self.live.get(gid)
            if ent is None or ent[1] != cls:
                if ent is not None:
                    self.vis.remove_geometry(ent[0], reset_bounding_box=False)
                mesh = self.o3d.geometry.TriangleMesh(proto)
                mesh.vertices = self.o3d.utility.Vector3dVector((R @ V.T).T + t)
                mesh.vertex_normals = self.o3d.utility.Vector3dVector((R @ N.T).T)
                self.vis.add_geometry(mesh, reset_bounding_box=False)
                self.live[gid] = (mesh, cls)
                print(f"+ {gid} {row.get('name', cls)} at x={pos[0]:.2f} z={pos[2]:.2f}", flush=True)
            else:
                mesh = ent[0]
                mesh.vertices = self.o3d.utility.Vector3dVector((R @ V.T).T + t)
                mesh.vertex_normals = self.o3d.utility.Vector3dVector((R @ N.T).T)
            mesh.paint_uniform_color(body_color(cls) if row.get('moving', True) else STATIC_COLOR)
            self.vis.update_geometry(mesh)
        for gid in [g for g in self.live if g not in seen]:
            self.vis.remove_geometry(self.live.pop(gid)[0], reset_bounding_box=False)
            print(f"- {gid}", flush=True)


def run_window(args, T):
    import open3d as o3d  # imported here so --print needs no open3d
    vis = o3d.visualization.Visualizer()
    vis.create_window('MinBand movers', width=1280, height=800)
    opt = vis.get_render_option()
    opt.background_color = np.asarray(BACKGROUND)
    opt.point_size = 2.0
    opt.mesh_show_back_face = True
    vis.add_geometry(floor_grid(o3d, T=T))                               # sets the view's bounding box (20 m)
    axes = o3d.geometry.TriangleMesh.create_coordinate_frame(size=0.5)   # marker: X red, Y green, Z blue
    if T is not None:
        axes.transform(T)
    vis.add_geometry(axes, reset_bounding_box=False)                     # add_geometry fits the view to the LAST geometry otherwise
    if args.scene:
        vis.add_geometry(load_scene(o3d, args.scene), reset_bounding_box=False)
    # Start looking at the marker from where the phone usually stands (+Z side), 3/4 view from above.
    # Open3D zoom: smaller is closer; 0.7 is the default for the bounding box.
    R = np.eye(3) if T is None else T[:3, :3] / max(1e-9, np.linalg.norm(T[:3, 0]))
    ctr = vis.get_view_control()
    ctr.set_lookat(np.zeros(3) if T is None else T[:3, 3])
    ctr.set_up(R @ np.array([0.0, 1.0, 0.0]))
    ctr.set_front(R @ np.array([0.35, 0.55, 0.75]))
    ctr.set_zoom(0.45)
    feed = Feed(args.server, hz=args.hz, include_static=args.all)
    feed.start()
    bodies = Bodies(o3d, vis, T)
    last_status, frame_period = 0.0, 1.0 / 60
    try:
        while vis.poll_events():
            t0 = time.monotonic()
            bodies.sync(feed.current(t0))
            vis.update_renderer()
            if t0 - last_status > 5.0:
                last_status = t0
                s = feed.status()
                if s['error']:
                    print(f"server {args.server}: {s['error']} (polls {s['polls']}, failures {s['failures']})", flush=True)
            time.sleep(max(0.0, frame_period - (time.monotonic() - t0)))
    finally:
        feed.stop()
        vis.destroy_window()


def run_print(args, T):
    feed = Feed(args.server, hz=args.hz, include_static=args.all)
    feed.start()
    try:
        while True:
            rows = feed.current()
            s = feed.status()
            if s['error'] and not rows:
                print(f"waiting for {args.server}: {s['error']}", flush=True)
            else:
                age = f"{s['ageS'] * 1000:.0f} ms" if s['ageS'] is not None else '-'
                line = f"n={len(rows)} poll age {age}"
                for row, pos in rows:
                    p = pos if T is None else T[:3, :3] @ pos + T[:3, 3]
                    hd = row.get('headingDeg')
                    line += (f" | {row['gid']} {row['name']} ({p[0]:.2f}, {p[1]:.2f}, {p[2]:.2f})"
                             f" {row['speed']:.1f} m/s" + (f" hdg {hd:.0f}" if hd is not None else '')
                             + ('' if row.get('moving', True) else ' static') + (' coasting' if row.get('coasting') else ''))
                print(line, flush=True)
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        feed.stop()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--server', default='http://127.0.0.1:8080', help='MinBand server base URL (laptop IP, port 8080)')
    ap.add_argument('--transform', help='4x4 text matrix: marker frame -> scene frame (align.py)')
    ap.add_argument('--scene', help='static reconstruction to show (.ply .pcd .xyz .obj .glb .stl)')
    ap.add_argument('--hz', type=float, default=20.0, help='poll rate (default 20)')
    ap.add_argument('--all', action='store_true', help='also show static entities (grey)')
    ap.add_argument('--print', action='store_true', help='no window: print the movers twice a second')
    args = ap.parse_args(argv)
    T = load_transform(args.transform) if args.transform else None
    if args.print:
        run_print(args, T)
    else:
        run_window(args, T)


if __name__ == '__main__':
    sys.exit(main())
