#!/usr/bin/env python
"""map_sender_slot.py - LIVE map sender (runs on "home" next to MASt3R-SLAM).

Polls the SLAM snapshot directory (snap_%04d.npz: pts float32[N,3], rgb uint8[N,3], traj float64[K,8], kf int),
always takes the NEWEST unprocessed snapshot, feeds the growing cloud to a whole-cloud LIVE streamer
(research/stream_best.py | stream_lod_conf.py | stream_sched.py, same make_encoder/update API) through a live adapter
(fixed ground frame + origin from the first snapshot with a safety margin, carried sent-sets / alpha / context state),
and transmits the chunk over TCP with <I len><payload> framing, rate-limited to --rate bytes/s:
    payload b'POSE' + traj[-1] float64[8] + float64 dt_slot        (before each chunk)
    payload = one record exactly as produced by the streamer (first record of the stream carries the stream header)
Record splitting depends on the streamer: stream_lod_conf / stream_sched chunks are split into their self-contained
band records (coarse band first); stream_best chunks are ONE record (single geometry + colour stream per chunk);
a streamer exposing split_records(chunk, first) is used as is.

Slot modes:  --slot 0 (default) one chunk per new snapshot, dt = time since the previous chunk started (first 6 s);
             --slot S  one chunk every S seconds on the newest snapshot (refinement continues without new keyframes),
                       budget = token bucket that gains rate*S per slot and loses the bytes actually queued (unused bytes
                       roll over, like live_eval.py --slot; capped at rate*--bucket seconds); if encoding takes longer than S the
                       next slot starts right after and the bucket is credited for the real elapsed time.
Logs every frame to --log (default <this dir>/sender_log.txt) and prints one line per chunk.
usage (on home):  cd ~/mapstream && python map_sender_slot.py [--snapshots ~/MASt3R-SLAM/live_out] [--connect 127.0.0.1:5555]
                  [--rate 1250] [--slot 0] [--streamer stream_best.py] [--timeout 90] [--margin 150,50,150]
"""
import os, sys, time, glob, struct, socket, argparse, threading, queue, importlib.util, traceback
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__)); RESEARCH = os.path.join(HERE, "research")
sys.path.insert(0, RESEARCH)
M21 = (1 << 21) - 1
def keyf(u): return (u[:, 0].astype(np.int64) << 42) | (u[:, 1].astype(np.int64) << 21) | u[:, 2].astype(np.int64)

def load_module(path):
    s = importlib.util.spec_from_file_location("streamer_mod", path); m = importlib.util.module_from_spec(s); __import__("sys").modules[s.name] = m; s.loader.exec_module(m); return m

