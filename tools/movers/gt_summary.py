"""Summarize a phone ground-truth log (`gt-<unix>.csv`, one row per track per 30 Hz tick) per track:
how long each id lived, how far it went and how fast, so a tracking problem shows up without the
viewer. The decisive sign of the id churn fixed in Oct 2026 was a hundred person tracks each living
1.4-2.0 s (two hits, then a 2 s coast to death) in a room with three people.

    adb pull /sdcard/Android/data/dev.minband.android/files/gt-1791622538.csv .
    python tools/movers/gt_summary.py gt-1791622538.csv [--min-life 0]

Columns: id, class, life (s), net displacement (m), largest displacement from the first position,
largest displacement inside any 2 s window (what the MOVING rule looks at), median and maximum
reported speed, rows. Ticks are 120 per second (core TICK_HZ).
"""
from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import defaultdict

TICKS_PER_SECOND = 120.0
WINDOW_S = 2.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("csv")
    ap.add_argument("--min-life", type=float, default=0.0, help="hide tracks shorter than this (s)")
    args = ap.parse_args()

    with open(args.csv, newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print("empty log", file=sys.stderr)
        return 1
    by: dict[int, list] = defaultdict(list)
    for r in rows:
        by[int(r["id"])].append((int(r["tick"]) / TICKS_PER_SECOND, float(r["x"]), float(r["z"]),
                                 float(r["vx"]), float(r["vz"]), int(r["class"])))
    span = (int(rows[-1]["tick"]) - int(rows[0]["tick"])) / TICKS_PER_SECOND
    lives = []
    print(f"{len(rows)} rows, {len(by)} tracks, {span:.1f} s")
    print(" id cls  life_s  net_m  maxdisp_m  max2s_m  medspeed  maxspeed  rows")
    for i, h in sorted(by.items()):
        h.sort()
        life = h[-1][0] - h[0][0]
        lives.append(life)
        if life < args.min_life:
            continue

        def d(a, b) -> float:
            return math.hypot(a[1] - b[1], a[2] - b[2])

        net = d(h[-1], h[0])
        maxd = max(d(p, h[0]) for p in h)
        m2 = 0.0
        j = 0
        for k, p in enumerate(h):
            while h[k][0] - h[j][0] > WINDOW_S:
                j += 1
            m2 = max(m2, d(p, h[j]))
        sp = sorted(math.hypot(p[3], p[4]) for p in h)
        print(f"{i:3d} {h[0][5]:3d} {life:7.1f} {net:6.2f} {maxd:9.2f} {m2:8.2f} {sp[len(sp) // 2]:9.2f} {sp[-1]:9.2f} {len(h):5d}")
    lives.sort()
    print(f"median track life {lives[len(lives) // 2]:.1f} s; {sum(1 for l in lives if l < 2.5)} of {len(lives)} tracks lived under 2.5 s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
