"""Merge the eye audit into one inventory: candidate verdicts + objects the reader added + mover flags.

    python tools/recon3d/eval/merge_gt.py CANDIDATES.json ANN_DIR MOVERS.json --clip CLIP --out gt.json

CANDIDATES.json: the detector union the reader was shown ({frame: [{n, box, ...}]}); ANN_DIR/f<frame>.json: the
reader's verdict per candidate ("car", "false", "dup:<m>", "part:<m>", optionally a corrected box and "hard"),
the objects no candidate covered ("missed") and ignore regions; MOVERS.json: the mover inventory (sightings every
0.5 s, verified on motion-compensated strips). An inventory object is a mover when a mover sighting at that frame
overlaps it (IoU >= 0.2 or either centre inside the other box); a mover sighting with no inventory object is
added (the reader missed it) and counted in the merge log.
"""
import argparse
import json
import pathlib

CLASSES = {"car", "van", "truck", "bus", "person", "bicycle", "motorcycle", "other"}


def iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0])); iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    i = ix * iy; u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - i
    return i / u if u > 0 else 0


def centre_in(a, b):
    cx, cy = (a[0] + a[2]) / 2, (a[1] + a[3]) / 2
    return b[0] <= cx <= b[2] and b[1] <= cy <= b[3]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("candidates"); ap.add_argument("ann"); ap.add_argument("movers")
    ap.add_argument("--clip", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--frames", help="only these frames (comma list), e.g. while the audit is in progress")
    a = ap.parse_args()
    cands = json.loads(pathlib.Path(a.candidates).read_text())
    movers = json.loads(pathlib.Path(a.movers).read_text())["movers"] if a.movers != "-" else []
    frames, log = [], {}
    for f in sorted(cands, key=int):
        if a.frames and f not in a.frames.split(","):
            continue
        ann = json.loads((pathlib.Path(a.ann) / f"f{f}.json").read_text())
        byn = {c["n"]: c for c in cands[f]}
        missing = [n for n in byn if str(n) not in ann["candidates"]]
        if missing:
            raise SystemExit(f"frame {f}: no verdict for candidates {missing}")
        objs, bad = [], []
        for n, v in ann["candidates"].items():
            cls = v["v"]
            if cls in CLASSES:
                objs.append({"box": v.get("box") or byn[int(n)]["box"], "cls": cls, "hard": bool(v.get("hard")), "tag": f"cand {n}"})
            elif not (cls == "false" or cls.startswith("dup:") or cls.startswith("part:") or cls == "unsure"):
                bad.append((n, cls))
        if bad:
            raise SystemExit(f"frame {f}: unknown verdicts {bad}")
        for m in ann.get("missed", []):
            if m["cls"] not in CLASSES:
                raise SystemExit(f"frame {f}: unknown class in missed {m}")
            objs.append({"box": m["box"], "cls": m["cls"], "hard": bool(m.get("hard")), "tag": "added: " + m.get("note", "")})
        # Mover flags; a mover the reader did not list joins the inventory.
        fi = int(f)
        added = 0
        for o in objs:
            o["moving"] = False
        for mv in movers:
            for s in mv["sightings"]:
                if s["frame"] != fi:
                    continue
                hit = max(objs, key=lambda o: iou(o["box"], s["box"]), default=None)
                if hit and (iou(hit["box"], s["box"]) >= 0.2 or centre_in(hit["box"], s["box"]) or centre_in(s["box"], hit["box"])):
                    hit["moving"] = True; hit["mover"] = mv["id"]
                else:
                    objs.append({"box": s["box"], "cls": mv["cls"], "hard": bool(s.get("hard")), "moving": True, "mover": mv["id"], "tag": "added from the mover inventory"})
                    added += 1
        frames.append({"frame": fi, "t": fi / 30, "objects": objs, "ignore": [r["box"] for r in ann.get("ignore", [])],
                       "ignore_est": sum(r.get("est", 0) for r in ann.get("ignore", []))})
        log[f] = {"objects": len(objs), "from_candidates": sum(o["tag"].startswith("cand") for o in objs), "added_by_reader": sum(o["tag"].startswith("added:") for o in objs),
                  "added_from_movers": added, "movers": sum(o["moving"] for o in objs), "hard": sum(o["hard"] for o in objs), "ignore_regions": len(ann.get("ignore", []))}
    out = {"clip": a.clip, "width": 1920, "height": 1080, "fps": 30, "frames": frames,
           "method": "eye audit: verdicts on a detector-union candidate pool, every 2x tile scanned for misses, movers from motion-compensated strips"}
    pathlib.Path(a.out).write_text(json.dumps(out, indent=1))
    for f, l in log.items():
        print(f, l)
    print("total objects", sum(l["objects"] for l in log.values()), "movers", sum(l["movers"] for l in log.values()))


if __name__ == "__main__":
    main()
