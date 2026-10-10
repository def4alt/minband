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
Logs every frame to --log (default <this dir>/sender_log.txt, truncated per run) and prints one line per chunk.
The receiver greets with b'MAPR' right after accept (a tunnel accepts connections even without a listener).
usage (on home):  cd ~/mapstream && python map_sender_slot.py [--snapshots ~/MASt3R-SLAM/live_out] [--connect 127.0.0.1:5555]
                  [--rate 1250] [--slot 0] [--streamer stream_best.py] [--timeout 90] [--margin 150,50,150]
"""
import os, sys, time, glob, struct, socket, argparse, threading, queue, importlib.util, traceback
import numpy as np
try: np.core.multiarray._set_madvise_hugepage(False)  # numpy madvise(HUGEPAGE) + defrag=madvise -> 0.2-1 s compaction stalls on home
except Exception: pass

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
        self.enc = None; self.hdr_extra = b""; self.n_dropped = 0; self.hdr_done = False  # hdr_done: the first record (stream header) was queued

    def _rebuild_bands(self, enc):
        old = getattr(enc, "bands", None); enc.bands = []; self._uq = {}
        for bi, v in enumerate(enc.vb):
            kk = keyf(np.floor((enc.Q3 - enc.o) / v).astype(np.int64)); uq, inv = np.unique(kk, return_inverse=True)
            u = np.stack([uq >> 42, (uq >> 21) & M21, uq & M21], 1); sent = np.zeros(len(uq), bool)
            if old is not None and old[bi]["sent"].any(): sent = np.isin(uq, keyf(old[bi]["u"][old[bi]["sent"]]))
            enc.bands.append(dict(u=u, inv=inv.ravel(), n=len(uq), sent=sent)); self._uq[bi] = uq
        B = enc.bands[0]; enc.cen = (B["u"] + 0.5) * enc.vb[0] + enc.o; enc.cenw = enc.cen @ enc.R  # stream_best zeff centres

    def _extend_bands(self, enc, Qn):
        """Append-only fast path of _rebuild_bands: voxelise only the new points Qn (ground frame) and merge them into the
        per-band sorted voxel tables.  Produces exactly the arrays _rebuild_bands would (same sorted unique keys, inv,
        carried sent flags), in O(new points) + O(voxels) instead of re-sorting the whole cloud."""
        for bi, v in enumerate(enc.vb):
            B = enc.bands[bi]; kn = keyf(np.floor((Qn - enc.o) / v).astype(np.int64)); uq_old = self._uq[bi]
            un = np.unique(kn); merged = np.union1d(uq_old, un)
            pos_old = np.searchsorted(merged, uq_old); inv = np.concatenate([pos_old[B["inv"]], np.searchsorted(merged, kn)])
            sent = np.zeros(len(merged), bool); sent[pos_old] = B["sent"]
            u = np.stack([merged >> 42, (merged >> 21) & M21, merged & M21], 1)
            enc.bands[bi] = dict(u=u, inv=inv, n=len(merged), sent=sent); self._uq[bi] = merged
        B = enc.bands[0]; enc.cen = (B["u"] + 0.5) * enc.vb[0] + enc.o; enc.cenw = enc.cen @ enc.R

    def _fix_origin(self, enc):
        enc.o = enc.Q3.min(0) - self.margin / self.L.M
        if hasattr(enc, "bands"): del enc.bands
        self._rebuild_bands(enc); self._pregrow(enc); self._n_kept = len(enc.Q3)

    def _pregrow(self, enc):
        """stream_rt: grow the encoder's (growable) occupancy levels to the current cloud's band extents now, so the
        reallocate+copy happens at ingestion and never inside a slot's encode (it cost seconds at the finest band).
        Contents and coordinates are unchanged (growth only extends the high end); the kernels mask by shape."""
        st = getattr(enc, "st", None)
        if st is None or getattr(st, "dims", None) is not None or not hasattr(st, "occ"): return
        for bj, B in enumerate(enc.bands):
            if bj >= len(st.occ) or not len(B["u"]): continue
            top = B["u"].max(0).astype(np.int64)
            for l, G in list(st.occ[bj].items()):
                need = (top >> l) + 1; d = np.array(G.shape, np.int64)
                if (need > d).any():
                    nd = np.maximum(d, need + (need >> 2) + 4); G2 = np.zeros(tuple(int(x) for x in nd), np.bool_)
                    G2[:d[0], :d[1], :d[2]] = G; st.occ[bj][l] = G2

    def _inside(self, P, enc):  # fixed grid: ids >= 0 and record-box origins must fit the uint16 wire header
        Q3 = P @ enc.R.T; return (Q3 >= enc.o).all(1) & (((Q3 - enc.o) / enc.vb[0]) < 65535).all(1)

    def has_grid(self):  # band-table encoders (stream_best / stream_rt ...) have a fixed grid; raw / conf streamers do not
        return hasattr(self.enc, "bands")

    def set_cloud(self, P, C, traj, prefix=None):
        """prefix: number of leading rows of P/C identical to the previous call's input (append-only) -> incremental path."""
        P = np.ascontiguousarray(P, np.float64); C = np.ascontiguousarray(C, np.uint8); L = self.L; n_in_prev = getattr(self, "_n_in", None); self._n_in = len(P)
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
        self.n_dropped = 0
        nk = getattr(self, "_n_kept", -1); Cold = getattr(enc, "C", None); Cfold = getattr(enc, "Cf", None)
        inc = (prefix is not None and prefix == n_in_prev and hasattr(enc, "bands") and len(getattr(self, "_uq", {})) == len(enc.bands)
               and getattr(enc, "Q3", None) is not None and len(enc.Q3) == len(enc.P) == nk
               and (Cold is None or len(Cold) == nk) and (Cfold is None or len(Cfold) == nk) and (Cold is not None or Cfold is not None))
        if inc:  # append-only: only the new rows are transformed / tested / voxelised
            Pn, Cn = P[prefix:], C[prefix:]
            if self.has_grid():
                ok = self._inside(Pn, enc); self.n_dropped = int((~ok).sum())
                if self.n_dropped: Pn, Cn = Pn[ok], Cn[ok]; print(f"[sender] WARNING: {self.n_dropped} points outside the fixed grid dropped", flush=True)
            Qn = Pn @ enc.R.T; enc.P = np.concatenate([enc.P, Pn]); enc.Q3 = np.concatenate([enc.Q3, Qn])
            if Cfold is not None: enc.Cf = np.concatenate([Cfold, Cn.astype(float)])
            if hasattr(enc, "CfT"): enc.CfT = np.ascontiguousarray(enc.Cf.T)
            if Cold is not None: enc.C = np.concatenate([Cold, Cn])
            self._extend_bands(enc, Qn); self._pregrow(enc); self._n_kept = len(enc.P); return
        if self.has_grid():  # only grid encoders must drop points outside the fixed grid (the raw streamer has no grid)
            ok = self._inside(P, enc); self.n_dropped = int((~ok).sum())
            if self.n_dropped: P, C = P[ok], C[ok]; print(f"[sender] WARNING: {self.n_dropped} points outside the fixed grid dropped", flush=True)
        enc.P = P; enc.Q3 = P @ enc.R.T
        if hasattr(enc, "Cf"): enc.Cf = C.astype(float)
        if hasattr(enc, "CfT"): enc.CfT = np.ascontiguousarray(enc.Cf.T)   # stream_rt: transposed colour cache used by its kernels
        if hasattr(enc, "C") or not hasattr(enc, "Cf"): enc.C = C
        if hasattr(enc, "bands"): self._rebuild_bands(enc); self._pregrow(enc)
        self._n_kept = len(P)

    def sent_count(self):  # for the log line only (never used as a gate: -1 = unknown)
        enc = self.enc
        if hasattr(enc, "n_sent"): return int(enc.n_sent)
        if hasattr(enc, "bands") and enc.bands: return int(sum(b["sent"].sum() for b in enc.bands))
        if hasattr(enc, "sent"): return int(sum(len(s) for s in enc.sent))
        return -1

    def update(self, k, traj, dt):
        """-> chunk (the stream header is prepended while header_done() is False). The header is kept until the sender
        confirms it with header_sent(); if the encoder raises before any record went out, a _setup encoder is reset so the
        next update() regenerates its header."""
        hdr = self.hdr_extra
        if hdr: dt = max(dt - len(hdr) / self.rate, 0.1)  # keep the whole frame inside the slot budget
        try: out = self.enc.update(k, traj, dt)
        except Exception:
            if not self.hdr_done and hasattr(self.enc, "_setup") and getattr(self.enc, "R", None) is not None: self.enc.R = None
            raise
        return hdr + out

    def header_sent(self): self.hdr_done = True; self.hdr_extra = b""

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
    def __init__(self, sock, rate, logf, t0, record=None):
        super().__init__(daemon=True); self.sock, self.rate, self.logf, self.t0 = sock, rate, logf, t0
        self.recf = open(record, "wb") if record else None  # replay file: <d t_send><B kind><I len> payload, per frame
        self.q = queue.Queue(); self.sent = 0; self.nframes = 0; self.err = None; self.lock = threading.Lock(); self.inflight = 0
        self.pose = None; self.plock = threading.Lock(); self.pose_bytes = 0; self.npose = 0; self.start()
    def put(self, kind, payload): self.q.put((kind, payload))
    def put_pose(self, payload):  # newest live pose: sent before the next queued frame; an unsent older one is replaced
        with self.plock: self.pose = payload
    def log(self, line):
        with self.lock:
            if not self.logf.closed: self.logf.write(line); self.logf.flush()
    def run(self):
        piece = max(64, min(int(self.rate / 5), 262144)); t_next = time.monotonic()  # <= 256 KB per sendall: pacing / stall granularity
        while True:
            with self.plock: pp, self.pose = self.pose, None
            if pp is not None: item = ("LPOSE", pp)
            else:
                try: item = self.q.get(timeout=0.02)
                except queue.Empty: continue
            if item is None: break
            kind, payload = item; data = struct.pack("<I", len(payload)) + payload; self.inflight = len(data); t_f = time.time()
            try:
                for i in range(0, len(data), piece):
                    part = data[i:i + piece]; now = time.monotonic()
                    if t_next > now: time.sleep(t_next - now); now = t_next
                    if i == 0: t_wire = time.time()  # first byte of this frame leaves (after pacing)
                    self.sock.sendall(part); t_next = now + len(part) / self.rate
            except OSError as e:
                self.err = e; self.inflight = 0; print(f"[link] send failed: {e}", flush=True); return
            if self.recf is not None:
                self.recf.write(struct.pack("<dBI", t_wire, {"REC": 0, "POSE": 1, "LPOSE": 2, "STAT": 3}.get(kind, 0), len(payload)) + payload); self.recf.flush()
            self.sent += len(data); self.nframes += 1; self.inflight = 0
            if kind == "LPOSE": self.pose_bytes += len(data); self.npose += 1; continue  # not logged per frame (5-10 Hz)
            self.log(f"{time.time():.3f} {time.time() - self.t0:8.2f} FRAME {kind} {len(data)} {self.sent} send_s={time.time() - t_f:.2f}\n")
    def pending(self):  # bytes still to go: queued frames + the frame currently inside sendall
        return sum(len(p) + 4 for _, p in list(self.q.queue)) + self.inflight
    def finish(self, timeout):
        """drain: wait until every queued frame left sendall (or the link died / timeout); -> True when fully drained."""
        self.q.put(None); self.join(timeout); return not self.is_alive()

