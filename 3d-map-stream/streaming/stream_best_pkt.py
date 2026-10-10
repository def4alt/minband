"""stream_best_pkt: stream_best_rec (stream_best coder, per-band records, persistent contexts) with a PACKET SIZE:
every record is ~PKT bytes (env PKT, default 600) so the operator's map updates several times per second instead of
once per planning slot.

Within a planning slot (same selection / budget fit as stream_best):
  * bands coarse -> fine (whole area appears first, finer blocks refine it);
  * inside a band the new voxels are ordered NEAREST TO THE DRONE first (drone = last pose of the passed trajectory,
    mapped into the grid frame) and cut into groups whose record is ~PKT bytes: the group size is estimated from a
    running bits/voxel estimate per band (encoder-only state, updated after every record) and re-cut once if the coded
    record misses PKT by more than 40 %;
  * every group is its own self-contained record <B band><I n><payload> (payload = <B depth><3H box origin><3B/3H
    extent-1><varint lg> (~11 B) + geometry range-coder stream (lg B) + colour range-coder stream): the octree of a
    record covers only its own box (origin aligned to the box size in the global grid, so the known-voxel context flags
    are exactly those of stream_best inside the box), coded with the SAME persistent adaptive context tables,
    colour model and 16-colour palette as stream_best / stream_best_rec: encoder and decoder update them record by
    record in transmission order, so the only per-record cost is the two range-coder flushes + ~16 B of record fields
    (measured ~30-40 B per record incl. context effects);
  * the stream header (same layout as stream_best, 117 B) rides on the first record of the whole stream.
The budget fit codes the packetized chunk directly (exact: every slot stays within its budget).
Pairing measurement (env PKT_PAIR=1, default): after each slot the SAME voxels are also coded as one record per band
(stream_best_rec coding) on a copy of the pre-slot state -> enc.stats[k] = dict(pkt_bytes, band_bytes, nrec, ...) so
live_eval_pkt can report the exact packetization overhead.
API: make_encoder / make_decoder as live_eval.py; split_records(chunk_bytes, first) -> records in transmission order;
Decoder.apply accepts a single record, any run of records, or a whole chunk; Decoder.map() valid after every record.
PKT=1e9 -> one record per band (== stream_best_rec)."""
import os, sys, math, struct, numpy as np, constriction
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stream_best as SB, stream_best_rec as SR
from stream_best import walk, kn_grid, greedy_band, rc_encode, parent_ctx, LEVELS, NS, SPL, MAXT, FILL, TIE
import lod_common as L

PKT = float(os.environ.get("PKT", 600)); PAIR = os.environ.get("PKT_PAIR", "1") == "1"
MINVOX = int(os.environ.get("PKT_MINVOX", 8)); RECUT = float(os.environ.get("PKT_RECUT", 0.4))
ORDER = os.environ.get("PKT_ORDER", "block")  # block: nearest-first at the granularity of 2^m-voxel blocks (compact records); shell: pure distance sort
header_len = SR.header_len; split_records = SR.split_records
def _lex(u): return np.lexsort(u.T[::-1])

def kn_grid_off(bi, sh, e, known, org):
    """stream_best.kn_grid restricted to the box [org, org+ext) of band bi (org in band-bi voxel units, a multiple of
    2^D): identical flags to the global grid inside the box; cells outside the box are 'not covered' like beyond ext."""
    KN = np.zeros(tuple(e + 4), np.int8)
    if not SB.KNOWN: return KN
    osh = org >> sh
    for val in (1, 2):
        for bj, U in enumerate(known):
            if len(U) == 0: continue
            s = int(round(math.log2(LEVELS[bj] / LEVELS[bi]))) - sh
            if (val == 1) != (s > 0): continue
            if s > 0:
                lo = (osh - (1 << s) + 1) >> s; hi = (osh + e - 1) >> s; U = U[(U >= lo).all(1) & (U <= hi).all(1)]
                if len(U) == 0: continue
                r = np.arange(1 << s); offs = np.stack(np.meshgrid(r, r, r, indexing="ij"), -1).reshape(-1, 3)
                cells = (((U << s) - osh)[:, None, :] + offs[None]).reshape(-1, 3)
            elif s == 0: cells = U - osh
            else: cells = (U >> (-s)) - osh
            m = (cells >= 0).all(1) & (cells < e).all(1); cells = cells[m] + 2
            KN[cells[:, 0], cells[:, 1], cells[:, 2]] = val
    return KN

