"""Turn a `tools/footage` run (drone footage: `track.py detect` then `track.py track`) into a replay
CSV for the Android tracking chain (`PhoneReplayTest`), the same shape `phone_detect.py` writes:
one row per box, `frame,t,cls,conf,x,y,w,h` normalized to the reference frame, and a header
line with the frame size, rate and the ground fit's camera (focal length in pixels, pitch, height),
so the JVM test can lift the boxes to the ground the way the phone does and judge who moves.

    .venv/bin/python track.py detect clips/<clip>.mp4 --model yolov8n.onnx --start 0 --end 60 --out ../../runs/footage/<run>
    .venv/bin/python track.py track ../../runs/footage/<run>
    python tools/movers/footage_boxes.py runs/footage/<run> --out android/app/src/test/resources/replay/drone-<name>.csv

Each box is mapped through its frame's homography onto the reference frame (the drone drifts; the
registration undoes that), so the replay sees a fixed camera. Only people are kept (the phone's
classes; vehicles have no width prior there).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys

import numpy as np


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("run", help="tools/footage run directory (detections.npy, detect.json, summary.json)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--classes", default="0", help="comma-separated COCO class ids to keep (default: person)")
    ap.add_argument("--max-height", type=float, default=0.0,
                    help="drop boxes taller than this fraction of the frame: the tile detector's junk (on the MEVA clip people are under 0.03 of a 4K frame at 28 m up; 0.05 removed 1564 boxes of 108-172 px that were a third of the movers)")
    args = ap.parse_args()

    dets = np.load(os.path.join(args.run, "detections.npy"))
    meta = json.load(open(os.path.join(args.run, "detect.json")))
    summary = json.load(open(os.path.join(args.run, "summary.json")))
    ground = summary["ground"]
    homs = {int(k): np.array(v, np.float64) for k, v in meta["homographies"].items()}
    keep = {int(c) for c in args.classes.split(",")}
    w, h, fps = meta["width"], meta["height"], meta["fps"]
    f0 = meta.get("start_frame", 0)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    n = 0
    with open(args.out, "w", newline="") as fh:
        fh.write(f"# video={meta['video']} width={w} height={h} fps={fps:.3f} model={meta.get('model', '?')} "
                 f"cam_f={ground['f_px']:.1f} cam_pitch={ground['pitch_deg']:.2f} cam_height={ground['height_m']:.2f} source=footage\n")
        wr = csv.writer(fh)
        # ox..oh: the same box in its own video frame (normalized), for drawing the replay's
        # verdicts back onto the footage (`render_replay.py`); the JVM reader ignores them.
        wr.writerow(["frame", "t", "cls", "conf", "x", "y", "w", "h", "ox", "oy", "ow", "oh"])
        for row in dets:
            frame, x1, y1, x2, y2, conf, cls = row
            if int(cls) not in keep:
                continue
            if args.max_height and (y2 - y1) / h > args.max_height:
                continue
            H = homs.get(int(frame))
            if H is None:
                continue
            # The four corners through the homography, then their bounding box.
            pts = np.array([[x1, y1, 1], [x2, y1, 1], [x1, y2, 1], [x2, y2, 1]], np.float64).T
            q = H @ pts
            q = q[:2] / q[2]
            bx0, by0 = q[0].min() / w, q[1].min() / h
            bx1, by1 = q[0].max() / w, q[1].max() / h
            bx0, by0, bx1, by1 = max(0.0, bx0), max(0.0, by0), min(1.0, bx1), min(1.0, by1)
            if bx1 - bx0 < 1e-3 or by1 - by0 < 1e-3:
                continue
            fr = int(frame) - f0
            wr.writerow([fr, f"{fr / fps:.3f}", int(cls), f"{conf:.3f}", f"{bx0:.4f}", f"{by0:.4f}", f"{bx1 - bx0:.4f}", f"{by1 - by0:.4f}",
                         f"{x1 / w:.4f}", f"{y1 / h:.4f}", f"{(x2 - x1) / w:.4f}", f"{(y2 - y1) / h:.4f}"])
            n += 1
    print(f"{n} boxes -> {args.out} (camera f {ground['f_px']:.0f} px, pitch {ground['pitch_deg']:.1f} deg, {ground['height_m']:.1f} m up)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
