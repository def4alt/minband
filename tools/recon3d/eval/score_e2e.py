"""End-to-end score: what reached the operator, against an eye-made inventory, in the drone's own image.

    python tools/recon3d/eval/score_e2e.py RUN_DIR DUMP.jsonl GT.json [--movers MOVERS.json] [--json out.json] [--misses out.json]

DUMP.jsonl is the driver's batch replay (DUMP=... npm start): one line per 0.1 s step with what the edge tracked
(`edge.tracks`, metres) and what the receiver holds (`rx.contacts`, decoded from the wire, dead-reckoned) plus the
pose the receiver last decoded (`rx.pose`). At every inventory frame both are projected into that frame:

  edge  - the edge's 3D tracks through the edge's own camera (cameras.csv at that tick): losses in detection,
          lifting and tracking;
  rx    - the receiver's contacts through the pose the receiver holds (what the operator's simulation draws from
          the drone's point of view): adds the link, the contact layer (grouping, revision bands, dead reckoning).

A projected point matches an inventory box when it falls inside the box grown by max(6 px, 30 % of its long side);
singles match one-to-one (Hungarian on the distance to the box centre); a group contact then claims up to `count`
of the remaining boxes whose centres fall inside its ring projected at its range (radius + 1.5 m). Recall is
reported for singles alone and with groups, overall, for movers and by size (long side of the box). Contacts in
view that match nothing and are not in an ignore box are listed as unmatched (false, or an object the inventory
missed: look at them). Position: for matched singles, the pixel distance from the projected point to the box
centre and that distance at the object's range in metres.
"""
import argparse
import csv
import json
import math
import pathlib

import numpy as np
from scipy.optimize import linear_sum_assignment

TICK_HZ = 120
SIZES = [(0, 20, "<20px"), (20, 40, "20-40"), (40, 80, "40-80"), (80, 1e9, ">=80")]


def basis(yaw, pitch, roll):
    """Camera axes in ENU from lift.py's yaw/pitch/roll (yaw from north, pitch up, roll about the optical axis)."""
    y, p, r = map(math.radians, (yaw, pitch, roll))
    fwd = np.array([math.sin(y) * math.cos(p), math.cos(y) * math.cos(p), math.sin(p)])
    right0 = np.array([math.cos(y), -math.sin(y), 0.0])
    up0 = np.cross(right0, fwd)
    right = right0 * math.cos(r) - up0 * math.sin(r)
    down = np.cross(fwd, right)
    return fwd, right, down


def project(pts, cam, f, W, H):
    """ENU points (N,3) -> pixel (N,2), depth (N,); behind the camera -> depth <= 0."""
    fwd, right, down = basis(cam["yaw"], cam["pitch"], cam.get("roll", 0.0))
    d = np.asarray(pts, float) - np.array([cam["e"], cam["n"], cam["u"]])
    z = d @ fwd
    zz = np.where(z > 1e-3, z, 1e-3)
    return np.stack([W / 2 + f * (d @ right) / zz, H / 2 + f * (d @ down) / zz], 1), z


def inside(p, b, grow):
    return b[0] - grow <= p[0] <= b[2] + grow and b[1] - grow <= p[1] <= b[3] + grow