# ----------------------------------------------------------------------------- live adapter
class LiveAdapter:
    """Grow the cloud of a whole-cloud streamer Encoder between updates.
    Kept across updates: enc.R, enc.o (fixed frame/origin), sent sets / per-band sent flags, alpha, context state.
    Replaced before each update: enc.P, enc.C/enc.Cf, enc.Q3 (= P @ R.T) and, for band-table encoders
    (stream_best / stream_sched: bands[bi] = dict(u, inv, n, sent)), the per-band voxel tables (sent flags carried by voxel key)."""
    def __init__(self, mod, rate, margin_m):
        import lod_common as L
        self.L, self.mod, self.rate = L, mod, rate
        self.margin = np.broadcast_to(np.asarray(margin_m, float), (3,)).copy()  # metres per ground-frame axis (x, up, z)
        self.enc = None; self.hdr_extra = b""; self.n_dropped = 0

    def _rebuild_bands(self, enc):
        old = getattr(enc, "bands", None); enc.bands = []
        for bi, v in enumerate(enc.vb):
            kk = keyf(np.floor((enc.Q3 - enc.o) / v).astype(np.int64)); uq, inv = np.unique(kk, return_inverse=True)
            u = np.stack([uq >> 42, (uq >> 21) & M21, uq & M21], 1); sent = np.zeros(len(uq), bool)
            if old is not None and old[bi]["sent"].any(): sent = np.isin(uq, keyf(old[bi]["u"][old[bi]["sent"]]))
            enc.bands.append(dict(u=u, inv=inv.ravel(), n=len(uq), sent=sent))
        B = enc.bands[0]; enc.cen = (B["u"] + 0.5) * enc.vb[0] + enc.o; enc.cenw = enc.cen @ enc.R  # stream_best zeff centres

    def _fix_origin(self, enc):
        enc.o = enc.Q3.min(0) - self.margin / self.L.M
        if hasattr(enc, "bands"): del enc.bands
        self._rebuild_bands(enc)

    def _inside(self, P, enc):
        Q3 = P @ enc.R.T; return (Q3 >= enc.o).all(1) & (((Q3 - enc.o) / enc.vb[0]) < M21).all(1)

    def set_cloud(self, P, C, traj):
        P = np.ascontiguousarray(P, np.float64); C = np.ascontiguousarray(C, np.uint8); L = self.L
        if self.enc is None:
            enc = self.enc = self.mod.make_encoder(self.rate, P, C)
            if not hasattr(enc, "vb"): enc.vb = self.mod.LEVELS / L.M
            if hasattr(enc, "_setup"):  # stream_best / stream_sched: let the streamer set up R, bands, header; then move the origin
                orig = enc._setup
                def hooked(traj, _orig=orig):
                    _orig(traj); self._fix_origin(enc)
                enc._setup = hooked
            else:  # stream_lod_conf: set the frame ourselves, the stream header is prepended to the first chunk by update()
                d = np.linalg.norm(P - traj[0, 1:4], axis=1); m0 = d < np.percentile(d, 50)
                enc.R = L.ground_frame(P[m0], traj); enc.Q3 = P @ enc.R.T; enc.o = enc.Q3.min(0) - self.margin / L.M
                self.hdr_extra = (enc.R.astype(np.float32).tobytes() + enc.o.astype(np.float32).tobytes()
                                  + struct.pack("<fB", float(self.mod.SPL), len(self.mod.LEVELS)) + np.asarray(self.mod.LEVELS, np.float32).tobytes())
            return
        enc = self.enc
        if enc.R is None:  # not set up yet (first update still pending)
            enc.P, enc.C, enc.Cf = P, C, C.astype(float)
            if hasattr(enc, "CfT"): enc.CfT = np.ascontiguousarray(enc.Cf.T)
            return
        ok = self._inside(P, enc); self.n_dropped = int((~ok).sum())
        if self.n_dropped: P, C = P[ok], C[ok]
        enc.P = P; enc.Q3 = P @ enc.R.T
        if hasattr(enc, "Cf"): enc.Cf = C.astype(float)
        if hasattr(enc, "CfT"): enc.CfT = np.ascontiguousarray(enc.Cf.T)   # stream_rt: transposed colour cache used by its kernels
        if hasattr(enc, "C") or not hasattr(enc, "Cf"): enc.C = C
        if hasattr(enc, "bands"): self._rebuild_bands(enc)

    def sent_count(self):
        enc = self.enc
        if hasattr(enc, "bands") and enc.bands: return int(sum(b["sent"].sum() for b in enc.bands))
        if hasattr(enc, "sent"): return int(sum(len(s) for s in enc.sent))
        return -1

    def update(self, k, traj, dt):
        hdr = self.hdr_extra; self.hdr_extra = b""
        if hdr: dt = max(dt - len(hdr) / self.rate, 0.1)  # keep the whole frame inside the slot budget
        return hdr + self.enc.update(k, traj, dt)

# ----------------------------------------------------------------------------- record splitting (per streamer)
def split_records(mod, name, chunk, first):
    if hasattr(mod, "split_records"): return list(mod.split_records(chunk, first))
    base = os.path.basename(name)
    rec_hdr = {"stream_lod_conf.py": 5, "stream_sched.py": 11}.get(base)  # <B band><I n>[<3H off>] + n bytes
    if rec_hdr is None: return [chunk]  # stream_best: one geometry + one colour stream per chunk -> not splittable
    p, hdr = 0, b""
    if first: nb = chunk[52]; p = 53 + 4 * nb; hdr = chunk[:p]
    recs = []
    while p < len(chunk):
        n = struct.unpack_from("<I", chunk, p + 1)[0]; recs.append((chunk[p], chunk[p:p + rec_hdr + n])); p += rec_hdr + n
    out = [r for _, r in sorted(recs, key=lambda x: -x[0])]  # coarse band first
    if first: out = [hdr + (out[0] if out else b"")] + out[1:]
    return out

# ----------------------------------------------------------------------------- paced TCP link
class Link(threading.Thread):
    """Sends <I len><payload> frames from a queue, paced to `rate` bytes/s (framing bytes included)."""
    def __init__(self, sock, rate, logf, t0):
        super().__init__(daemon=True); self.sock, self.rate, self.logf, self.t0 = sock, rate, logf, t0
        self.q = queue.Queue(); self.sent = 0; self.nframes = 0; self.err = None; self.start()
    def put(self, kind, payload): self.q.put((kind, payload))
    def run(self):
        piece = max(64, int(self.rate / 5)); t_next = time.monotonic()
        while True:
            item = self.q.get()
            if item is None: break
            kind, payload = item; data = struct.pack("<I", len(payload)) + payload
            try:
                for i in range(0, len(data), piece):
                    part = data[i:i + piece]; now = time.monotonic()
                    if t_next > now: time.sleep(t_next - now); now = t_next
                    self.sock.sendall(part); t_next = now + len(part) / self.rate
            except OSError as e:
                self.err = e; print(f"[link] send failed: {e}", flush=True); return
            self.sent += len(data); self.nframes += 1
            self.logf.write(f"{time.time():.3f} {time.time() - self.t0:8.2f} FRAME {kind} {len(data)} {self.sent}\n"); self.logf.flush()
    def finish(self, timeout):
        self.q.put(None); self.join(timeout)

