"""Draw a detections.json (and optionally a ground-truth inventory) on frames of the clip, for eye inspection.

    python tools/recon3d/eval/draw_det.py DET.json CLIP.mp4 OUT_DIR [--frames 75,225] [--gt GT.json] [--conf 0.3]
        [--crop x1,y1,x2,y2] [--zoom 2]

Writes OUT_DIR/<det name>-f<frame>.jpg. Detections are green (class and confidence written next to the box);
with --gt, GT boxes are yellow when found, red and thick when missed, detections that are on nothing
(false) are magenta, duplicates orange, ignore regions grey. --frames defaults to the GT frames (with
--gt) or to the frames of DET.json. --crop / --zoom cut a source-pixel window out and magnify it (small
objects). The matching is score_det.py's.
"""
import argparse
import json
import pathlib
import sys

import cv2
import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from score_det import match, in_any  # noqa: E402

NAMES = {0: "person", 1: "bike", 2: "car", 3: "moto", 5: "bus", 7: "truck"}
GREEN, YELLOW, RED, MAGENTA, ORANGE, GREY = (0, 220, 0), (0, 230, 255), (0, 0, 255), (255, 0, 255), (0, 140, 255), (160, 160, 160)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("det")
    ap.add_argument("clip")
    ap.add_argument("out")
    ap.add_argument("--frames", default=None, help="comma list (default: the GT frames, else all in DET.json)")
    ap.add_argument("--gt", default=None)
    ap.add_argument("--conf", type=float, default=0.3)
    ap.add_argument("--crop", default=None, help="x1,y1,x2,y2 in source pixels")
    ap.add_argument("--zoom", type=float, default=1.0)
    ap.add_argument("--max-ratio", type=float, default=10.0)
    a = ap.parse_args()

    det = json.loads(pathlib.Path(a.det).read_text())
    by_frame = {f["i"]: f["dets"] for f in det["frames"]}
    gt = {f["frame"]: f for f in json.loads(pathlib.Path(a.gt).read_text())["frames"]} if a.gt else {}
    frames = [int(v) for v in a.frames.split(",")] if a.frames else sorted(gt) if gt else sorted(by_frame)
    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    crop = [int(v) for v in a.crop.split(",")] if a.crop else None
    cap = cv2.VideoCapture(a.clip)
    stem = pathlib.Path(a.det).parent.name + "-" + pathlib.Path(a.det).stem if pathlib.Path(a.det).stem == "detections" else pathlib.Path(a.det).stem
    for fi in frames:
        cap.set(cv2.CAP_PROP_POS_FRAMES, fi)
        ok, img = cap.read()
        if not ok:
            print(f"frame {fi}: cannot read")
            continue
        dets = [d for d in by_frame.get(fi, []) if d["conf"] >= a.conf]
        dbox = np.array([d["box"] for d in dets], float).reshape(-1, 4)
        g = gt.get(fi)
        colors = [GREEN] * len(dets)
        gcol, gthick = [], []
        if g:
            gbox = np.array([o["box"] for o in g["objects"]], float).reshape(-1, 4)
            d_of_g, g_of_d, on = match(dbox, gbox, a.max_ratio)
            gcol = [YELLOW if d >= 0 else RED for d in d_of_g]
            gthick = [1 if d >= 0 else 3 for d in d_of_g]
            for k, d in enumerate(dets):
                if g_of_d[k] >= 0:
                    continue
                c = ((d["box"][0] + d["box"][2]) / 2, (d["box"][1] + d["box"][3]) / 2)
                colors[k] = ORANGE if on[k].any() else GREY if in_any(c, g.get("ignore") or []) else MAGENTA
            for r in g.get("ignore") or []:
                cv2.rectangle(img, tuple(map(int, r[:2])), tuple(map(int, r[2:])), GREY, 1, cv2.LINE_AA)
            for o, col, th in zip(g["objects"], gcol, gthick):
                cv2.rectangle(img, tuple(int(v) - 2 for v in o["box"][:2]), tuple(int(v) + 2 for v in o["box"][2:]), col, th, cv2.LINE_AA)
        for d, col in zip(dets, colors):
            x1, y1, x2, y2 = map(int, d["box"])
            cv2.rectangle(img, (x1, y1), (x2, y2), col, 1, cv2.LINE_AA)
            cv2.putText(img, f"{NAMES.get(d['cls'], d['cls'])} {d['conf']:.2f}", (x1, max(10, y1 - 3)), cv2.FONT_HERSHEY_SIMPLEX, 0.35, col, 1, cv2.LINE_AA)
        if crop:
            img = img[crop[1]:crop[3], crop[0]:crop[2]]
        if a.zoom != 1.0:
            img = cv2.resize(img, None, fx=a.zoom, fy=a.zoom, interpolation=cv2.INTER_CUBIC)
        label = f"{stem} f{fi} conf>={a.conf:g}: {len(dets)} boxes"
        if g:
            label += f", GT {len(g['objects'])}, missed {sum(c == RED for c in gcol)}"
        cv2.putText(img, label, (8, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 3, cv2.LINE_AA)
        cv2.putText(img, label, (8, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 1, cv2.LINE_AA)
        path = out / f"{stem}-f{fi}.jpg"
        cv2.imwrite(str(path), img, [cv2.IMWRITE_JPEG_QUALITY, 92])
        print(path)


if __name__ == "__main__":
    main()