class PoseFeed(threading.Thread):
    """Polls SNAPDIR/pose_live.bin (written by SLAM for every tracked frame) and sends the newest pose at <= hz as a
    POSE frame (same wire format as the per-chunk keyframe pose, dt = 1/hz), via the link's priority slot."""
    def __init__(self, path, link, hz):
        super().__init__(daemon=True); self.path, self.link, self.hz = path, link, hz; self.active = False; self.last_n = None; self.stop = threading.Event(); self.start()
    def run(self):
        while not self.stop.wait(1.0 / self.hz):
            try:
                with open(self.path, "rb") as f: b = f.read(72)
                if len(b) != 72: continue
                n = struct.unpack_from("<Q", b)[0]
                if n == self.last_n: continue
                self.last_n = n; row = np.frombuffer(b[8:72], np.float64)
                if not np.all(np.isfinite(row)): continue
                self.link.put_pose(b"POSE" + row.tobytes() + struct.pack("<d", 1.0 / self.hz)); self.active = True
            except FileNotFoundError: pass
            except Exception as e: print(f"[sender] pose feed: {e!r}", flush=True)

def warm_encoder(mod, rate, margin):
    """Compile (numba) and initialise the streamer once on a synthetic scene (ground strip in front of a camera moving
    forward, SLAM camera convention: +z forward, +y down) so the first real keyframe is encoded in ~0.3 s, not ~8 s.
    The synthetic encoder is thrown away; per-encoder state (incl. stream_rt's ZMODE check) is re-initialised later."""
    t = time.time(); rng = np.random.default_rng(0); n = 60000
    z = rng.uniform(1.3, 4.0, n); x = rng.uniform(-0.6, 0.6, n) * z; y = 0.5 + 0.03 * rng.standard_normal(n)
    box = rng.random(n) < 0.15; y[box] -= rng.uniform(0.0, 0.15, box.sum())  # some "buildings"
    P = np.stack([x, y, z], 1); C = rng.integers(40, 220, (n, 3)).astype(np.uint8)
    traj = np.array([[i * 0.2, 0, 0, 0.1 * i, 0, 0, 0, 1.0] for i in range(3)], np.float64)
    ad = LiveAdapter(mod, rate, margin); ad.set_cloud(P, C, traj); nb = 0
    for k in range(3):
        ch = ad.update(k, traj.copy(), 1.0); recs = split_records(mod, getattr(mod, "__file__", ""), ch, k == 0) if len(ch) else []
        if recs: ad.header_sent()
        nb += len(ch)
    traj2 = np.vstack([traj, traj[-1] + [0.2, 0, 0, 0.1, 0, 0, 0, 0]])  # growth path (new keyframe, more points)
    ad.set_cloud(np.concatenate([P, P + [0, 0, 0.5]]), np.concatenate([C, C]), traj2); ch = ad.update(3, traj2.copy(), 1.0); nb += len(ch)
    try:  # decoder kernels too (same process does not decode, but this catches a broken streamer before the run)
        dec = mod.make_decoder() if hasattr(mod, "make_decoder") else None
    except Exception: dec = None
    print(f"[sender] warm-up: encoder compiled and initialised in {time.time() - t:.1f} s ({nb} B synthetic)", flush=True)