def match(objs, singles, groups, f):
    """objs: list of {box}; singles: [(id, px, depth)]; groups: [(id, px, depth, radius_m, count)].
    Returns {obj index: (kind, contact id, px error)}."""
    out = {}
    if objs and singles:
        cost = np.full((len(objs), len(singles)), 1e9)
        for i, o in enumerate(objs):
            b = o["box"]; c = ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2); g = max(6.0, 0.3 * max(b[2] - b[0], b[3] - b[1]))
            for j, (_, p, _) in enumerate(singles):
                if inside(p, b, g):
                    cost[i, j] = math.hypot(p[0] - c[0], p[1] - c[1])
        r, c = linear_sum_assignment(cost)
        for i, j in zip(r, c):
            if cost[i, j] < 1e9:
                out[i] = ("single", singles[j][0], cost[i, j], singles[j][2])
    for gid, p, z, rad, cnt in groups:
        rp = f * (rad + 1.5) / max(z, 1e-3)
        cand = []
        for i, o in enumerate(objs):
            if i in out:
                continue
            b = o["box"]; c = ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
            dd = math.hypot(p[0] - c[0], p[1] - c[1])
            if dd <= rp + max(6.0, 0.3 * max(b[2] - b[0], b[3] - b[1])):
                cand.append((dd, i))
        for dd, i in sorted(cand)[:cnt]:
            out[i] = ("group", gid, dd, z)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run"); ap.add_argument("dump"); ap.add_argument("gt")
    ap.add_argument("--movers", help="movers.json: mover sightings every 0.5 s (scored at every sighting)")
    ap.add_argument("--json"); ap.add_argument("--misses", help="write the missed inventory objects and unmatched contacts here")
    ap.add_argument("--no-hard", action="store_true", help="leave objects marked hard out of the inventory")
    a = ap.parse_args()
    run = pathlib.Path(a.run)
    summ = json.loads((run / "scene/summary.json").read_text())
    W, H, f = summ["width"], summ["height"], summ["f_px"]
    cams = {}
    with open(run / "scene/cameras.csv") as fh:
        for r in csv.DictReader(fh):
            cams[int(r["tick"])] = {k: float(r[k]) for k in ("e", "n", "u", "yaw", "pitch", "roll")}
    steps = {}
    for line in open(a.dump):
        s = json.loads(line)
        steps[round(s["t"] * 10)] = s
    gt = json.loads(pathlib.Path(a.gt).read_text())

    # Evaluation points: every inventory frame (all objects), and every mover sighting (movers only).
    points = []
    for fr in gt["frames"]:
        objs = [o for o in fr["objects"] if not (a.no_hard and o.get("hard"))]
        points.append({"frame": fr["frame"], "objs": objs, "ignore": fr.get("ignore", []), "kind": "inventory"})
    if a.movers:
        mv = json.loads(pathlib.Path(a.movers).read_text())
        byf = {}
        for m in mv["movers"]:
            for s in m["sightings"]:
                if a.no_hard and s.get("hard"):
                    continue
                byf.setdefault(s["frame"], []).append({"box": s["box"], "cls": m["cls"], "moving": True, "mover": m["id"]})
        for fr, objs in sorted(byf.items()):
            points.append({"frame": fr, "objs": objs, "ignore": [], "kind": "movers"})

    res = {"edge": [], "rx": []}
    unmatched = []
    for P in points:
        k10 = round(P["frame"] / 30 * 10)
        s = steps.get(k10)
        if s is None:
            continue
        tick = P["frame"] * TICK_HZ // 30
        ecam = cams.get(tick) or cams[min(cams, key=lambda t: abs(t - tick))]
        for level in ("edge", "rx"):
            if level == "edge":
                cam = ecam
                items = [(x["id"], (x["e"], x["n"], x["u"]), 1, 0.0, False) for x in s["edge"]["tracks"]]
            else:
                cam = s["rx"]["pose"]
                if cam is None:
                    continue
                items = [(c["id"], (c["e"], c["n"], c["u"] if c["u"] is not None else 0.75), c["count"], c["radius"], c["group"]) for c in s["rx"]["contacts"]]
            if not items:
                m = {}
                singles, groups = [], []
            else:
                px, z = project([it[1] for it in items], cam, f, W, H)
                singles = [(it[0], px[j], z[j]) for j, it in enumerate(items) if not it[4] and z[j] > 0]
                groups = [(it[0], px[j], z[j], it[3], it[2]) for j, it in enumerate(items) if it[4] and z[j] > 0]
                m = match(P["objs"], singles, groups, f)
            for i, o in enumerate(P["objs"]):
                b = o["box"]; L = max(b[2] - b[0], b[3] - b[1])
                hit = m.get(i)
                res[level].append({"frame": P["frame"], "kind": P["kind"], "cls": o.get("cls"), "moving": bool(o.get("moving")), "hard": bool(o.get("hard")),
                                   "size": L, "box": b, "hit": hit[0] if hit else None, "err_px": hit[2] if hit else None,
                                   "err_m": (hit[2] * hit[3] / f) if hit else None, "mover": o.get("mover")})
            if P["kind"] == "inventory" and items:
                used = {v[1] for v in m.values()}
                for cid, p, z in singles:
                    if cid in used or not (0 <= p[0] < W and 0 <= p[1] < H):
                        continue
                    if any(inside(p, ib["box"] if isinstance(ib, dict) else ib, 4) for ib in P["ignore"]):
                        continue
                    unmatched.append({"level": level, "frame": P["frame"], "id": cid, "px": [round(float(p[0]), 1), round(float(p[1]), 1)], "depth_m": round(float(z), 1)})

    def rate(rows):
        n = len(rows)
        s1 = sum(r["hit"] == "single" for r in rows)
        sg = sum(r["hit"] is not None for r in rows)
        return n, s1, sg

    report = {}
    print(f"{'level':5} {'set':22} {'n':>5} {'single':>8} {'+group':>8}   median err px / m (singles)")
    for level in ("edge", "rx"):
        R = res[level]
        sets = {"inventory: all": [r for r in R if r["kind"] == "inventory"],
                "inventory: movers": [r for r in R if r["kind"] == "inventory" and r["moving"]],
                "inventory: not hard": [r for r in R if r["kind"] == "inventory" and not r["hard"]],
                "mover sightings": [r for r in R if r["kind"] == "movers"]}
        for lo, hi, name in SIZES:
            sets[f"inventory: {name}"] = [r for r in R if r["kind"] == "inventory" and lo <= r["size"] < hi]
        report[level] = {}
        for name, rows in sets.items():
            n, s1, sg = rate(rows)
            errs = [r["err_px"] for r in rows if r["hit"] == "single"]
            errm = [r["err_m"] for r in rows if r["hit"] == "single"]
            report[level][name] = {"n": n, "single": s1, "with_groups": sg, "recall_single": s1 / n if n else None, "recall": sg / n if n else None,
                                   "err_px_median": float(np.median(errs)) if errs else None, "err_m_median": float(np.median(errm)) if errm else None}
            if n:
                print(f"{level:5} {name:22} {n:5d} {100 * s1 / n:7.1f}% {100 * sg / n:7.1f}%   "
                      f"{np.median(errs) if errs else float('nan'):.1f} / {np.median(errm) if errm else float('nan'):.2f}")
        movers = sorted({r["mover"] for r in R if r["kind"] == "movers"})
        if movers:
            per = {mid: rate([r for r in R if r["kind"] == "movers" and r["mover"] == mid]) for mid in movers}
            print(f"{level:5} per mover (sightings hit/n): " + ", ".join(f"{k} {v[2]}/{v[0]}" for k, v in per.items()))
            report[level]["per_mover"] = {k: {"n": v[0], "single": v[1], "with_groups": v[2]} for k, v in per.items()}
        um = [u for u in unmatched if u["level"] == level]
        print(f"{level:5} unmatched in-view singles at inventory frames: {len(um)}")
        report[level]["unmatched"] = len(um)
    if a.json:
        pathlib.Path(a.json).write_text(json.dumps(report, indent=1))
    if a.misses:
        pathlib.Path(a.misses).write_text(json.dumps({"missed": {lv: [r for r in res[lv] if r["hit"] is None] for lv in res}, "unmatched": unmatched}, indent=1))


if __name__ == "__main__":
    main()
