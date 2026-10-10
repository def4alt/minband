"""Frame-level scoring of detections.json files against a hand-annotated ground-truth inventory.

    python tools/recon3d/eval/score_det.py GT.json DET.json [DET2.json ...] [--conf 0.1,0.2,0.3,0.4]
        [--json out.json] [--per-frame] [--list] [--max-ratio 10]
    python tools/recon3d/eval/score_det.py --selftest

GT format (frames are video frame indices, boxes source pixels):
    {"clip": "...", "width": 1920, "height": 1080, "fps": 30,
     "frames": [{"frame": 75, "t": 2.5,
                 "objects": [{"box": [x1, y1, x2, y2], "cls": "car|van|truck|bus|person|bicycle|motorcycle",
                              "moving": true|false|null, "tag": "free text"}],
                 "ignore": [[x1, y1, x2, y2], ...]}]}

Only the GT frames are scored; a detections file may hold more. A detection is "on" a GT object when its
IoU >= 0.1, or it contains the GT centre, or the GT box contains its centre (small boxes whose IoU is
noisy); the containment tests need the two areas within --max-ratio of each other (default 10, the ratio
IoU 0.1 itself allows), so one huge box over a parking lot is not "on" every car in it. Matching is
one-to-one, greedy by IoU (ties by centre distance); any class counts. Per detection file and confidence
floor:

    found      GT objects with a matched detection; recall = found / GT
    moving     the same for objects with moving == true
    size       the same by the GT box's long side: <20, 20-40, 40-80, >=80 px
    cls ok     matched detections whose class agrees with the GT (car/van -> car), of the found
    false      unmatched detections that are on no GT object and not centred in an ignore box
    dup        unmatched detections on a GT object that another detection already has
    ign        unmatched detections centred in an ignore box and on no GT object (not counted as false)

--per-frame prints the breakdown per frame (first floor), --list the missed GT objects and the false
detections of every floor. --json writes everything, including both lists, for further tooling.
"""
import argparse
import json
import pathlib
import sys

import numpy as np

COCO_OF = {"person": 0, "bicycle": 1, "car": 2, "van": 2, "motorcycle": 3, "bus": 5, "truck": 7}
BUCKETS = [("<20", 0, 20), ("20-40", 20, 40), ("40-80", 40, 80), (">=80", 80, 1e9)]


def areas(b):
    return np.clip(b[:, 2] - b[:, 0], 0, None) * np.clip(b[:, 3] - b[:, 1], 0, None)


def match(dbox, gbox, max_ratio=10.0):
    """dbox (D, 4), gbox (G, 4) -> (det of each GT or -1, GT of each det or -1, on (D, G) bool)."""
    D, G = len(dbox), len(gbox)
    if D == 0 or G == 0:
        return -np.ones(G, int), -np.ones(D, int), np.zeros((D, G), bool)
    x1 = np.maximum(dbox[:, None, 0], gbox[None, :, 0]); y1 = np.maximum(dbox[:, None, 1], gbox[None, :, 1])
    x2 = np.minimum(dbox[:, None, 2], gbox[None, :, 2]); y2 = np.minimum(dbox[:, None, 3], gbox[None, :, 3])
    inter = np.clip(x2 - x1, 0, None) * np.clip(y2 - y1, 0, None)
    da, ga = areas(dbox), areas(gbox)
    iou = inter / np.maximum(da[:, None] + ga[None, :] - inter, 1e-9)
    dc = np.stack([(dbox[:, 0] + dbox[:, 2]) / 2, (dbox[:, 1] + dbox[:, 3]) / 2], 1)
    gc = np.stack([(gbox[:, 0] + gbox[:, 2]) / 2, (gbox[:, 1] + gbox[:, 3]) / 2], 1)
    d_has_g = (gc[None, :, 0] >= dbox[:, None, 0]) & (gc[None, :, 0] <= dbox[:, None, 2]) & (gc[None, :, 1] >= dbox[:, None, 1]) & (gc[None, :, 1] <= dbox[:, None, 3])
    g_has_d = (dc[:, None, 0] >= gbox[None, :, 0]) & (dc[:, None, 0] <= gbox[None, :, 2]) & (dc[:, None, 1] >= gbox[None, :, 1]) & (dc[:, None, 1] <= gbox[None, :, 3])
    ratio = np.maximum(da[:, None], ga[None, :]) / np.maximum(np.minimum(da[:, None], ga[None, :]), 1e-9)
    on = (iou >= 0.1) | ((d_has_g | g_has_d) & (ratio <= max_ratio))
    dist = np.hypot(dc[:, None, 0] - gc[None, :, 0], dc[:, None, 1] - gc[None, :, 1])
    pairs = sorted(((iou[d, g], -dist[d, g], d, g) for d, g in zip(*np.nonzero(on))), reverse=True)
    g_of_d, d_of_g = -np.ones(D, int), -np.ones(G, int)
    for _, _, d, g in pairs:
        if g_of_d[d] < 0 and d_of_g[g] < 0:
            g_of_d[d], d_of_g[g] = g, d
    return d_of_g, g_of_d, on


