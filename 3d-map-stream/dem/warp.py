"""Ground-warp metric for a DEM: low-pass the DEM's local minima (ground), fit a plane, report the residual spread.
A flat campus should give ~0; values are in camera altitudes.  usage: python warp.py DEM_DIR [...]"""
import sys, json, numpy as np
from scipy import ndimage
for d in sys.argv[1:]:
    H = np.load(d + "/dem.npy"); m = json.load(open(d + "/meta.json"))
    cell = m["cell"]; k = max(3, int(round(0.05 * max(H.shape))))  # ~5% of the extent: wider than buildings
    Hf = np.where(np.isfinite(H), H, np.nanmax(H))
    ground = ndimage.grey_opening(Hf, size=(k, k))  # removes objects narrower than k cells
    ground = ndimage.uniform_filter(ground, k)
    ok = np.isfinite(H)
    rr, cc = np.nonzero(ok)
    A = np.c_[cc, rr, np.ones(len(rr))]; z = ground[rr, cc]
    coef = np.linalg.lstsq(A, z, rcond=None)[0]; res = z - A @ coef
    ndsm = H - ground
    print(f"{d}: ground warp p95-p5 {np.percentile(res,95)-np.percentile(res,5):.3f} alt, rms {res.std():.3f} alt | "
          f"objects >0.05 alt: {(ndsm[ok] > 0.05).mean()*100:.1f}% of cells | coverage {ok.mean()*100:.0f}%")
