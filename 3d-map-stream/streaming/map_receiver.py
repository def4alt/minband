#!/usr/bin/env python
"""Operator-side receiver / viewer for the LIVE 10 kbit/s map stream (protocol items 2-3).

Wire: TCP, one sender, frames = <I len (little-endian)><payload>.
  payload b'POSE' + float64[8] traj row (t x y z qx qy qz qw) + float64 dt_slot   -> newest keyframe pose
  anything else = one band record of the streamer (the very first record carries the stream header) -> Decoder.apply
After every applied record: dec.map(), render 640x360 from the latest POSE (numpy splat renderer of demo_live_render.py),
write recv_frames/frame_%05d.png (+ latest.png) with an overlay, append to recv_log.txt.
On socket close / idle timeout: recv_final.ply (map), recv_final.txt (all POSE rows, TUM format), exit.

usage: python map_receiver.py [--listen 0.0.0.0:5555] [--out DIR] [--streamer stream_x.py] [--gui]
       (--gui = Open3D window; run with `env -u WAYLAND_DISPLAY`.  Nothing else depends on it.)
"""
import os, sys, time, socket, struct, argparse, importlib.util, traceback, numpy as np
from scipy.spatial.transform import Rotation
from PIL import Image, ImageDraw, ImageFont
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
W, H, HFOV = 640, 360, 70.0; FX = W / 2 / np.tan(np.radians(HFOV) / 2)
SKY = np.array([184, 209, 237], np.uint8)
POSE_LEN = 4 + 8 * 8 + 8
STAT_LEN = 27  # b"STAT" + <IHIIIfB> (23 B)


def default_streamer():
    for n in ("stream_best.py", "stream_lod_conf.py"):
        p = os.path.join(HERE, n)
        if os.path.exists(p): return p
    raise SystemExit("no streamer found in " + HERE)


def load_streamer(path):
    s = importlib.util.spec_from_file_location("streamer", path); m = importlib.util.module_from_spec(s); s.loader.exec_module(m); return m


def render(pts, cols, R, c, splat):  # numpy splat renderer (demo_live_render.py); robust to an empty map
    img = np.empty((H, W, 3), np.uint8); img[:] = SKY
    if pts is None or len(pts) == 0: return img
    q = (pts - c) @ R; k = q[:, 2] > 0.05; q, col = q[k], cols[k]
    if len(q) == 0: return img
    u = (FX * q[:, 0] / q[:, 2] + W / 2).astype(np.int32); v = (FX * q[:, 1] / q[:, 2] + H / 2).astype(np.int32)
    k = (u >= 0) & (u < W - 1) & (v >= 0) & (v < H - 1); u, v, z, col = u[k], v[k], q[k, 2], col[k]
    if len(z) == 0: return img
    o = np.argsort(-z); u, v, z, col = u[o], v[o], z[o], col[o]
    size = np.clip(np.ceil(FX * splat / z), 2, 8).astype(np.int32) if splat and splat > 0 else np.full(len(z), 2, np.int32)
    smax = int(size.max()); sel = [None] + [np.nonzero(size > m)[0] for m in range(1, smax)]  # points needing each splat size (once)
    for du in range(smax):
        for dv in range(smax):
            m = max(du, dv)
            if m == 0: img[v, u] = col; continue
            kk = sel[m]; img[np.minimum(v[kk] + dv, H - 1), np.minimum(u[kk] + du, W - 1)] = col[kk]
    return img


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


