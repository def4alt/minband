#!/usr/bin/env python3
"""Generate the MinBand origin marker (ARKit reference image) at two resolutions.

  python3 ios/Markers/generate_marker.py     # needs Pillow; run from anywhere

Writes
  ios/MinBand/Assets.xcassets/Markers.arresourcegroup/minband-marker-a.arreferenceimage/minband-marker-a.png
      2000 x 1400 px, the image ARKit matches against (physical width 0.42 m in Contents.json)
  ios/Markers/minband-marker-a3.png
      4961 x 3473 px at 300 dpi = 420 x 294 mm, the same design for printing on A3 landscape.
  ios/MinBand/Assets.xcassets/AppIcon.appiconset/icon-1024.png
      the app icon: a white hairline contour mountain on near-black (docs/STYLE.md).

Design: thick black frame, a seeded random 6x4 block grid where every block is itself a random
3x3 mosaic of four grey levels (many corners, no symmetry, broad histogram, which is what ARKit's
feature detector wants), a solid corner key so the image is never ambiguous under rotation, and a
label strip naming the axes. Deterministic: same seed -> same pixels.
"""
import math
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


ICON_BG = (0x07, 0x08, 0x09)    # docs/STYLE.md --bg
ICON_INK = (0xE9, 0xEC, 0xEF)   # --ink


def icon_height(x, z):
    """Terrain height (m) at ground (x right, z away), same relief as the app's standby screen
    (ios/MinBand/UI/ContourField.swift)."""
    def bump(cx, cz, h, sx, sz):
        dx, dz = (x - cx) / sx, (z - cz) / sz
        return h * math.exp(-(dx * dx + dz * dz))
    h = (bump(0.6, 11.5, 3.1, 1.9, 2.4) + bump(-0.9, 13.5, 1.8, 2.2, 2.0)
         + bump(-5.0, 15.0, 2.0, 2.8, 3.0) + bump(5.4, 14.0, 1.6, 2.4, 2.6) + bump(2.8, 8.0, 0.55, 1.4, 1.2))
    relief = 0.06 + 0.18 * min(1.0, h / 1.5)
    return h + relief * math.sin(1.9 * x + 0.7 * z) * math.cos(1.3 * z - 0.6 * x) + 0.04 * math.sin(3.7 * x - 2.3 * z)


def icon():
    """App icon: a wireframe contour mountain in white hairlines on near-black (docs/STYLE.md).
    Rows of constant depth drawn far to near, each filling below itself with the background so
    nearer ridges hide farther lines. Drawn 4x and downsampled for clean antialiased lines; the
    line width is chosen to land near 1 px on the 60 pt home-screen icon."""
    ss = 4
    n = 1024 * ss
    img = Image.new("RGB", (n, n), ICON_BG)
    d = ImageDraw.Draw(img)
    horizon, f, eye = n * 0.40, n * 0.95, 1.7
    z_near, z_far, rows, cols = 3.2, 24.0, 18, 160
    for r in range(rows):
        s = r / (rows - 1)
        z = 1 / (1 / z_far + (1 / z_near - 1 / z_far) * s)
        half = (n / 2 + 40) * z / f
        pts = []
        for c in range(cols + 1):
            x = -half + 2 * half * c / cols
            pts.append((n / 2 + x * f / z, horizon + (eye - icon_height(x, z)) * f / z))
        d.polygon(pts + [(n + 50, n + 50), (-50, n + 50)], fill=ICON_BG)
        a = 0.30 + 0.70 * s ** 0.8
        col = tuple(round(ICON_BG[i] + (ICON_INK[i] - ICON_BG[i]) * a) for i in range(3))
        d.line(pts, fill=col, width=round(5.5 * ss), joint="curve")
    return img.resize((1024, 1024), Image.LANCZOS)


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
