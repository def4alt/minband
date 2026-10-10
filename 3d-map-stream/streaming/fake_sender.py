#!/usr/bin/env python
"""Fake sender for testing map_receiver.py without SLAM: runs the streamer's encoder on a finished map
(houses_7fps.ply + .txt), one chunk per keyframe, splits each chunk into its band records (coarse first) and
sends POSE + records with the wire framing <I len><payload> at --rate B/s (token bucket, 10 kbit/s default).

usage: python fake_sender.py [--host 127.0.0.1 --port 5555] [--rate 1250] [--streamer stream_x.py]
                             [--ply ../maps/source/houses_7fps.ply --traj ../maps/source/houses_7fps.txt]
                             [--split auto|band|chunk] [--chunks N] [--no-wait]
--split auto: per-band records when the chunk tiles exactly into <B band><I n> records (stream_lod_conf family),
              otherwise the whole chunk is one record (stream_best / stream_ctx: one range-coder stream per chunk).
--no-wait:    encode all chunks first, then send back to back (default: encode slot k right before sending it,
              like the live sender, so the pacing includes encode time)."""
import os, sys, time, socket, struct, argparse, importlib.util, numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
SRC = os.path.join(os.path.dirname(HERE), "maps", "source")


def load_streamer(path):
    s = importlib.util.spec_from_file_location("streamer", path); m = importlib.util.module_from_spec(s); s.loader.exec_module(m); return m


def default_streamer():
    for n in ("stream_best.py", "stream_lod_conf.py"):
        if os.path.exists(os.path.join(HERE, n)): return os.path.join(HERE, n)
    raise SystemExit("no streamer")


def load_ply(path):
    import open3d as o3d
    p = o3d.io.read_point_cloud(path); return np.asarray(p.points), (np.asarray(p.colors) * 255).round().astype(np.uint8)


def split_records(b, first, mode="auto"):
    """-> list of wire payloads. Header (first chunk) rides on the first record; coarse band first."""
    p = 0; hdr = b""
    if first:
        nb = b[52]; p = 53 + 4 * nb; hdr = b[:p]
    if mode == "chunk": return [b]
    recs, q = [], p
    while q + 5 <= len(b):
        band, n = struct.unpack("<BI", b[q:q + 5])
        if first and band >= nb: break
        if q + 5 + n > len(b): break
        recs.append((band, b[q:q + 5 + n])); q += 5 + n
    ok = q == len(b) and len(recs) > 0
    if not ok:
        if mode == "band": raise ValueError("chunk is not a sequence of band records")
        return [b]
    recs = [r for _, r in sorted(recs, key=lambda x: -x[0])]  # coarse band first
    recs[0] = hdr + recs[0]; return recs


class Pacer:  # token-bucket style: a frame of n bytes may finish no earlier than t_allow
    def __init__(self, rate): self.rate, self.t = rate, time.time()
    def wait(self, n):
        self.t = max(self.t, time.time()) + n / self.rate; dt = self.t - time.time()
        if dt > 0: time.sleep(dt)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1"); ap.add_argument("--port", type=int, default=5555); ap.add_argument("--rate", type=float, default=1250.0, help="bytes/s")
    ap.add_argument("--streamer", default=None); ap.add_argument("--ply", default=os.path.join(SRC, "houses_7fps.ply")); ap.add_argument("--traj", default=os.path.join(SRC, "houses_7fps.txt"))
    ap.add_argument("--split", default="auto", choices=["auto", "band", "chunk"]); ap.add_argument("--chunks", type=int, default=0, help="limit number of chunks (0 = all keyframes)")
    ap.add_argument("--no-wait", action="store_true"); ap.add_argument("--connect-timeout", type=float, default=60.0)
    ap.add_argument("--piece", type=int, default=256, help="bytes per paced write")
    a = ap.parse_args()
    streamer = a.streamer or default_streamer(); m = load_streamer(streamer)
    P, C = load_ply(a.ply); traj = np.loadtxt(a.traj).reshape(-1, 8); ts = traj[:, 0]; K = len(traj) if a.chunks <= 0 else min(a.chunks, len(traj))
    print(f"[send] {len(P):,} pts, {K} keyframes, streamer {os.path.basename(streamer)}, rate {a.rate:g} B/s", flush=True)
    enc = m.make_encoder(a.rate, P.copy(), C.copy())
    def slot(k):
        dt = (ts[k + 1] - ts[k]) if k + 1 < len(ts) else float(np.median(np.diff(ts))) if len(ts) > 1 else 6.0
        t1 = time.time(); chunk = enc.update(k, traj[: k + 1].copy(), dt); tenc = time.time() - t1
        recs = split_records(chunk, k == 0, a.split)
        print(f"[send] slot {k}: {len(chunk)} B / budget {a.rate*dt:.0f} B, enc {tenc:.1f}s, {len(recs)} record(s) {[len(r) for r in recs]}", flush=True)
        return dt, recs
    pre = [slot(k) for k in range(K)] if a.no_wait else None
    # ---- connect (retry until the receiver listens)
    t0 = time.time(); s = None
    while s is None:
        try: s = socket.create_connection((a.host, a.port), timeout=5)
        except OSError as e:
            if time.time() - t0 > a.connect_timeout: raise SystemExit(f"[send] cannot connect to {a.host}:{a.port}: {e}")
            time.sleep(0.5)
    s.settimeout(30); pacer = Pacer(a.rate); total = 0; tstart = time.time()
    def send(payload):  # paced in --piece byte slices so the bytes trickle like a serial link (not one burst + sleep)
        nonlocal total; f = struct.pack("<I", len(payload)) + payload
        for i in range(0, len(f), a.piece):
            s.sendall(f[i:i + a.piece]); total += len(f[i:i + a.piece]); pacer.wait(len(f[i:i + a.piece]))
    try:
        for k in range(K):
            dt, recs = pre[k] if pre else slot(k)
            send(b"POSE" + traj[k].astype(np.float64).tobytes() + struct.pack("<d", float(dt)))
            for i, r in enumerate(recs):
                send(r); print(f"[send] {time.time()-tstart:6.1f}s slot {k} record {i}: {len(r)} B  total {total/1e3:.1f} KB", flush=True)
    finally:
        s.close(); el = time.time() - tstart
        print(f"[send] done: {total} B in {el:.1f} s = {8*total/el/1e3:.2f} kbit/s", flush=True)


if __name__ == "__main__": main()