def box(u):
    """-> (org, ext, D): smallest octree box (aligned to its own size 2^D in the global grid) that holds the voxels."""
    lo, hi = u.min(0), u.max(0); D = max(1, int(np.ceil(np.log2((hi - lo + 1).max()))))
    while True:
        org = (lo >> D) << D; ext = hi - org + 1
        if ext.max() <= (1 << D): return org, ext, D
        D += 1

def code_record(bi, u, col, st, pal64):
    """one self-contained record for band bi (u int64 lexsorted, col uint8). Mutates st like stream_best_rec.
    payload = pack_hdr (D, org, ext, lg: ~11 B) + geometry stream (lg B) + colour stream."""
    enc = constriction.stream.queue.RangeEncoder()
    def coder(fam, p, y): enc.encode(y, fam, p); return y
    org, ext, D = box(u)
    walk(ext, D, coder, st, lambda sh, e: kn_grid_off(bi, sh, e, st.known, org), u - org)
    st.known[bi] = np.concatenate([st.known[bi], u]); g = enc.get_compressed().tobytes()
    pre = st.col.copy(); syms, ctxs = [], []
    idx = greedy_band(u, col, pal64, st.col, syms, ctxs, parent_ctx(bi, u, st)); st.cidx[bi] = np.concatenate([st.cidx[bi], idx])
    cb = rc_encode(syms, ctxs, pre); st.col = pre
    payload = pack_hdr(org, ext, D, len(g)) + g + cb
    return struct.pack("<BI", bi, len(payload)) + payload

def pack_hdr(org, ext, D, lg):
    """compact payload header: <B D><3H org> + ext-1 as 3B (D<=8) or 3H + lg as 1 B (<255) or 0xFF+<I>  -> 11 B typical."""
    h = struct.pack("<B3H", D, *map(int, org)) + struct.pack("<3B" if D <= 8 else "<3H", *map(int, ext - 1))
    return h + (struct.pack("<B", lg) if lg < 255 else struct.pack("<BI", 255, lg))

def unpack_hdr(pl):
    D, ox, oy, oz = struct.unpack("<B3H", pl[:7]); p = 7
    if D <= 8: ext = np.array(struct.unpack("<3B", pl[p:p + 3]), np.int64) + 1; p += 3
    else: ext = np.array(struct.unpack("<3H", pl[p:p + 6]), np.int64) + 1; p += 6
    lg = pl[p]; p += 1
    if lg == 255: lg = struct.unpack("<I", pl[p:p + 4])[0]; p += 4
    return np.array([ox, oy, oz], np.int64), ext, D, lg, p

class Decoder(SR.Decoder):
    """stream header + record framing as stream_best_rec; record payload with the local box origin."""
    def _record(self, bi, pl):
        org, ext, D, lg, p = unpack_hdr(pl); g = pl[p:p + lg]; cb = pl[p + lg:] + b"\0" * 5
        dec = constriction.stream.queue.RangeDecoder(np.frombuffer(g, np.uint32).copy())
        def coder(fam, pr, y): return dec.decode(fam, pr).astype(np.int32)
        u = walk(ext, D, coder, self.st, lambda sh, e: kn_grid_off(bi, sh, e, self.st.known, org)) + org
        self.st.known[bi] = np.concatenate([self.st.known[bi], u])
        rd = SB.RcDec(cb, self.st.col)
        self.st.cidx[bi] = np.concatenate([self.st.cidx[bi], rd.band(u, parent_ctx(bi, u, self.st))])