class Gui:  # optional Open3D non-blocking window
    def __init__(self):
        import open3d as o3d; self.o3d = o3d; self.vis = o3d.visualization.Visualizer()
        self.vis.create_window("map_receiver (live)", 960, 540); self.pcd = o3d.geometry.PointCloud(); self.added = False
    def update(self, pts, cols):
        self.pcd.points = self.o3d.utility.Vector3dVector(np.asarray(pts, float)); self.pcd.colors = self.o3d.utility.Vector3dVector(np.asarray(cols, float) / 255.0)
        if not self.added: self.vis.add_geometry(self.pcd); self.added = True
        else: self.vis.update_geometry(self.pcd)
        self.poll()
    def poll(self):
        try: self.vis.poll_events(); self.vis.update_renderer()
        except Exception: pass
    def close(self):
        try: self.vis.destroy_window()
        except Exception: pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--listen", default="0.0.0.0:5555"); ap.add_argument("--out", default="/home/matteo/Documents/3d/slam_results/00_LIVE_E2E/run")
    ap.add_argument("--streamer", default=None, help="streamer module (only make_decoder is used); default stream_best.py else stream_lod_conf.py")
    ap.add_argument("--gui", action="store_true"); ap.add_argument("--accept-timeout", type=float, default=600.0, help="s to wait for the sender")
    ap.add_argument("--idle-timeout", type=float, default=120.0, help="s without data before finishing")
    ap.add_argument("--rate-kbit", type=float, default=10.0, help="only for the overlay label")
    a = ap.parse_args()
    streamer = a.streamer or default_streamer(); m = load_streamer(streamer); dec = m.make_decoder()
    os.makedirs(os.path.join(a.out, "recv_frames"), exist_ok=True)
    log = open(os.path.join(a.out, "recv_log.txt"), "a"); log.write(f"# map_receiver {time.strftime('%Y-%m-%d %H:%M:%S')} streamer={os.path.basename(streamer)}\n# t_rel_s t_abs_s kind bytes band cum_kB pts\n"); log.flush()
    try: font = ImageFont.load_default(size=15)
    except TypeError: font = ImageFont.load_default()
    gui = Gui() if a.gui else None
    host, port = a.listen.rsplit(":", 1); srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, int(port))); srv.listen(1); srv.settimeout(0.5); print(f"[recv] listening on {a.listen}, out={a.out}, streamer={os.path.basename(streamer)}", flush=True)
    conn, t_start = None, time.time()
    while conn is None:
        try: conn, addr = srv.accept(); print(f"[recv] sender connected from {addr}", flush=True); conn.sendall(b"MAPR")  # greeting: proves a receiver is behind the tunnel
        except socket.timeout:
            if gui: gui.poll()
            if time.time() - t_start > a.accept_timeout: print("[recv] no sender, giving up", flush=True); srv.close(); return 1
    srv.close()
    rd = FrameReader(conn, a.idle_timeout, tick=gui.poll if gui else None, first_timeout=a.accept_timeout)
    poses, pose, dt_slot, nrec, cum, t0, nframe = [], None, None, 0, 0, None, 0
    pts, cols, splat = np.zeros((0, 3)), np.zeros((0, 3), np.uint8), 0.0
    reason = "socket closed"
    try:
        while True:
            try: pay = rd.read()
            except (TimeoutError, ValueError) as e: reason = str(e); break
            if pay is None: break
            now = time.time(); t0 = t0 or now; cum += len(pay) + 4; trel = now - t0
            if len(pay) == STAT_LEN and pay[:4] == b"STAT":  # sender status (not a map record)
                log.write(f"{trel:.3f} {now:.3f} STAT {len(pay)+4} - {cum/1e3:.3f} {len(pts)}\n"); log.flush(); continue
            if len(pay) == POSE_LEN and pay[:4] == b"POSE":
                row = np.frombuffer(pay[4:68], np.float64).copy(); dt_slot = struct.unpack("<d", pay[68:76])[0]; poses.append(row); pose = row
                log.write(f"{trel:.3f} {now:.3f} POSE {len(pay)+4} - {cum/1e3:.3f} {len(pts)}\n"); log.flush()
                print(f"[recv] {trel:6.1f}s POSE kf t={row[0]:.2f} dt={dt_slot:.2f}", flush=True); continue
            first = nrec == 0  # label only: the parser never assumes a record layout
            band = "hdr" if first else (f"b{pay[0]}" if len(pay) >= 5 and struct.unpack("<I", pay[1:5])[0] == len(pay) - 5 else "chunk")
            t1 = time.time()
            try: dec.apply(pay); applied = True
            except Exception:
                applied = False; print(f"[recv] apply FAILED on record {nrec} ({len(pay)} B):", flush=True); traceback.print_exc()
            if applied:
                nrec += 1
                try: p2, c2, sp = dec.map(); pts, cols, splat = np.asarray(p2, float), np.asarray(c2, np.uint8), float(sp)
                except Exception: print("[recv] map() failed (empty?)", flush=True); traceback.print_exc()
            tdec = time.time() - t1
            # ---- render from the latest POSE (sky + overlay while no POSE has arrived yet)
            img = render(pts, cols, *pose_RC(pose), splat) if pose is not None else np.tile(SKY, (H, W, 1))
            im = Image.fromarray(np.ascontiguousarray(img)); d = ImageDraw.Draw(im); d.rectangle([0, 0, W, 22], fill=(0, 0, 0))
            d.text((6, 4), f"LIVE {a.rate_kbit:g} kbit/s  rec {nrec}  {cum/1e3:.1f} KB  t={trel:.1f}s  {len(pts):,} pts  kf {len(poses)-1 if poses else '-'}{'' if pose is not None else '  (waiting for POSE)'}", fill=(255, 255, 255), font=font)
            im.save(os.path.join(a.out, "recv_frames", f"frame_{nframe:05d}.png")); nframe += 1
            tmp = os.path.join(a.out, "latest.tmp.png"); im.save(tmp); os.replace(tmp, os.path.join(a.out, "latest.png"))
            log.write(f"{trel:.3f} {now:.3f} REC {len(pay)+4} {band} {cum/1e3:.3f} {len(pts)}\n"); log.flush()
            print(f"[recv] {trel:6.1f}s record {nrec} band {band} {len(pay)} B  cum {cum/1e3:.1f} KB  pts {len(pts):,}  dec+map {tdec:.2f}s  frame {nframe-1}", flush=True)
            if gui and len(pts): gui.update(pts, cols)
    except KeyboardInterrupt: reason = "interrupted"
    finally:
        try: conn.close()
        except Exception: pass
        write_ply(os.path.join(a.out, "recv_final.ply"), pts, cols)
        np.savetxt(os.path.join(a.out, "recv_final.txt"), np.array(poses).reshape(-1, 8), fmt="%.6f")
        with open(os.path.join(a.out, "recv_final.splat"), "w") as f: f.write(f"{splat:.6g}\n")
        log.write(f"# end: {reason}, {nrec} records, {cum/1e3:.1f} KB, {len(pts)} pts, {nframe} frames\n"); log.close()
        print(f"[recv] done ({reason}): {nrec} records, {cum/1e3:.1f} KB, {len(pts):,} pts, {nframe} frames, {len(poses)} poses -> {a.out}", flush=True)
        if gui: gui.close()
    return 0


if __name__ == "__main__": sys.exit(main())