def connect(hp, timeout):
    """TCP connect + wait for the receiver's greeting b'MAPR' (through an ssh -R tunnel the connect succeeds even when nothing
    listens on the far side; the greeting proves a receiver is there). Retries until --connect-timeout."""
    host, port = hp.rsplit(":", 1); t0 = time.time(); n = 0
    while True:
        try:
            s = socket.create_connection((host, int(port)), timeout=5); s.settimeout(10)
            try: hello = s.recv(4)
            except socket.timeout: hello = None  # an old receiver without greeting: proceed
            if hello == b"": s.close(); raise OSError("connection closed before the receiver greeted (tunnel without listener?)")
            if hello is not None and hello != b"MAPR": print(f"[sender] WARNING: unexpected greeting {hello!r}", flush=True)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)  # no Nagle: small pose / status frames go out at once
            s.settimeout(None); return s  # no per-sendall timeout: a slow tunnel / receiver must not kill the link
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
    ap.add_argument("--timeout", type=float, default=90, help="seconds without a new snapshot before exiting (steady state)")
    ap.add_argument("--first-timeout", type=float, default=180, help="seconds to wait for the first usable snapshot / first record")
    ap.add_argument("--connect-timeout", type=float, default=300); ap.add_argument("--slot", type=float, default=0, help="fixed slot length in s (0 = one chunk per snapshot)")
    ap.add_argument("--first-dt", type=float, default=6.0); ap.add_argument("--max-dt", type=float, default=20.0, help="cap on dt in snapshot mode (s)")
    ap.add_argument("--bucket", type=float, default=0.0, help="slot mode: token bucket cap in seconds of --rate (default 0 = 3*slot; bounds the receiver blackout after a slow encode)")
    ap.add_argument("--margin", default="150,50,150", help="grid margin in metres below the first cloud (x,up,z or one value); 500 = spec, costly (dense octree grids)")
    ap.add_argument("--record", default="", help="write every sent frame with its send time to this file (map_replay.py plays it back)")
    ap.add_argument("--alpha-min", type=float, default=0.0, help="stream_rt resolution floor (higher = coarser; 0.014 ~ 0.5-1 m near, 1-2 m far)")
    ap.add_argument("--jit", type=int, default=1, help="1 = just-in-time: a slot never encodes more than the link can send before the next slot")
    ap.add_argument("--urgent-bytes", type=float, default=1000, help="a new keyframe is encoded at once with at most this many bytes (coarse first look), slots continue after")
    ap.add_argument("--no-warmup", action="store_true", help="skip the synthetic warm-up (first keyframe then pays numba compile + setup, ~8 s)")
    ap.add_argument("--pose-hz", type=float, default=5.0, help="live camera pose rate (from SNAPDIR/pose_live.bin; 0 = keyframe pose per chunk only)")
    ap.add_argument("--no-append", action="store_true", help="old behaviour: replace the whole cloud per snapshot (re-sends refined areas)"); ap.add_argument("--log", default=os.path.join(HERE, "sender_log.txt")); ap.add_argument("--max-chunks", type=int, default=0)
    a = ap.parse_args()
    if a.streamer is None: a.streamer = "stream_best.py" if os.path.exists(os.path.join(RESEARCH, "stream_best.py")) else "stream_lod_conf.py"
    spath = a.streamer if os.path.exists(a.streamer) else os.path.join(RESEARCH, a.streamer)
    mod = load_module(spath); margin = [float(x) for x in a.margin.split(",")]
    if a.alpha_min > 0 and hasattr(mod, "ALPHA_MIN"): mod.ALPHA_MIN = a.alpha_min; print(f"[sender] resolution floor alpha >= {a.alpha_min}", flush=True)
    if not a.no_warmup:
        try: warm_encoder(mod, a.rate, margin if len(margin) == 3 else margin[0])
        except Exception: traceback.print_exc(); print("[sender] warm-up failed (continuing; first keyframe will be slow)", flush=True)
    ad = LiveAdapter(mod, a.rate, margin if len(margin) == 3 else margin[0])
    t0 = time.time(); logf = open(a.log, "w"); logf.write(f"# {time.strftime('%F %T')} sender start streamer={os.path.basename(spath)} rate={a.rate} slot={a.slot} snapshots={a.snapshots}\n")
    sock = connect(a.connect, a.connect_timeout); link = Link(sock, a.rate, logf, t0, a.record or None); wlog = link.log
    print(f"[sender] connected to {a.connect}; streamer {os.path.basename(spath)}, {a.rate:.0f} B/s, slot {a.slot or 'per snapshot'}", flush=True)
    feed = PoseFeed(os.path.join(a.snapshots, "pose_live.bin"), link, a.pose_hz) if a.pose_hz > 0 else None; pb_prev = 0
    k = 0; done = set(); fails = {}; t_prev = None; bucket = 0.0; t_last_snap = time.time(); traj = None; snap_name = None; npts = 0
    acc_pts, acc_rgb, acc_kf = [], [], 0; n_rec = 0; last_nrec = 1
    t_stat = [0.0]; got_kf = False; react = None
    def send_stat(k, flags, chunk_b=0, enc_s=0.0, force=False):
        """STAT frame (27 B, ~1/s, counted in the budget): b'STAT' <I slot><H keyframes><I cloud pts><I chunk B><I backlog B><f enc s><B flags>
        flags: 1 = nothing new to send (map up to date), 2 = waiting for SLAM keyframes, 4 = new keyframe ingested since last STAT."""
        nowt = time.time()
        if not force and nowt - t_stat[0] < 0.95: return 0
        t_stat[0] = nowt; pay = b"STAT" + struct.pack("<IHIIIfB", int(k), int(len(traj) if traj is not None else acc_kf), int(npts), int(chunk_b), int(link.pending()), float(enc_s), int(flags))
        link.put("STAT", pay); return len(pay) + 4
    try:
        while True:
            now = time.time()
            if link.err: print("[sender] link closed by receiver"); break
            new = [(m, f) for m, f in list_snaps(a.snapshots) if f not in done]
            if new:
                for _, f in new: done.add(f)
                f = new[-1][1]; f_mtime = new[-1][0]; z = None
                try:  # 1) read the file (atomic writes: a failure here is unexpected; retried at most 3 times)
                    zz = np.load(f); pts, rgb, tr = zz["pts"], zz["rgb"], np.atleast_2d(zz["traj"]).astype(np.float64)
                    kf = int(zz["kf"]) if "kf" in zz else len(tr) - 1; off = zz["kf_off"] if "kf_off" in zz else None; z = True
                except Exception as e:
                    fails[f] = fails.get(f, 0) + 1; print(f"[sender] SNAP-ERROR failed to load {f} (try {fails[f]}): {e}", flush=True); wlog(f"{time.time():.3f} SNAP-ERROR {os.path.basename(f)} {e}\n")
                    if fails[f] < 3: done.discard(f)
                    new = []
                if z is not None:  # 2) ingest (append-only) and hand the cloud to the encoder; acc_* only advance when set_cloud succeeded
                    tl = time.time(); K = len(tr); ingested = False; nnew = 0
                    try:
                        if off is not None and not a.no_append:
                            if K < 2: print(f"[sender] {os.path.basename(f)}: only {K} keyframe(s), waiting for the second one", flush=True)
                            elif K > acc_kf:
                                sl = slice(int(off[acc_kf]), int(off[K])); P = np.concatenate(acc_pts + [pts[sl]]); Cc = np.concatenate(acc_rgb + [rgb[sl]])
                                ad.set_cloud(P, Cc, tr, prefix=sum(len(x) for x in acc_pts)); acc_pts.append(pts[sl]); acc_rgb.append(rgb[sl]); nnew = int(off[K] - off[acc_kf]); acc_kf = K; npts = len(P); ingested = True; got_kf = True
                                react = (K - 1, f_mtime, time.time())  # keyframe, snapshot written, ingested
                            else: print(f"[sender] {os.path.basename(f)}: no new keyframe, ignored (refinements are not re-sent)", flush=True)
                            if K >= 2: traj = tr
                        else: ad.set_cloud(pts, rgb, tr); npts = len(pts); ingested = True; traj = tr
                    except Exception:
                        traceback.print_exc(); print(f"[sender] INGEST-ERROR on {os.path.basename(f)} (snapshot skipped)", flush=True); wlog(f"{time.time():.3f} INGEST-ERROR {os.path.basename(f)}\n")
                    snap_name = os.path.basename(f); t_last_snap = time.time()
                    if ingested: print(f"[sender] loaded {snap_name} kf={kf} pts={npts:,} (+{nnew:,} new) traj={len(tr)} (skipped {len(new) - 1} stale) adapter {time.time() - tl:.1f}s" + (f" dropped {ad.n_dropped} outside grid" if ad.n_dropped else ""), flush=True)
                    wlog(f"{time.time():.3f} {time.time() - t0:8.2f} SNAP {snap_name} kf={kf} snap_pts={len(pts)} cloud_pts={npts} new={nnew} ingested={int(ingested)} skipped={len(new) - 1}\n")
            tmo = a.first_timeout if n_rec == 0 else a.timeout  # the first keyframes / first encode may take long; steady state uses --timeout
            if traj is None:
                if now - t_last_snap > tmo: print(f"[sender] no usable snapshot for {tmo:.0f} s, exiting"); break
                send_stat(k, 2); time.sleep(0.2); continue
            if a.slot > 0:
                if t_prev is not None and now < t_prev + a.slot and react is None: time.sleep(min(t_prev + a.slot - now, 0.05)); continue  # new keyframe -> encode now
                if now - t_last_snap > tmo: print(f"[sender] no new snapshot for {tmo:.0f} s, exiting"); break
                bucket = min(bucket + a.rate * (a.slot if t_prev is None else now - t_prev), a.rate * (a.bucket or 3 * a.slot))  # token bucket: unused bytes roll over
                pb = link.pose_bytes; bucket = max(bucket - (pb - pb_prev), 0.0); pb_prev = pb  # live poses share the budget
                dt = max(bucket - ((0 if (feed is not None and feed.active) else 84) + 4 * max(1, last_nrec) + (80 * a.pose_hz * a.slot if feed is not None and feed.active else 0)), 0.0) / a.rate  # reserve POSE frames + record framing so the wire stays within budget
                if react is not None and a.urgent_bytes > 0: dt = min(dt, a.urgent_bytes / a.rate)  # new keyframe: small, fast first look
                if a.jit: dt = min(dt, max(a.rate * a.slot - link.pending(), 0.0) / a.rate)  # just-in-time: nothing piles up in the link queue
            else:
                if not new:
                    if now - t_last_snap > tmo: print(f"[sender] no new snapshot for {tmo:.0f} s, exiting"); break
                    time.sleep(0.2); continue
                dt = a.first_dt if t_prev is None else min(now - t_prev, a.max_dt)
            t_start = time.time(); before = ad.sent_count(); first = not ad.hdr_done
            try:
                chunk = ad.update(k, traj.copy(), dt)
                recs = split_records(mod, spath, chunk, first) if len(chunk) else []  # gate on the chunk itself (never on a sentinel)
            except Exception:
                traceback.print_exc(); wlog(f"{time.time():.3f} ENCODE-ERROR slot {k}\n"); t_prev = t_start; k += 1; continue
            enc_s = time.time() - t_start
            if recs:
                pose = b"" if (feed is not None and feed.active) else b"POSE" + np.ascontiguousarray(traj[-1], np.float64).tobytes() + struct.pack("<d", dt)
                if pose: link.put("POSE", pose)
                for r in recs: link.put("REC", r)
                if react is not None:
                    tq = time.time(); wlog(f"{tq:.3f} {tq - t0:8.2f} REACT kf={react[0]} first_rec={n_rec + 1} snap_written={react[1]:.3f} ingest_s={react[2] - react[1]:.3f} encode_s={tq - react[2]:.3f} queued_behind={link.pending() - sum(len(r) + 4 for r in recs)}B\n")
                    print(f"[sender] REACT kf={react[0]}: snapshot->queued {tq - react[1]:.2f} s (ingest {react[2] - react[1]:.2f} + encode {tq - react[2]:.2f})", flush=True); react = None
                ad.header_sent(); n_rec += len(recs); last_nrec = len(recs)
                if a.slot > 0: bucket = max(bucket - ((len(pose) + 4 if pose else 0) + sum(len(r) + 4 for r in recs)), 0.0)
            sb = send_stat(k, (0 if recs else 1) | (4 if got_kf else 0), len(chunk), enc_s, force=True); got_kf = False
            if a.slot > 0: bucket = max(bucket - sb, 0.0)
            msg = (f"chunk {k}: {snap_name} kf={len(traj) - 1} pts={npts:,} dt={dt:.2f}s budget={a.rate * dt:.0f}B bytes={len(chunk)} "
                   f"({100 * len(chunk) / max(a.rate * dt, 1e-9):.0f}%) records={len(recs)} enc={enc_s:.2f}s sent={before}->{ad.sent_count()} alpha={getattr(ad.enc, 'alpha', float('nan')):.4f} queued={link.q.qsize()} backlog={link.pending()}B"
                   + (f" bucket={bucket:.0f}B" if a.slot > 0 else "") + ("" if recs else " [empty, skipped]"))
            print(f"[sender] {msg}", flush=True); wlog(f"{time.time():.3f} {time.time() - t0:8.2f} CHUNK {msg}\n")
            t_prev = t_start; k += 1
            if a.max_chunks and k >= a.max_chunks: print("[sender] max chunks reached"); break
    except KeyboardInterrupt: print("[sender] interrupted")
    if feed is not None: feed.stop.set()
    pending = link.pending() if not link.err else 0; t_d = time.time()
    print(f"[sender] draining {pending} queued bytes (~{pending / a.rate:.0f} s at --rate; the tunnel may be slower)", flush=True)
    ok = link.finish(timeout=(max(300.0, pending / a.rate + 30) if not link.err else 5.0))
    if not ok: print(f"[sender] WARNING: link still sending after {time.time() - t_d:.0f} s, closing anyway", flush=True)
    try: sock.shutdown(socket.SHUT_RDWR)
    except OSError: pass
    sock.close()
    if link.is_alive(): link.join(2)
    with link.lock: logf.write(f"# end: {k} chunks, {link.nframes} frames, {link.sent} bytes on the wire in {time.time() - t0:.1f} s (drain {time.time() - t_d:.1f} s)\n"); logf.close()
    print(f"[sender] live poses sent: {link.npose} ({link.pose_bytes} B)", flush=True); print(f"[sender] done: {k} chunks, {link.nframes} frames, {link.sent} B on the wire, {time.time() - t0:.1f} s (drain {time.time() - t_d:.1f} s)", flush=True)

if __name__ == "__main__": main()