def in_any(pt, boxes):
    return any(b[0] <= pt[0] <= b[2] and b[1] <= pt[1] <= b[3] for b in boxes)


def bucket_of(box):
    side = max(box[2] - box[0], box[3] - box[1])
    return next(n for n, lo, hi in BUCKETS if lo <= side < hi)


def score_frame(gtf, dets, conf, max_ratio=10.0):
    """One frame at one floor -> counts and the missed / false lists."""
    objs = gtf["objects"]
    gbox = np.array([o["box"] for o in objs], float).reshape(-1, 4)
    keep = [d for d in dets if d["conf"] >= conf]
    dbox = np.array([d["box"] for d in keep], float).reshape(-1, 4)
    d_of_g, g_of_d, on = match(dbox, gbox, max_ratio)
    ign = gtf.get("ignore") or []
    res = {"frame": gtf["frame"], "gt": len(objs), "dets": len(keep), "found": 0, "false": 0, "dup": 0, "ign": 0, "cls_ok": 0,
           "moving": [0, 0], "size": {n: [0, 0] for n, _, _ in BUCKETS}, "missed": [], "false_dets": []}
    for g, o in enumerate(objs):
        hit = int(d_of_g[g] >= 0)
        res["found"] += hit
        b = bucket_of(o["box"])
        res["size"][b][1] += 1
        res["size"][b][0] += hit
        if o.get("moving") is True:
            res["moving"][1] += 1
            res["moving"][0] += hit
        if hit:
            res["cls_ok"] += int(keep[d_of_g[g]]["cls"] == COCO_OF.get(o["cls"], -2))
        else:
            res["missed"].append({"frame": gtf["frame"], "box": o["box"], "cls": o["cls"], "moving": o.get("moving"), "tag": o.get("tag", "")})
    for d, det in enumerate(keep):
        if g_of_d[d] >= 0:
            continue
        cx, cy = (det["box"][0] + det["box"][2]) / 2, (det["box"][1] + det["box"][3]) / 2
        if on[d].any():
            res["dup"] += 1
        elif in_any((cx, cy), ign):
            res["ign"] += 1
        else:
            res["false"] += 1
            res["false_dets"].append({"frame": gtf["frame"], "box": det["box"], "cls": det["cls"], "conf": det["conf"]})
    return res


def score_file(gt, det, floors, max_ratio=10.0):
    by_frame = {f["i"]: f["dets"] for f in det["frames"]}
    out = {}
    for conf in floors:
        per, skipped = [], []
        for gtf in gt["frames"]:
            if gtf["frame"] not in by_frame:
                skipped.append(gtf["frame"])
                continue
            per.append(score_frame(gtf, by_frame[gtf["frame"]], conf, max_ratio))
        tot = {"gt": sum(p["gt"] for p in per), "found": sum(p["found"] for p in per), "dets": sum(p["dets"] for p in per),
               "false": sum(p["false"] for p in per), "dup": sum(p["dup"] for p in per), "ign": sum(p["ign"] for p in per),
               "cls_ok": sum(p["cls_ok"] for p in per), "frames": len(per), "frames_missing": skipped,
               "moving": [sum(p["moving"][0] for p in per), sum(p["moving"][1] for p in per)],
               "size": {n: [sum(p["size"][n][0] for p in per), sum(p["size"][n][1] for p in per)] for n, _, _ in BUCKETS}}
        tot["recall"] = tot["found"] / tot["gt"] if tot["gt"] else None
        tot["per_frame"] = per
        tot["missed"] = [m for p in per for m in p["missed"]]
        tot["false_dets"] = [m for p in per for m in p["false_dets"]]
        out[f"{conf:g}"] = tot
    return out


def frac(a):
    return f"{a[0]}/{a[1]}" if a[1] else "-"


def table(name, det, res):
    tile = f"tile {det['tile']} x{det.get('tile_scale')}" if det.get("tile") else "no tiles"
    print(f"\n{name}  (model {det.get('model')}, imgsz {det.get('imgsz')}, {tile}, {det.get('s_per_frame')} s/frame)")
    head = ["conf", "frames", "boxes/f", "found", "recall", "moving", *[f"{n} px" for n, _, _ in BUCKETS], "cls ok", "false", "dup", "ign"]
    rows = []
    for c, r in res.items():
        nf = max(1, r["frames"])
        rows.append([c, str(r["frames"]), f"{r['dets'] / nf:.1f}", frac([r["found"], r["gt"]]),
                     f"{100 * r['recall']:.0f}%" if r["recall"] is not None else "-", frac(r["moving"]),
                     *[frac(r["size"][n]) for n, _, _ in BUCKETS], frac([r["cls_ok"], r["found"]]), str(r["false"]), str(r["dup"]), str(r["ign"])])
    w = [max(len(h), *(len(r[k]) for r in rows)) for k, h in enumerate(head)]
    print("  ".join(h.rjust(w[k]) for k, h in enumerate(head)))
    for r in rows:
        print("  ".join(v.rjust(w[k]) for k, v in enumerate(r)))
    for c, r in res.items():
        if r["frames_missing"]:
            print(f"  conf {c}: GT frames not in the detections file: {r['frames_missing']}")


