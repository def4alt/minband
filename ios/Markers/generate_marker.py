#!/usr/bin/env python3
"""Generate the MinBand origin marker (ARKit reference image) at two resolutions.

  python3 ios/Markers/generate_marker.py     # needs Pillow; run from anywhere

Writes
  ios/MinBand/Assets.xcassets/Markers.arresourcegroup/minband-marker-a.arreferenceimage/minband-marker-a.png
      2000 x 1400 px, the image ARKit matches against (physical width 0.42 m in Contents.json)
  ios/Markers/minband-marker-a3.png
      4961 x 3473 px at 300 dpi = 420 x 294 mm, the same design for printing on A3 landscape.
  ios/MinBand/Assets.xcassets/AppIcon.appiconset/icon-1024.png
      the app icon (a crop of the same block mosaic).

Design: thick black frame, a seeded random 6x4 block grid where every block is itself a random
3x3 mosaic of four grey levels (many corners, no symmetry, broad histogram, which is what ARKit's
feature detector wants), a solid corner key so the image is never ambiguous under rotation, and a
label strip naming the axes. Deterministic: same seed -> same pixels.
"""
import os
import random

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
IOS = os.path.dirname(HERE)
ASSET_DIR = os.path.join(IOS, "MinBand", "Assets.xcassets", "Markers.arresourcegroup",
                         "minband-marker-a.arreferenceimage")
WIDTH_MM = 420.0
SEED = 20261009
GREYS = [0, 85, 170, 255]

FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def font(px):
    for f in FONT_CANDIDATES:
        if os.path.exists(f):
            return ImageFont.truetype(f, px)
    return ImageFont.load_default()


def render(width):
    s = width / 2000.0
    W, H = width, round(1400 * s)
    img = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(img)
    S = lambda v: round(v * s)

    # Frame.
    border, gap, label_h = 90, 50, 130
    d.rectangle([0, 0, W - 1, H - 1], fill=0)
    d.rectangle([S(border), S(border), W - 1 - S(border), H - 1 - S(border)], fill=255)

    # 6x4 grid of 3x3 mosaics.
    rng = random.Random(SEED)
    gx0, gy0 = border + gap, border + gap
    gx1, gy1 = 2000 - border - gap, 1400 - border - gap - label_h
    cols, rows, sub = 6, 4, 3
    cw, ch = (gx1 - gx0) / cols, (gy1 - gy0) / rows
    for r in range(rows):
        for c in range(cols):
            x0, y0 = gx0 + c * cw, gy0 + r * ch
            for i in range(sub):
                for j in range(sub):
                    g = rng.choice(GREYS)
                    d.rectangle([S(x0 + j * cw / sub), S(y0 + i * ch / sub),
                                 S(x0 + (j + 1) * cw / sub) - 1, S(y0 + (i + 1) * ch / sub) - 1], fill=g)
            # Thin white separators keep neighbouring blocks from merging.
            d.rectangle([S(x0), S(y0), S(x0 + cw) - 1, S(y0 + ch) - 1], outline=255, width=max(1, S(6)))

    # Orientation key: solid black square in the top-left grid corner, white dot inside.
    kw, kh = 2 * cw / sub, 2 * ch / sub
    d.rectangle([S(gx0), S(gy0), S(gx0 + kw) - 1, S(gy0 + kh) - 1], fill=0)
    r0 = 0.3 * min(kw, kh)
    d.ellipse([S(gx0 + kw / 2 - r0), S(gy0 + kh / 2 - r0), S(gx0 + kw / 2 + r0), S(gy0 + kh / 2 + r0)], fill=255)

    # Label strip.
    ly = gy1 + 25
    d.text((S(gx0), S(ly)), "MinBand", font=font(S(78)), fill=0)
    d.text((S(gx0 + 400), S(ly + 6)),
           "origin marker A   print width 420 mm (A3 landscape, 100%)", font=font(S(34)), fill=0)
    d.text((S(gx0 + 400), S(ly + 50)),
           "+X -> right    +Z -> toward this edge    +Y up out of the page", font=font(S(34)), fill=60)
    return img


def icon():
    m = render(2000).crop((140, 140, 140 + 860, 140 + 860)).resize((1024, 1024), Image.NEAREST)
    out = Image.new("RGB", (1024, 1024), (0, 0, 0))
    out.paste(m.convert("RGB"), (0, 0))
    d = ImageDraw.Draw(out)
    d.rectangle([0, 700, 1023, 1023], fill=(0, 0, 0))
    d.text((70, 730), "MinBand", font=font(200), fill=(80, 220, 120))
    return out


def main():
    os.makedirs(ASSET_DIR, exist_ok=True)
    ref = render(2000)
    ref_dpi = 2000 / (WIDTH_MM / 25.4)
    ref.save(os.path.join(ASSET_DIR, "minband-marker-a.png"), dpi=(ref_dpi, ref_dpi), optimize=True)
    big_w = round(WIDTH_MM / 25.4 * 300)
    render(big_w).save(os.path.join(HERE, "minband-marker-a3.png"), dpi=(300, 300), optimize=True)
    icon_dir = os.path.join(IOS, "MinBand", "Assets.xcassets", "AppIcon.appiconset")
    os.makedirs(icon_dir, exist_ok=True)
    icon().save(os.path.join(icon_dir, "icon-1024.png"), optimize=True)
    print("wrote", os.path.join(ASSET_DIR, "minband-marker-a.png"), ref.size)
    print("wrote", os.path.join(HERE, "minband-marker-a3.png"), (big_w, round(1400 * big_w / 2000)))


if __name__ == "__main__":
    main()