class Encoder(SB.Encoder):
    def __init__(self, rate, P, C):
        super().__init__(rate, P, C); self.bpv = [6.0] * len(LEVELS); self.stats = []; self._drone = None

    def order(self, bi, u, dg, g):
        """nearest-to-the-drone-first order of the band's voxels. ORDER=block: whole 2^m-voxel blocks (m chosen so a
        ~g-voxel record spans >= 8 blocks) sorted by block-centre distance, voxels inside a block in lex order."""
        d = np.linalg.norm((u + 0.5) * self.vb[bi] - dg, axis=1)
        if ORDER != "block": return np.argsort(d, kind="stable")
        m = 0
        for mm in range(1, 7):
            if len(np.unique(SB.keyf(u >> mm))) < 8 * len(u) / g: break
            m = mm
        if m == 0: return np.argsort(d, kind="stable")
        kb = SB.keyf(u >> m); blk, inv = np.unique(kb, return_inverse=True); inv = inv.ravel()
        bc = np.stack([blk >> 42, (blk >> 21) & SB.M21, blk & SB.M21], 1); db = np.linalg.norm(((bc + 0.5) * (1 << m)) * self.vb[bi] - dg, axis=1)
        rank = np.argsort(np.argsort(db, kind="stable"), kind="stable")  # rank of each block by distance
        return np.lexsort((u[:, 2], u[:, 1], u[:, 0], rank[inv]))

    def code_pkt(self, bands, st, limit=None, abort=None):
        """bands coarse->fine [(bi, u, col, idx)]; cut each band into ~PKT records nearest-first.
        limit: stop before the record that would exceed `limit` bytes (fallback only). Returns (bytes, nrec, kept) with
        kept = [(bi, idx of the voxels actually coded)] so the caller marks only those as sent.
        abort: stop as soon as the output exceeds `abort` bytes (the trial is over budget anyway) and return an ESTIMATE
        of the full size (coded bytes + remaining voxels * bits/voxel) as 4th value; otherwise the exact size."""
        out = b""; nrec = 0; kept = []; pal64 = self.pal.astype(np.float64)
        dg = (self.R @ self._drone - self.o) if self._drone is not None else None  # drone in grid (ground) frame, metres
        for bi, u, col, idx in bands:
            n = len(u)
            if dg is not None and n > MINVOX:
                o = self.order(bi, u, dg, max(MINVOX, int(PKT * 8 / self.bpv[bi]))); u, col, idx = u[o], col[o], idx[o]
            i = 0; full = True
            while i < n:
                g = max(MINVOX, int(PKT * 8 / self.bpv[bi]))
                if n - i < 1.5 * g: g = n - i                      # absorb a small remainder into the last record
                for attempt in range(2):
                    sel = _lex(u[i:i + g]); st2 = st.copy(); rec = code_record(bi, u[i:i + g][sel], col[i:i + g][sel], st2, pal64)
                    bpv = 8 * len(rec) / g; self.bpv[bi] = 0.5 * self.bpv[bi] + 0.5 * bpv
                    if attempt == 0 and g < n - i and abs(len(rec) - PKT) > RECUT * PKT:  # re-cut once with the measured bits/voxel
                        g = max(MINVOX, int(PKT * 8 / bpv))
                        if n - i < 1.5 * g: g = n - i
                        continue
                    break
                if limit is not None and len(out) + len(rec) > limit:  # fallback: one smaller record to fill what is left
                    g = int((limit - len(out) - 16) * 8 / max(bpv, 1e-3) * 0.9)
                    if g >= MINVOX:
                        sel = _lex(u[i:i + g]); st2 = st.copy(); rec = code_record(bi, u[i:i + g][sel], col[i:i + g][sel], st2, pal64)
                        if len(out) + len(rec) <= limit:
                            st.tb, st.tf, st.col, st.known, st.cidx = st2.tb, st2.tf, st2.col, st2.known, st2.cidx; out += rec; nrec += 1; i += g
                    full = False; break
                st.tb, st.tf, st.col, st.known, st.cidx = st2.tb, st2.tf, st2.col, st2.known, st2.cidx
                out += rec; nrec += 1; i += g
                if abort is not None and len(out) > abort:
                    rest = sum(len(bb[1]) * self.bpv[bb[0]] / 8 for bb in bands if bb[0] < bi)  # finer bands still to come
                    return out, nrec, kept + [(bi, idx[:i])], len(out) + (n - i) * self.bpv[bi] / 8 + rest
            kept.append((bi, idx[:i]))
            if not full: break
        return out, nrec, kept, len(out)

    def trial(self, alpha, z, ztrue, limit=None, abort=None):
        st = self.st.copy(); bands = self.select(alpha, z, ztrue)
        b, nrec, kept, est = self.code_pkt(bands, st, limit, abort); self._nrec = nrec; self._kept = kept; self._est = est
        return b, st, bands

    def update(self, k, traj, dt):
        budget = self.rate * dt; hdr = b""; self._drone = traj[-1, 1:4].astype(float)
        if self.R is None:
            self._setup(traj)
            hdr = (self.R.astype(np.float32).tobytes() + self.o.astype(np.float32).tobytes() + struct.pack("<fB", SPL, len(LEVELS))
                   + LEVELS.astype(np.float32).tobytes() + self.pal.tobytes())
        z, ztrue = self._z(traj, dt)
        if TIE > 0:
            d = np.linalg.norm(self.P - traj[-1, 1:4], axis=1); z = z * (1 + TIE * d / d.max())
        bud = budget - len(hdr); a = self.alpha; best = None; lo = hi = None; tr = []; pre = self.st.copy()
        for it in range(MAXT):
            b, st, bands = self.trial(a, z, ztrue, abort=bud); n = self._est; tr.append((a, n)); self.ntrials = it + 1
            if n <= bud:
                if best is None or n > len(best[0]): best = (b, st, bands, a, self._nrec, self._kept)
                hi = a if hi is None else min(hi, a)
                if n > FILL * bud: break
            else: lo = a if lo is None else max(lo, a)
            if lo is not None and hi is not None and hi / lo < 1.001: break
            if len(tr) >= 2 and tr[-1][1] != tr[-2][1] and tr[-1][0] != tr[-2][0] and min(tr[-1][1], tr[-2][1]) > 0:
                g = -math.log(tr[-1][1] / tr[-2][1]) / math.log(tr[-1][0] / tr[-2][0]); self.g = min(max(g, 0.7), 30.0)
            a_new = a * (n / (0.99 * bud)) ** (1 / self.g) if n > 0 else a * 0.85  # nothing new at this LOD: step gently finer
            if lo is not None and hi is not None and not (lo < a_new < hi): a_new = math.sqrt(lo * hi)
            elif lo is not None and a_new <= lo: a_new = lo * 1.05
            elif hi is not None and a_new >= hi: a_new = hi / 1.05
            a = a_new
        if lo is not None and (best is None or len(best[0]) < 0.9 * bud) and tr[[x[0] for x in tr].index(lo)][1] < 2.5 * bud:
            # poorly filled: take the finest over-budget LOD and drop its farthest records (nearest-first order) to fit
            b, st, bands = self.trial(lo, z, ztrue, limit=bud); self.ntrials += 1
            if best is None or len(b) > len(best[0]): best = (b, st, bands, lo, self._nrec, self._kept)
        while best is None:  # nothing fits (coarsest band saturated): keep alpha, drop the farthest records to fit the budget
            a *= 1.5; b, st, bands = self.trial(a, z, ztrue, limit=bud)
            if len(b) <= bud or a > 1.0: best = (b, st, bands, a, self._nrec, self._kept)
        b, st, bands, a, nrec, kept = best; self.alpha = min(a, 1.0); self.st = st
        for bi, idx in kept: self.bands[bi]["sent"][idx] = True
        sent = [(bi, u[np.isin(idx, ki)], col[np.isin(idx, ki)]) for (bi, u, col, idx), (_, ki) in zip(bands, kept)]
        sent = [(bi, u[_lex(u)], c[_lex(u)]) for bi, u, c in sent if len(u)]
        row = dict(k=k, pkt_bytes=len(b), nrec=nrec, nvox=int(sum(len(u) for _, u, _ in sent)), hdr=len(hdr))
        if PAIR and sent:  # same voxels, one record per band (stream_best_rec coding) from the same pre-slot state
            row['band_bytes'] = len(SR.code_records(sent, pre, self.pal))
        self.stats.append(row)
        if os.environ.get("DBG"): print(f"   slot {k}: {self.ntrials} trials alpha {a:.4f} {len(b)}/{bud:.0f} B {nrec} rec " + " ".join(f"{LEVELS[bi]}m:{len(u)}" for bi, u, _ in sent) + (f" band-coded {row['band_bytes']} B" if "band_bytes" in row else "") + " tr " + str([(round(x, 4), int(y)) for x, y in tr]))
        return hdr + b

def make_encoder(rate, P, C): return Encoder(rate, P, C)
def make_decoder(): return Decoder()
