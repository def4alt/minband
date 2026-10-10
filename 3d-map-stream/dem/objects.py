"""Object heights in a DEM: nDSM = DEM - ground (grey opening), connected components above a threshold.
usage: python objects.py DIR:metres_per_height_unit [...]   (use 1 for DEMs already in metres)
Prints the largest objects (area, p90 height) so the same building can be compared across pipelines."""
import sys, json, numpy as np
from scipy import ndimage

for arg in sys.argv[1:]:
    d, _, s = arg.rpartition(":"); s = float(s)
    H = np.load(d + "/dem.npy") * s; meta = json.load(open(d + "/meta.json"))
    cell = meta["cell"] * (s if meta.get("units") != "metres" else 1.0)  # grid units -> metres
    k = max(3, int(round(40.0 / cell)))  # ground = opening wider than a 40 m building
    Hf = np.where(np.isfinite(H), H, np.nanmax(H))
    ground = ndimage.uniform_filter(ndimage.grey_opening(Hf, size=(k, k)), max(3, k // 2))
    nd = np.where(np.isfinite(H), H - ground, 0)
    lab, n = ndimage.label(nd > 3.0)
    objs = []
    for i in range(1, n + 1):
        mm = lab == i
        area = mm.sum() * cell * cell
        if area > 150:
            objs.append((area, np.percentile(nd[mm], 90)))
    objs.sort(reverse=True)
    print(f"{d}: cell {cell:.2f} m | " + "  ".join(f"[{a:.0f} m2, {h:.1f} m]" for a, h in objs[:5]))