def per_frame_table(res):
    c = next(iter(res))
    print(f"\nper frame at conf {c}: found/GT  false  dup  boxes")
    for p in res[c]["per_frame"]:
        print(f"  frame {p['frame']:5d}: {p['found']:3d}/{p['gt']:<3d}  false {p['false']:3d}  dup {p['dup']:3d}  boxes {p['dets']:4d}")


def lists(res):
    for c, r in res.items():
        print(f"\nconf {c}: missed GT ({len(r['missed'])})")
        for m in r["missed"]:
            print(f"  frame {m['frame']:5d}  {m['cls']:<10s} {[round(v) for v in m['box']]}  moving={m['moving']}  {m['tag']}")
        print(f"conf {c}: false detections ({len(r['false_dets'])})")
        for m in r["false_dets"]:
            print(f"  frame {m['frame']:5d}  cls {m['cls']} conf {m['conf']:.2f} {[round(v) for v in m['box']]}")


def selftest():
    gt = {"frames": [{"frame": 0, "objects": [
        {"box": [100, 100, 140, 120], "cls": "car", "moving": True},      # found by a good box
        {"box": [300, 300, 315, 310], "cls": "car", "moving": False},     # found by a loose box that holds its centre
        {"box": [500, 500, 540, 520], "cls": "truck", "moving": None},    # missed
        {"box": [700, 100, 720, 130], "cls": "person", "moving": None}],  # found by an offset box (IoU 0.1)
        "ignore": [[900, 900, 1000, 1000]]}]}
    dets = [{"i": 0, "dets": [
        {"id": -1, "cls": 2, "conf": 0.9, "box": [102, 100, 142, 120]},
        {"id": -1, "cls": 2, "conf": 0.8, "box": [98, 100, 138, 121]},    # duplicate of the first
        {"id": -1, "cls": 2, "conf": 0.7, "box": [296, 296, 322, 316]},   # contains the small car's centre
        {"id": -1, "cls": 7, "conf": 0.2, "box": [0, 0, 1000, 1000]},     # huge box: not "on" anything, and false
        {"id": -1, "cls": 2, "conf": 0.6, "box": [920, 920, 950, 940]},   # in the ignore box
        {"id": -1, "cls": 0, "conf": 0.5, "box": [200, 700, 215, 730]},   # false
        {"id": -1, "cls": 0, "conf": 0.5, "box": [710, 100, 730, 130]}]}]
    r = score_file(gt, {"frames": dets}, [0.1, 0.5])
    a, b = r["0.1"], r["0.5"]
    assert (a["found"], a["gt"], a["dup"], a["false"], a["ign"], a["moving"], a["cls_ok"]) == (3, 4, 1, 2, 1, [1, 1], 3), a
    assert a["size"]["<20"] == [1, 1] and a["size"]["20-40"] == [1, 1] and a["size"]["40-80"] == [1, 2], a["size"]
    assert (b["found"], b["dup"], b["false"], b["ign"]) == (3, 1, 1, 1), b
    assert a["missed"][0]["cls"] == "truck"
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("gt", nargs="?")
    ap.add_argument("det", nargs="*")
    ap.add_argument("--conf", default="0.3", help="confidence floor(s), comma list")
    ap.add_argument("--json", default=None, help="write all results here")
    ap.add_argument("--per-frame", action="store_true")
    ap.add_argument("--list", action="store_true", help="list missed GT objects and false detections")
    ap.add_argument("--max-ratio", type=float, default=10.0, help="max area ratio for the containment tests")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.gt or not a.det:
        ap.error("GT.json and at least one DET.json are required")
    gt = json.loads(pathlib.Path(a.gt).read_text())
    floors = [float(c) for c in a.conf.split(",")]
    print(f"GT {a.gt}: {len(gt['frames'])} frames, {sum(len(f['objects']) for f in gt['frames'])} objects")
    results = []
    for path in a.det:
        det = json.loads(pathlib.Path(path).read_text())
        res = score_file(gt, det, floors, a.max_ratio)
        table(path, det, res)
        if a.per_frame:
            per_frame_table(res)
        if a.list:
            lists(res)
        results.append({"det": path, "model": det.get("model"), "imgsz": det.get("imgsz"), "tile": det.get("tile"), "tile_scale": det.get("tile_scale"),
                        "s_per_frame": det.get("s_per_frame"), "conf": res})
    if a.json:
        pathlib.Path(a.json).write_text(json.dumps({"gt": a.gt, "floors": floors, "results": results}, indent=1))
        print(f"\nwrote {a.json}")


if __name__ == "__main__":
    sys.exit(main())
