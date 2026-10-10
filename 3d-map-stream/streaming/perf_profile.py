"""perf_profile: time breakdown of the live coder (stream_best_rec, optionally stream_best_pkt) driven EXACTLY like
live_eval.py --rate 20 --slot 1 for the first N slots (default 10).  Pure wrapper timers around the real functions
(no logic change -> identical bytes), per slot and per decoder record, plus RSS.
usage (home):  python perf_profile.py [--slots 10] [--rate 20] [--slot 1] [--streamer stream_best_rec.py ...] [--cprofile]
Timers are INCLUSIVE (kn_grid and _ctx are inside walk; _geom_ctx/_vote inside greedy_band; select inside trial)."""
import sys, os, time, math, argparse, struct, resource, importlib.util, cProfile, pstats, io, numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import quality as Q
import stream_best as SB

T = {}; SIDE = ["enc"]; TRIALS = []; RECS = []

def rss_mb():
    try:
        for ln in open("/proc/self/status"):
            if ln.startswith("VmRSS"): return int(ln.split()[1]) / 1024.0
    except Exception: pass
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0

def acc(name, dt): k = SIDE[0] + "." + name; T[k] = T.get(k, 0.0) + dt

def timed(name, fn):
    def w(*a, **kw):
        t = time.perf_counter(); r = fn(*a, **kw); acc(name, time.perf_counter() - t); return r
    w.__name__ = getattr(fn, "__name__", name); w.__wrapped__ = fn; return w

def snapshot(): return dict(T)
def delta(a, b): return {k: b.get(k, 0.0) - a.get(k, 0.0) for k in set(a) | set(b)}

# ---------- timed re-implementation of Encoder.select with phase timers (verified identical to SB.Encoder.select) ----------
def timed_select(self, alpha, z, ztrue):
    t = time.perf_counter()
    want = alpha * z; band = np.abs(np.log(want)[:, None] - np.log(SB.LEVELS)[None]).argmin(1); out = []
    acc("sel.bandassign", time.perf_counter() - t)
    for bi in range(len(SB.LEVELS) - 1, -1, -1):
        B = self.bands[bi]; t = time.perf_counter(); m = band == bi
        if not m.any(): acc("sel.vox", time.perf_counter() - t); continue
        inv = B["inv"][m]; cnt = np.bincount(inv, minlength=B["n"]); ok = cnt >= SB.MINCNT; keep = ok.copy()
        acc("sel.vox", time.perf_counter() - t); t = time.perf_counter()
        if SB.CONF > 0 and ok.any():
            zs = np.bincount(inv, ztrue[m], minlength=B["n"]); dens = cnt * (zs / np.maximum(cnt, 1)) ** 2
            keep &= dens >= SB.CONF * np.median(dens[ok])
        keep &= ~B["sent"]; idx = np.nonzero(keep)[0]
        acc("sel.conf", time.perf_counter() - t)
        if len(idx) == 0: continue
        t = time.perf_counter()
        col = np.stack([np.bincount(inv, self.Cf[m, ch], minlength=B["n"])[idx] for ch in range(3)], 1) / cnt[idx, None]
        out.append((bi, B["u"][idx], np.round(col).astype(np.uint8), idx)); acc("sel.colmean", time.perf_counter() - t)
    return out

