"""Benchmark harness. A codec module must define:
    encode(vox int32[N,3], rgb uint8[N,3]) -> bytes
    decode(bytes) -> (vox int32[M,3], rgb uint8[M,3])
Rules: decoded voxel SET must equal input exactly (any order); at most 16 distinct
decoded colours (4-bit). Reports bytes, bits/voxel, colour error, encode/decode time.
usage: python3 verify.py codec_file.py
"""
import sys, time, importlib.util, numpy as np
d = np.load("input_0.5m.npz"); vox, rgb = d["vox"], d["rgb"]
spec = importlib.util.spec_from_file_location("codec", sys.argv[1]); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
t0 = time.time(); blob = m.encode(vox.copy(), rgb.copy()); t1 = time.time()
v2, c2 = m.decode(blob); t2 = time.time()
v2, c2 = np.asarray(v2, np.int64), np.asarray(c2, np.uint8)
def keyof(v): return (v[:, 0] << 42) | (v[:, 1] << 21) | v[:, 2]
k1, k2 = keyof(vox.astype(np.int64)), keyof(v2)
o1, o2 = np.argsort(k1), np.argsort(k2)
ok = len(k1) == len(k2) and np.array_equal(k1[o1], k2[o2])
ncol = len(np.unique(c2, axis=0))
err = np.sqrt(((rgb[o1].astype(float) - c2[o2].astype(float)) ** 2).sum(1)).mean() if ok else float("nan")
print(f"{sys.argv[1]}: {'OK' if ok else 'GEOMETRY MISMATCH'} | {len(blob):,} B = {len(blob)/1e3:.1f} KB | "
      f"{8*len(blob)/len(vox):.3f} bits/voxel | colours={ncol}{' (TOO MANY)' if ncol > 16 else ''} | "
      f"mean RGB err {err:.1f} | enc {t1-t0:.2f}s dec {t2-t1:.2f}s")
