"""Draw the Android chain's verdicts (MOVING / static, track id) back onto the drone footage.

The chain itself runs on the JVM (`PhoneReplayTest.droneClipWalkersAreMovers`: Lift3D -> Tracker ->
OverlayMotion on the boxes of a `tools/footage` run); this only paints what it decided, box by box,
so the result is the clip with the phone's amber MOVING tag on whoever the chain says moves.

    tools/footage/.venv/Scripts/python tools/movers/footage_boxes.py runs/footage/<run> --out android/app/src/test/resources/replay/drone-meva.csv
    cd android && MINBAND_REPLAY_EXPORT=$PWD/../runs/footage/<run>/verdicts.csv ./gradlew :app:testDebugUnitTest --tests "*PhoneReplayTest.droneClip*" --rerun
    tools/footage/.venv/Scripts/python tools/movers/render_replay.py runs/footage/<run> \
        --boxes android/app/src/test/resources/replay/drone-meva.csv --verdicts runs/footage/<run>/verdicts.csv --out runs/footage/<run>/movers-demo.mp4

Amber box + "MOVING": the chain tagged it (its track is moving, or the box sits on a mover). Grey box
with an id: tracked, static. Thin grey: detected, no track yet. Verdicts hold until the next
detection frame (the detector ran on every 6th frame).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import defaultdict

import cv2
import numpy as np

AMBER = (0, 170, 255)      # BGR
GREY = (190, 190, 190)
FAINT = (110, 110, 110)
INK = (20, 20, 20)


def load_boxes(path):
    """{replay frame: [(ox, oy, ow, oh, cls), ...]} in file order (the JVM's detection index)."""
    by_frame = defaultdict(list)
    with open(path) as fh:
        lines = [l for l in fh.read().splitlines() if l.strip()]
    rd = csv.DictReader(lines[1:])
    if "ox" not in (rd.fieldnames or []):
        sys.exit("the boxes CSV has no original-frame columns: regenerate it with footage_boxes.py")
    for r in rd:
        by_frame[int(r["frame"])].append((float(r["ox"]), float(r["oy"]), float(r["ow"]), float(r["oh"]), int(r["cls"])))
    return by_frame


def load_verdicts(path):
    """{replay frame: {detection index: (id or None, moving)}}"""
    out = defaultdict(dict)
    with open(path) as fh:
        for r in csv.DictReader(fh):
            out[int(r["frame"])][int(r["i"])] = (int(r["id"]) if r["id"] else None, r["moving"] == "1")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("run", help="tools/footage run directory (detect.json names the clip)")
    ap.add_argument("--boxes", required=True, help="replay CSV from footage_boxes.py (with ox..oh)")
    ap.add_argument("--verdicts", required=True, help="CSV written by PhoneReplayTest with MINBAND_REPLAY_EXPORT")
    ap.add_argument("--out", required=True, help="output .mp4")
    ap.add_argument("--clips", default=os.path.join(os.path.dirname(__file__), "..", "footage", "clips"), help="where the clip lives")
    ap.add_argument("--width", type=int, default=1920, help="output width (the 4K clip is downscaled)")
    ap.add_argument("--max-seconds", type=float, default=0, help="stop after this many seconds (0 = the whole run)")
    args = ap.parse_args()

    meta = json.load(open(os.path.join(args.run, "detect.json")))
    video = os.path.join(args.clips, meta["video"])
    f0, f1, fps = int(meta.get("start_frame", 0)), int(meta.get("end_frame", 10 ** 9)), float(meta["fps"])
    boxes = load_boxes(args.boxes)
    verdicts = load_verdicts(args.verdicts)
    det_frames = sorted(boxes)

    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        sys.exit(f"cannot open {video}")
    W, H = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    scale = args.width / W
    ow, oh = args.width, int(round(H * scale / 2) * 2)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    writer = None
    for fourcc in ("avc1", "mp4v"):
        writer = cv2.VideoWriter(args.out, cv2.VideoWriter_fourcc(*fourcc), fps, (ow, oh))
        if writer.isOpened():
            break
        writer.release(); writer = None
    if writer is None:
        sys.exit("no mp4 encoder in this OpenCV build")

    cap.set(cv2.CAP_PROP_POS_FRAMES, f0)
    last_det = None
    n = 0
    movers_seen, ids_seen = set(), set()
    font = cv2.FONT_HERSHEY_SIMPLEX
    hold = int(meta.get("every", 6)) + 1
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        fr = f0 + n                      # the clip's frame number
        if fr > f1 or (args.max_seconds and n / fps > args.max_seconds):
            break
        rf = fr - f0                     # the replay's frame number
        if rf in boxes:
            last_det = rf
        img = cv2.resize(frame, (ow, oh), interpolation=cv2.INTER_AREA)
        n_people = n_movers = 0
        if last_det is not None and rf - last_det < hold:
            vs = verdicts.get(last_det, {})
            for i, (bx, by, bw, bh, cls) in enumerate(boxes[last_det]):
                tid, moving = vs.get(i, (None, False))
                x0, y0 = int(bx * ow), int(by * oh)
                x1, y1 = int((bx + bw) * ow), int((by + bh) * oh)
                n_people += 1
                if moving:
                    n_movers += 1
                    movers_seen.add(tid)
                    cv2.rectangle(img, (x0 - 2, y0 - 2), (x1 + 2, y1 + 2), AMBER, 2)
                    label = f"{tid if tid is not None else ''} MOVING".strip()
                    (tw, th), _ = cv2.getTextSize(label, font, 0.5, 1)
                    cv2.rectangle(img, (x0 - 2, y0 - th - 10), (x0 + tw + 6, y0 - 2), AMBER, -1)
                    cv2.putText(img, label, (x0 + 2, y0 - 6), font, 0.5, INK, 1, cv2.LINE_AA)
                elif tid is not None:
                    ids_seen.add(tid)
                    cv2.rectangle(img, (x0, y0), (x1, y1), GREY, 1)
                    cv2.putText(img, str(tid), (x0, max(10, y0 - 4)), font, 0.4, GREY, 1, cv2.LINE_AA)
                else:
                    cv2.rectangle(img, (x0, y0), (x1, y1), FAINT, 1)
        # HUD: what this is, the clock, the counts, the legend.
        cv2.rectangle(img, (0, 0), (ow, 46), (30, 30, 30), -1)
        cv2.putText(img, "MinBand  |  phone tracking chain replayed on drone footage (MEVA UAV, 28 m up, 4K, detector 5 Hz)", (12, 20), font, 0.55, (235, 235, 235), 1, cv2.LINE_AA)
        cv2.putText(img, f"t = {n / fps:5.1f} s    people in view {n_people:3d}    MOVING {n_movers:3d}", (12, 40), font, 0.55, (235, 235, 235), 1, cv2.LINE_AA)
        cv2.rectangle(img, (ow - 330, 8), (ow - 310, 22), AMBER, -1)
        cv2.putText(img, "MOVING (chain's verdict)", (ow - 300, 20), font, 0.5, (235, 235, 235), 1, cv2.LINE_AA)
        cv2.rectangle(img, (ow - 330, 28), (ow - 310, 42), GREY, 1)
        cv2.putText(img, "tracked, static", (ow - 300, 40), font, 0.5, (235, 235, 235), 1, cv2.LINE_AA)
        writer.write(img)
        n += 1
        if n % 300 == 0:
            print(f"  {n / fps:.0f} s", flush=True)
    writer.release(); cap.release()
    print(f"{n} frames -> {args.out} ({ow}x{oh}); movers tagged at some point: {len(movers_seen)}, other tracked ids: {len(ids_seen - movers_seen)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