def install(mods):
    """patch SB and every streamer module namespace that imported names from SB."""
    SB.walk = timed("walk", SB.walk); SB.kn_grid = timed("kn_grid", SB.kn_grid); SB._ctx = timed("walk._ctx", SB._ctx)
    SB.greedy_band = timed("greedy", SB.greedy_band); SB._geom_ctx = timed("greedy._geom_ctx", SB._geom_ctx); SB._vote = timed("greedy._vote", SB._vote)
    SB.rc_encode = timed("rc_encode", SB.rc_encode); SB.parent_ctx = timed("parent_ctx", SB.parent_ctx)
    SB.State.copy = timed("state.copy", SB.State.copy); SB.ColState.copy = timed("colstate.copy", SB.ColState.copy)
    SB.RcDec.band = timed("col_decode", SB.RcDec.band); SB.Decoder.map = timed("map", SB.Decoder.map)
    SB.Encoder._z = timed("zeff", SB.Encoder._z); SB.Encoder._setup = timed("setup", SB.Encoder._setup)
    orig_select = SB.Encoder.select
    def sel(self, alpha, z, ztrue):
        t = time.perf_counter(); r = timed_select(self, alpha, z, ztrue); acc("select", time.perf_counter() - t)
        if not getattr(self, "_sel_verified", False):  # one-time identity check of the timed copy vs the original
            r0 = orig_select(self, alpha, z, ztrue); assert len(r0) == len(r)
            for a, b in zip(r0, r): assert a[0] == b[0] and np.array_equal(a[1], b[1]) and np.array_equal(a[2], b[2]) and np.array_equal(a[3], b[3])
            self._sel_verified = True
        return r
    SB.Encoder.select = sel
    orig_trial = SB.Encoder.trial
    for m in mods:
        for nm in ("walk", "kn_grid", "greedy_band", "rc_encode", "parent_ctx"):
            if hasattr(m, nm) and getattr(m, nm) is getattr(SB, nm).__wrapped__: setattr(m, nm, getattr(SB, nm))
        if hasattr(m, "kn_grid_off"): m.kn_grid_off = timed("kn_grid", m.kn_grid_off)
        if hasattr(m, "code_records"): m.code_records = timed("code_records", m.code_records)
        if hasattr(m, "code_record"): m.code_record = timed("code_records", m.code_record)
        if hasattr(m, "Encoder"):
            tr = m.Encoder.trial
            def mk(tr):
                def trial(self, alpha, z, ztrue, *a, **kw):
                    s0 = snapshot(); t = time.perf_counter(); r = tr(self, alpha, z, ztrue, *a, **kw); dt = time.perf_counter() - t
                    acc("trial", dt); TRIALS.append(dict(alpha=alpha, bytes=len(r[0]), s=dt, sub=delta(s0, snapshot()))); return r
                return trial
            m.Encoder.trial = mk(tr)
        if hasattr(m, "Decoder") and hasattr(m.Decoder, "_record"):
            rec = m.Decoder._record
            def mkr(rec):
                def _record(self, bi, pl):
                    s0 = snapshot(); t = time.perf_counter(); rec(self, bi, pl); dt = time.perf_counter() - t
                    d = delta(s0, snapshot()); acc("record", dt)
                    RECS.append(dict(band=bi, bytes=len(pl) + 5, s=dt, walk=d.get("dec.walk", 0), kn=d.get("dec.kn_grid", 0), col=d.get("dec.col_decode", 0), pctx=d.get("dec.parent_ctx", 0)))
                return _record
            m.Decoder._record = mkr(rec)

def load(path):
    s = importlib.util.spec_from_file_location(os.path.basename(path)[:-3], path); m = importlib.util.module_from_spec(s); s.loader.exec_module(m); return m

def fmt(v): return "%6.3f" % v

