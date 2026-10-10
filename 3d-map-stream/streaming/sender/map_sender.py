"""map_sender.py (runs on home next to MASt3R-SLAM): watch the SLAM snapshot dir, encode each new keyframe's map with
the live streamer and send it to the operator over a 10 kbit/s TCP link.

wire: <I len little-endian><payload>; payload = b'POSE' + float64[8] newest keyframe row + float64 dt_slot, then the
chunk's band records coarse-first (first record of the stream carries the stream header).  Rate limit = --rate B/s.
usage: python map_sender.py [--snapshots DIR] [--connect HOST:PORT] [--rate 1250] [--streamer stream_best.py] [--timeout 90]
"""
import os, sys, time, glob, re, socket, struct, argparse, threading, queue, traceback, numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import live_adapter as LA

def log_line(f, msg):
    line = f"{time.time():.3f} {msg}"; print(line, flush=True); f.write(line + "\n"); f.flush()

class Link(threading.Thread):
    """paced sender: frames go out in order at <= rate B/s (125 B slices), pending bytes are tracked for the scheduler."""
    def __init__(self, sock, rate, logf):
        super().__init__(daemon=True); self.sock, self.rate, self.logf = sock, rate, logf
        self.q = queue.Queue(); self.pending = 0; self.sent = 0; self.lock = threading.Lock(); self.err = None; self.t_next = time.time()
    def put(self, kind, payload):
        with self.lock: self.pending += 4 + len(payload)
        self.q.put((kind, payload))
    def remaining_s(self):
        with self.lock: return self.pending / self.rate
    def run(self):
        while True:
            item = self.q.get()
            if item is None: return
            kind, payload = item; data = struct.pack("<I", len(payload)) + payload; t0 = time.time()
            try:
                for a in range(0, len(data), 125):
                    now = time.time(); self.t_next = max(self.t_next, now - 0.1)   # bucket: at most 0.1 s (125 B) of burst credit
                    if self.t_next > now: time.sleep(self.t_next - now)
                    s = data[a:a + 125]; self.sock.sendall(s); self.t_next += len(s) / self.rate
                    with self.lock: self.pending -= len(s); self.sent += len(s)
            except Exception as e:
                self.err = e; log_line(self.logf, f"SEND-ERROR {e}"); return
            log_line(self.logf, f"frame {kind} bytes {len(data)} cum {self.sent} tx_s {time.time()-t0:.2f}")

def connect(host, port, logf, deadline):
    while True:
        try:
            s = socket.create_connection((host, port), timeout=10); s.settimeout(60); s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            log_line(logf, f"connected {host}:{port}"); return s
        except OSError as e:
            if time.time() > deadline: log_line(logf, f"giving up connecting: {e}"); sys.exit(2)
            time.sleep(1.0)

def newest_snapshot(d, done):
    best = None
    for p in glob.glob(os.path.join(d, "snap_*.npz")):
        m = re.fullmatch(r"snap_(\d+)\.npz", os.path.basename(p))
        if m and int(m.group(1)) > done and (best is None or int(m.group(1)) > best[0]): best = (int(m.group(1)), p)
    return best

def load_snapshot(path):
    for _ in range(5):
        try:
            with np.load(path) as z: return dict(pts=z["pts"], rgb=z["rgb"], traj=np.atleast_2d(z["traj"]), kf=int(z["kf"]))
        except Exception: time.sleep(0.2)
    return None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshots", default=os.path.expanduser("~/MASt3R-SLAM/live_out")); ap.add_argument("--connect", default="127.0.0.1:5555")
    ap.add_argument("--rate", type=float, default=1250.0, help="bytes/s"); ap.add_argument("--streamer", default=None, help="stream_best.py | stream_lod_conf.py")
    ap.add_argument("--timeout", type=float, default=90.0, help="s without a new snapshot before exiting")
    ap.add_argument("--connect-timeout", type=float, default=600.0); ap.add_argument("--margin-m", type=float, default=100.0, help="origin margin (m) around the first snapshot (500 m makes stream_best 30x slower: dense octree grids)")
    ap.add_argument("--first-dt", type=float, default=6.0); ap.add_argument("--log", default=os.path.join(HERE, "sender_log.txt")); a = ap.parse_args()
    rdir = os.path.join(HERE, "research"); name = a.streamer or ("stream_best.py" if os.path.exists(os.path.join(rdir, "stream_best.py")) else "stream_lod_conf.py")
    spath = name if os.path.isabs(name) else os.path.join(rdir, os.path.basename(name)); mod = LA.load_streamer(spath)
    logf = open(a.log, "a"); log_line(logf, f"start streamer={os.path.basename(spath)} rate={a.rate} snapshots={a.snapshots} margin_m={a.margin_m}")
    host, port = a.connect.rsplit(":", 1); sock = connect(host, int(port), logf, time.time() + a.connect_timeout)
    link = Link(sock, a.rate, logf); link.start()
    le = None; done = -1; k = 0; prev_start = None; last_new = time.time(); est_enc = 3.0; tot = 0
    while link.err is None:
        snap = newest_snapshot(a.snapshots, done)
        if snap is None:
            if time.time() - last_new > a.timeout: log_line(logf, f"no new snapshot for {a.timeout:.0f} s -> done"); break
            time.sleep(0.5); continue
        last_new = time.time()
        if link.remaining_s() > est_enc + 0.5: time.sleep(0.25); continue   # link still busy: wait, then re-poll for an even newer snapshot
        idx, path = snap; S = load_snapshot(path)
        if S is None: log_line(logf, f"cannot read {path}, skipping"); done = idx; continue
        skipped = [i for i in range(done + 1, idx)]; done = idx
        if len(S["pts"]) < 1000 or S["traj"].shape[1] != 8: log_line(logf, f"skipping {os.path.basename(path)}: {len(S['pts'])} pts, traj {S['traj'].shape}"); continue
        now = time.time(); dt = a.first_dt if prev_start is None else now - prev_start; prev_start = now
        try:
            P = S["pts"].astype(np.float64); C = S["rgb"].astype(np.uint8); traj = S["traj"].astype(np.float64)
            if le is None: le = LA.LiveEncoder(mod, a.rate, P, C, a.margin_m / mod.L.M)
            le.set_cloud(P, C)
            t1 = time.time(); chunk = le.update(k, traj, dt); tenc = time.time() - t1; est_enc = 0.5 * est_enc + 0.5 * tenc
            recs = le.split_records(chunk, k == 0)
        except Exception:
            log_line(logf, f"ENCODE-ERROR on {os.path.basename(path)}:\n" + traceback.format_exc()); continue
        link.put("POSE", LA.pose_payload(traj[-1], dt))
        for r in recs: link.put(f"REC{k}", r)
        tot += len(chunk)
        log_line(logf, f"chunk {k} snap {idx} kf {S['kf']} pts {le.npts} dropped {le.dropped} dt {dt:.2f} budget {a.rate*dt:.0f} bytes {len(chunk)} "
                       f"records {len(recs)} enc_s {tenc:.2f} tot {tot}" + (f" skipped {skipped}" if skipped else ""))
        k += 1
    while link.err is None and link.remaining_s() > 0: time.sleep(0.2)   # drain
    link.q.put(None); link.join(timeout=5)
    try: sock.shutdown(socket.SHUT_RDWR)
    except OSError: pass
    sock.close(); log_line(logf, f"exit chunks {k} bytes {tot}")

if __name__ == "__main__": main()