def connect(hp, timeout):
    host, port = hp.rsplit(":", 1); t0 = time.time(); n = 0
    while True:
        try:
            s = socket.create_connection((host, int(port)), timeout=5); s.settimeout(30); return s
        except OSError as e:
            if time.time() - t0 > timeout: raise SystemExit(f"receiver {hp} not reachable after {timeout:.0f} s: {e}")
            if n % 10 == 0: print(f"[sender] waiting for receiver at {hp} ({e})", flush=True)
            n += 1; time.sleep(1)

def list_snaps(d):
    out = []
    for f in glob.glob(os.path.join(d, "snap_*.npz")):
        if ".tmp." in os.path.basename(f): continue
        try: out.append((os.path.getmtime(f), f))
        except OSError: pass
    return sorted(out)

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snapshots", default=os.path.expanduser("~/MASt3R-SLAM/live_out"))
    ap.add_argument("--connect", default="127.0.0.1:5555"); ap.add_argument("--rate", type=float, default=1250, help="bytes/s (1250 = 10 kbit/s)")
    ap.add_argument("--streamer", default=None, help="stream_best.py | stream_lod_conf.py | stream_sched.py (in research/) or a path")
    ap.add_argument("--timeout", type=float, default=90, help="seconds without a new snapshot before exiting")
    ap.add_argument("--connect-timeout", type=float, default=300); ap.add_argument("--slot", type=float, default=0, help="fixed slot length in s (0 = one chunk per snapshot)")
    ap.add_argument("--first-dt", type=float, default=6.0); ap.add_argument("--max-dt", type=float, default=20.0, help="cap on dt in snapshot mode (s)")
    ap.add_argument("--bucket", type=float, default=0.0, help="slot mode: token bucket cap in seconds of --rate (default 0 = 3*slot; bounds the receiver blackout after a slow encode)")
    ap.add_argument("--margin", default="150,50,150", help="grid margin in metres below the first cloud (x,up,z or one value); 500 = spec, costly (dense octree grids)")
    ap.add_argument("--no-append", action="store_true", help="old behaviour: replace the whole cloud per snapshot (re-sends refined areas)"); ap.add_argument("--log", default=os.path.join(HERE, "sender_log.txt")); ap.add_argument("--max-chunks", type=int, default=0)
    a = ap.parse_args()
    if a.streamer is None: a.streamer = "stream_best.py" if os.path.exists(os.path.join(RESEARCH, "stream_best.py")) else "stream_lod_conf.py"
    spath = a.streamer if os.path.exists(a.streamer) else os.path.join(RESEARCH, a.streamer)
    mod = load_module(spath); margin = [float(x) for x in a.margin.split(",")]
    ad = LiveAdapter(mod, a.rate, margin if len(margin) == 3 else margin[0])
    t0 = time.time(); logf = open(a.log, "a"); logf.write(f"# {time.strftime('%F %T')} sender start streamer={os.path.basename(spath)} rate={a.rate} slot={a.slot} snapshots={a.snapshots}\n")
    sock = connect(a.connect, a.connect_timeout); link = Link(sock, a.rate, logf, t0)
    print(f"[sender] connected to {a.connect}; streamer {os.path.basename(spath)}, {a.rate:.0f} B/s, slot {a.slot or 'per snapshot'}", flush=True)
    k = 0; done = set(); t_prev = None; bucket = 0.0; t_last_snap = time.time(); traj = None; snap_name = None; npts = 0; acc_pts, acc_rgb, acc_kf = [], [], 0
    try:
        while True:
            now = time.time()
            if link.err: print("[sender] link closed by receiver"); break
            new = [(m, f) for m, f in list_snaps(a.snapshots) if f not in done]
            if new:
                for _, f in new: done.add(f)
                f = new[-1][1]
                try:
                    z = np.load(f); pts, rgb, tr = z["pts"], z["rgb"], np.atleast_2d(z["traj"]).astype(np.float64); kf = int(z["kf"]) if "kf" in z else len(tr) - 1
                    tl = time.time(); K = len(tr)
                    if "kf_off" in z and not a.no_append:  # APPEND-ONLY ingestion: each keyframe's points are taken once (from the first
                        off = z["kf_off"]                   # snapshot in which >= 2 keyframes exist, so its Sim3 scale is settled) and never re-sent
                        if K < 2: print(f"[sender] {os.path.basename(f)}: only {K} keyframe(s), waiting for the second one", flush=True); t_last_snap = time.time(); continue
                        if K > acc_kf:
                            sl = slice(int(off[acc_kf]), int(off[K])); acc_pts.append(pts[sl]); acc_rgb.append(rgb[sl]); acc_kf = K
                            pts, rgb = np.concatenate(acc_pts), np.concatenate(acc_rgb); ad.set_cloud(pts, rgb, tr)
                        else: print(f"[sender] {os.path.basename(f)}: no new keyframe, ignored (refinements are not re-sent)", flush=True)
                    else: ad.set_cloud(pts, rgb, tr)
                    traj = tr; snap_name = os.path.basename(f); npts = len(pts); t_last_snap = time.time()
                    print(f"[sender] loaded {snap_name} kf={kf} pts={npts:,} traj={len(tr)} (skipped {len(new) - 1} stale) adapter {time.time() - tl:.1f}s" + (f" dropped {ad.n_dropped} outside grid" if ad.n_dropped else ""), flush=True)
                    logf.write(f"{time.time():.3f} {time.time() - t0:8.2f} SNAP {snap_name} kf={kf} pts={npts} skipped={len(new) - 1}\n")
                except Exception as e:  # unreadable (should not happen: writes are atomic) -> retry on the next poll
                    print(f"[sender] failed to load {f}: {e}", flush=True); done.discard(f); time.sleep(0.5); continue
            if traj is None:
                if now - t_last_snap > a.timeout: print("[sender] no snapshot arrived, exiting"); break
                time.sleep(0.2); continue
            if a.slot > 0:
                if t_prev is not None and now < t_prev + a.slot: time.sleep(min(t_prev + a.slot - now, 0.2)); continue
                if now - t_last_snap > a.timeout: print(f"[sender] no new snapshot for {a.timeout:.0f} s, exiting"); break
                bucket = min(bucket + a.rate * (a.slot if t_prev is None else now - t_prev), a.rate * (a.bucket or 3 * a.slot)); dt = bucket / a.rate  # token bucket: unused bytes roll over
            else:
                if not new:
                    if now - t_last_snap > a.timeout: print(f"[sender] no new snapshot for {a.timeout:.0f} s, exiting"); break
                    time.sleep(0.2); continue
                dt = a.first_dt if t_prev is None else min(now - t_prev, a.max_dt)
            t_start = time.time(); before = ad.sent_count()
            try: chunk = ad.update(k, traj.copy(), dt)
            except Exception:
                traceback.print_exc(); logf.write(f"{time.time():.3f} ENCODE-ERROR slot {k}\n"); t_prev = t_start; k += 1; continue
            enc_s = time.time() - t_start; first = k == 0
            recs = split_records(mod, spath, chunk, first) if (first or ad.sent_count() != before) else []
            if recs:
                pose = b"POSE" + np.ascontiguousarray(traj[-1], np.float64).tobytes() + struct.pack("<d", dt); link.put("POSE", pose)
                for r in recs: link.put("REC", r)
                if a.slot > 0: bucket -= (len(pose) + 4) + sum(len(r) + 4 for r in recs)
            msg = (f"chunk {k}: {snap_name} kf={len(traj) - 1} pts={npts:,} dt={dt:.2f}s budget={a.rate * dt:.0f}B bytes={len(chunk)} "
                   f"({100 * len(chunk) / (a.rate * dt):.0f}%) records={len(recs)} enc={enc_s:.2f}s alpha={getattr(ad.enc, 'alpha', float('nan')):.4f} queued={link.q.qsize()}"
                   + (f" bucket={bucket:.0f}B" if a.slot > 0 else "") + ("" if recs else " [empty, skipped]"))
            print(f"[sender] {msg}", flush=True); logf.write(f"{time.time():.3f} {time.time() - t0:8.2f} CHUNK {msg}\n"); logf.flush()
            t_prev = t_start; k += 1
            if a.max_chunks and k >= a.max_chunks: print("[sender] max chunks reached"); break
    except KeyboardInterrupt: print("[sender] interrupted")
    pending = sum(len(p) + 4 for _, p in list(link.q.queue)) if not link.err else 0
    print(f"[sender] draining {pending} queued bytes (~{pending / a.rate:.0f} s)", flush=True)
    link.finish(timeout=pending / a.rate + 15)
    try: sock.shutdown(socket.SHUT_RDWR)
    except OSError: pass
    sock.close(); logf.write(f"# end: {k} chunks, {link.nframes} frames, {link.sent} bytes on the wire in {time.time() - t0:.1f} s\n"); logf.close()
    print(f"[sender] done: {k} chunks, {link.nframes} frames, {link.sent} B on the wire, {time.time() - t0:.1f} s", flush=True)

if __name__ == "__main__": main()
