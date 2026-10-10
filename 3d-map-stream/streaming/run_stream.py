"""Drive a live streamer exactly like live_eval.py does and dump the wire bytes + decoded maps.
usage: python run_stream.py stream_x.py OUTDIR
writes OUTDIR/wire/chunk{k:02d}.bin, OUTDIR/map_after_slot{k}.ply, OUTDIR/final.ply, final.txt (trajectory), final.splat"""
import sys, os, time, json, importlib.util, shutil, numpy as np, open3d as o3d
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import quality as Q
streamer, out = sys.argv[1], sys.argv[2]; RATE_KBIT = 10.0; rate = RATE_KBIT * 1e3 / 8
os.makedirs(f"{out}/wire", exist_ok=True)
P, C, traj = Q.load_ref(); K = len(traj); ts = traj[:, 0]
s = importlib.util.spec_from_file_location("streamer", streamer); m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
enc = m.make_encoder(rate, P.copy(), C.copy()); dec = m.make_decoder(); rows = []
def save_ply(path, p, c):
    pc = o3d.geometry.PointCloud(); pc.points = o3d.utility.Vector3dVector(np.asarray(p, float))
    pc.colors = o3d.utility.Vector3dVector(np.asarray(c, float) / 255.0); o3d.io.write_point_cloud(path, pc)
for k in range(K):
    dt = (ts[k + 1] - ts[k]) if k + 1 < K else float(np.median(np.diff(ts))); budget = rate * dt
    t1 = time.time(); chunk = enc.update(k, traj[: k + 1].copy(), dt); tenc = time.time() - t1
    open(f"{out}/wire/chunk{k:02d}.bin", "wb").write(chunk)
    t2 = time.time(); dec.apply(chunk); p2, c2, splat = dec.map(); tdec = time.time() - t2
    save_ply(f"{out}/map_after_slot{k}.ply", p2, c2)
    rows.append(dict(k=k, t=float(ts[k]), dt=dt, bytes=len(chunk), budget=budget, use=len(chunk) / budget, enc_s=tenc, dec_s=tdec, pts=int(len(p2)), tx_s=len(chunk) / rate))
    print(f"slot {k}: {len(chunk):6d} B / {budget:6.0f} ({100*len(chunk)/budget:5.1f}%) enc {tenc:4.1f}s dec {tdec:4.1f}s pts {len(p2):,}", flush=True)
save_ply(f"{out}/final.ply", p2, c2); shutil.copy("houses_7fps.txt", f"{out}/final.txt")
open(f"{out}/final.splat", "w").write(f"{float(splat):.6g}\n")
json.dump(dict(streamer=os.path.basename(streamer), rate_kbit=RATE_KBIT, splat=float(splat), slots=rows, total_bytes=sum(r["bytes"] for r in rows)), open(f"{out}/wire/manifest.json", "w"), indent=1)
print("splat", splat, "total", sum(r["bytes"] for r in rows), "B")
