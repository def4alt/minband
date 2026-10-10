"""Near-field sharpness metric for a live_eval views dir.

Each PNG in the views dir is the reference render stacked on top of the candidate render
(np.vstack([ref, cand]), each H x W).  This script compares only the LOWER 50 % of rows of each
half: the ground close to the drone, where sharpness is visible to the operator.
PSNR/SSIM are computed exactly as live_eval.score does (quality.half() then MSE / quality.ssim),
plus a sky-colour hole proxy (reference surface pixel that is sky in the candidate).
usage: python nearfield.py VIEWS_DIR [VIEWS_DIR ...] [--pattern live|final|all]
Prints per-dir means for live*.png, final*.png and all, as one line each."""
import sys, os, glob, argparse, numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import quality as Q

def split_lower(img):
    """Stacked (2H x W x 3) PNG -> lower 50 % of rows of the reference and of the candidate."""
    H = img.shape[0] // 2; ref, cand = img[:H], img[H:]
    return ref[H // 2:], cand[H // 2:]

def near_scores(path):
    from PIL import Image
    img = np.asarray(Image.open(path).convert("RGB")); r, c = split_lower(img)
    hr, hc = Q.half(r), Q.half(c); mse = ((hr - hc) ** 2).mean()
    psnr = 10 * np.log10(255 ** 2 / max(mse, 1e-9)); ss = Q.ssim(hr, hc)
    sky = lambda im: np.all(im == Q.SKY, 2)
    rf, cf = ~sky(r), ~sky(c); holes = (rf & ~cf).sum() / max(rf.sum(), 1)
    return psnr, ss, holes

def near_field(views_dir, pattern="all"):
    """Return dict name -> (mean psnr, mean ssim, mean holes, n) for 'live', 'final', 'all'."""
    out = {}
    groups = {"live": "live*.png", "final": "final*.png"}
    res = {g: [near_scores(f) for f in sorted(glob.glob(os.path.join(views_dir, pat)))] for g, pat in groups.items()}
    res["all"] = res["live"] + res["final"]
    for g, v in res.items():
        if (pattern in ("all", g) or g == "all") and v:
            m = np.mean(v, 0); out[g] = (m[0], m[1], m[2], len(v))
    return out

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("views", nargs="+"); ap.add_argument("--pattern", default="all", choices=["live", "final", "all"]); a = ap.parse_args()
    for d in a.views:
        r = near_field(d, a.pattern)
        if not r: print(f"{d}: no live*/final*.png found"); continue
        parts = [f"{g.upper()} n={n} PSNR {p:.2f} SSIM {s:.3f} holes {100*h:.1f}%" for g, (p, s, h, n) in r.items()]
        print(f"NEARFIELD {os.path.basename(d.rstrip('/'))}: " + " | ".join(parts))

if __name__ == "__main__":
    main()
