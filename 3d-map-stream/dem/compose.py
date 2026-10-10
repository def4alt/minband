"""Comparison sheet: one row per DEM dir (ortho | relief | oblique), labelled.
usage: python compose.py OUT.png DIR[=label] [DIR[=label] ...]"""
import sys, os, json
from PIL import Image, ImageDraw, ImageFont

ROW_H, PAD, LABEL_H = 300, 8, 34
rows = []
for arg in sys.argv[2:]:
    d, _, label = arg.partition("=")
    meta = json.load(open(os.path.join(d, "meta.json")))
    label = label or os.path.basename(d.rstrip("/"))
    extra = f"  |  coverage {meta['coverage']*100:.0f}%"
    if "depth_ms_median" in meta:
        extra += f"  |  depth {meta['depth_ms_median']:.0f} ms/kf  |  kf spread {meta['median_frame_spread_alt']*100:.1f}% alt"
    ims = []
    for n in ("ortho.png", "relief.png", "oblique.png"):
        im = Image.open(os.path.join(d, n)).convert("RGB")
        ims.append(im.resize((round(im.width * ROW_H / im.height), ROW_H)))
    w = sum(i.width for i in ims) + PAD * (len(ims) + 1)
    row = Image.new("RGB", (w, ROW_H + LABEL_H + PAD), "white")
    ImageDraw.Draw(row).text((PAD, 8), label + extra, fill="black", font=ImageFont.load_default(18))
    x = PAD
    for im in ims:
        row.paste(im, (x, LABEL_H)); x += im.width + PAD
    rows.append(row)
W = max(r.width for r in rows)
out = Image.new("RGB", (W, sum(r.height for r in rows)), "white")
y = 0
for r in rows:
    out.paste(r, (0, y)); y += r.height
out.save(sys.argv[1])
print(sys.argv[1], out.size)