def run(mod, name, P, C, traj, rate, slot, nslots, cprof):
    T.clear(); TRIALS.clear(); RECS.clear(); SIDE[0] = "enc"
    ts = traj[:, 0]; nslot = int(np.ceil((ts[-1] - ts[0] + float(np.median(np.diff(ts)))) / slot))
    slots = [(ts[0] + i * slot, slot, max(1, int(np.searchsorted(ts, ts[0] + i * slot, side="right")))) for i in range(nslot)][:nslots]
    t0 = time.perf_counter(); enc = mod.make_encoder(rate, P.copy(), C.copy()); dec = mod.make_decoder(); tinit = time.perf_counter() - t0
    print("\n==== %s  (%d slots of %g s at %g kbit/s; init incl. palette k-means %.2f s; RSS %.0f MB) ====" % (name, len(slots), slot, rate * 8 / 1e3, tinit, rss_mb()))
    hdr = "slot  bytes/bud  ntr  UPDATE  setup  zeff  select(vox conf colmean) trial  walk(kn ctx) greedy(gctx vote) rc_enc pctx stcopy pack | nrec APPLY  walk  coldec  map   RSS"
    print(hdr); rows = []; bucket = 0.0; pr = cProfile.Profile() if cprof else None
    split = getattr(mod, "split_records", None)
    for k, (tk, dt, nkf) in enumerate(slots):
        bucket += rate * dt; dt_eff = bucket / rate; budget = rate * dt_eff
        SIDE[0] = "enc"; s0 = snapshot(); ntr0 = len(TRIALS); r0 = rss_mb()
        t1 = time.perf_counter()
        if pr: pr.enable()
        chunk = enc.update(k, traj[:nkf].copy(), dt_eff)
        if pr: pr.disable()
        tenc = time.perf_counter() - t1; bucket -= len(chunk); e = delta(s0, snapshot()); g = lambda n: e.get("enc." + n, 0.0)
        ntr = len(TRIALS) - ntr0; tri = TRIALS[ntr0:]
        pack = g("trial") - g("select") - g("walk") - g("greedy") - g("rc_encode") - g("parent_ctx") - g("state.copy")
        SIDE[0] = "dec"; s0 = snapshot(); nrec0 = len(RECS)
        recs = split(chunk, k == 0) if split else [chunk]
        t2 = time.perf_counter()
        if pr: pr.enable()
        for r in recs: dec.apply(r)
        tapply = time.perf_counter() - t2
        t3 = time.perf_counter(); p2, c2, spl = dec.map(); tmap = time.perf_counter() - t3
        if pr: pr.disable()
        d = delta(s0, snapshot()); h = lambda n: d.get("dec." + n, 0.0)
        row = dict(k=k, bytes=len(chunk), budget=budget, ntr=ntr, update=tenc, setup=g("setup"), zeff=g("zeff"), select=g("select"), vox=g("sel.vox"), conf=g("sel.conf"), colmean=g("sel.colmean"),
                   trial=g("trial"), walk=g("walk"), kn=g("kn_grid"), ctx=g("walk._ctx"), greedy=g("greedy"), gctx=g("greedy._geom_ctx"), vote=g("greedy._vote"), rc=g("rc_encode"), pctx=g("parent_ctx"), stcopy=g("state.copy"), pack=pack,
                   nrec=len(recs), apply=tapply, dwalk=h("walk"), dcol=h("col_decode"), map=tmap, rss=rss_mb(), pts=len(p2), trials=[(round(x["alpha"], 4), x["bytes"], round(x["s"], 3)) for x in tri])
        rows.append(row)
        print("%4d %6d/%-5.0f %3d  %6.3f %6.3f %6.3f %6.3f(%5.3f %5.3f %5.3f) %6.3f %6.3f(%5.3f %5.3f) %6.3f(%5.3f %5.3f) %6.3f %5.3f %5.3f %5.3f | %3d %6.3f %6.3f %6.3f %6.3f %5.0f" % (
            k, len(chunk), budget, ntr, tenc, row["setup"], row["zeff"], row["select"], row["vox"], row["conf"], row["colmean"], row["trial"], row["walk"], row["kn"], row["ctx"], row["greedy"], row["gctx"], row["vote"], row["rc"], row["pctx"], row["stcopy"], pack,
            len(recs), tapply, row["dwalk"], row["dcol"], tmap, row["rss"]), flush=True)
        print("      trials (alpha, bytes, s):", row["trials"], " recs:", [(r["band"], r["bytes"], round(r["s"], 3)) for r in RECS[nrec0:]], flush=True)
    n = len(rows); s = lambda f: sum(r[f] for r in rows)
    print("---- totals over %d slots: update %.2f s (mean %.2f, max %.2f) | zeff %.2f | select %.2f (vox %.2f conf %.2f colmean %.2f) | trials %d -> %.2f s: walk %.2f (kn_grid %.2f _ctx %.2f) greedy %.2f (geom_ctx %.2f vote %.2f) rc_encode %.2f parent_ctx %.2f state.copy %.2f pack/other %.2f" % (
        n, s("update"), s("update") / n, max(r["update"] for r in rows), s("zeff"), s("select"), s("vox"), s("conf"), s("colmean"), s("ntr"), s("trial"), s("walk"), s("kn"), s("ctx"), s("greedy"), s("gctx"), s("vote"), s("rc"), s("pctx"), s("stcopy"), s("pack")))
    if RECS:
        rb = np.array([r["bytes"] for r in RECS]); rs = np.array([r["s"] for r in RECS])
        print("---- decoder: %d records, %.0f B mean (max %d); apply/record mean %.3f s max %.3f s; walk %.3f coldec %.3f parent_ctx %.3f kn_grid %.3f (mean per record); dec.map mean %.3f s max %.3f s (%d pts final)" % (
            len(RECS), rb.mean(), rb.max(), rs.mean(), rs.max(), np.mean([r["walk"] for r in RECS]), np.mean([r["col"] for r in RECS]), np.mean([r["pctx"] for r in RECS]), np.mean([r["kn"] for r in RECS]), s("map") / n, max(r["map"] for r in rows), rows[-1]["pts"]))
        print("     per-500B normalised: apply %.3f s per 500 B" % (rs.sum() / rb.sum() * 500))
    print("---- RSS now %.0f MB, peak %.0f MB" % (rss_mb(), resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0))
    if pr:
        st = pstats.Stats(pr, stream=sys.stdout); print("\n---- cProfile top 35 by tottime (enc.update + dec.apply + dec.map of all slots; timers add some overhead)"); st.sort_stats("tottime").print_stats(35)
    return rows

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--slots", type=int, default=10); ap.add_argument("--rate", type=float, default=20.0); ap.add_argument("--slot", type=float, default=1.0)
    ap.add_argument("--streamer", nargs="*", default=["stream_best_rec.py", "stream_best_pkt.py"]); ap.add_argument("--cprofile", action="store_true"); a = ap.parse_args()
    here = os.path.dirname(os.path.abspath(__file__)); t = time.perf_counter(); P, C, traj = Q.load_ref(); print("load_ref %.1f s, %d pts, RSS %.0f MB" % (time.perf_counter() - t, len(P), rss_mb()))
    mods = []
    for s in a.streamer:
        try: mods.append((s, load(os.path.join(here, s))))
        except Exception as ex: print("skip %s: import failed: %r" % (s, ex))
    install([m for _, m in mods])
    for s, m in mods:
        try: run(m, s, P, C, traj, a.rate * 1e3 / 8, a.slot, a.slots, a.cprofile)
        except Exception as ex:
            import traceback; traceback.print_exc(); print("skip %s: run failed: %r" % (s, ex))

if __name__ == "__main__": main()
